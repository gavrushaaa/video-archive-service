"""Environment-based configuration for the video archive."""

import os
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


_CAMERA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE
)


def _valid_camera_id(value) -> bool:
    """Check IDs that are safe as path components on both Windows and Linux."""
    return bool(
        isinstance(value, str)
        and _CAMERA_ID_RE.fullmatch(value)
        and not value.endswith(".")
        and not _WINDOWS_RESERVED_NAME_RE.match(value)
    )


@dataclass(frozen=True)
class CameraSource:
    camera_id: str
    url: str


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    recordings_dir: Path = Path("recordings")
    inbox_dir: Path = Path("inbox")
    temp_dir: Path = Path("temp")
    segment_seconds: float = 3600.0
    source_type: str = "none"
    source_url: Optional[str] = None
    camera_id: Optional[str] = None
    cameras: Tuple[CameraSource, ...] = ()
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    source_timeout_seconds: float = 15.0
    retry_seconds: float = 5.0
    inbox_stability_seconds: float = 3.0
    min_free_bytes: int = 268435456
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, base_dir: Optional[Path] = None):
        """Load settings from VIDEO_* environment variables."""
        root = (base_dir or Path.cwd()).resolve()

        def path_setting(name, default):
            value = os.environ.get(name)
            path = Path(value) if value else default
            return path if path.is_absolute() else (root / path)

        cameras = cls._parse_cameras(os.environ.get("VIDEO_CAMERAS", ""))
        source_type = os.environ.get("VIDEO_SOURCE", "none").strip().lower()
        source_url = os.environ.get("VIDEO_SOURCE_URL") or None
        camera_id = os.environ.get("VIDEO_CAMERA_ID") or None
        if cameras and (source_type != "none" or source_url or camera_id):
            raise ValueError(
                "Используйте либо VIDEO_CAMERAS, либо VIDEO_SOURCE/VIDEO_SOURCE_URL/VIDEO_CAMERA_ID"
            )

        try:
            settings = cls(
                host=os.environ.get("VIDEO_HOST", "127.0.0.1"),
                port=int(os.environ.get("VIDEO_PORT", "8765")),
                recordings_dir=path_setting("VIDEO_RECORDINGS_DIR", root / "recordings"),
                inbox_dir=path_setting("VIDEO_INBOX_DIR", root / "inbox"),
                temp_dir=path_setting("VIDEO_TEMP_DIR", root / "temp"),
                segment_seconds=float(os.environ.get("VIDEO_SEGMENT_SECONDS", "3600")),
                source_type=source_type,
                source_url=source_url,
                camera_id=camera_id,
                cameras=cameras,
                ffmpeg_bin=os.environ.get("VIDEO_FFMPEG", "ffmpeg"),
                ffprobe_bin=os.environ.get("VIDEO_FFPROBE", "ffprobe"),
                source_timeout_seconds=float(os.environ.get("VIDEO_SOURCE_TIMEOUT_SECONDS", "15")),
                retry_seconds=float(os.environ.get("VIDEO_RETRY_SECONDS", "5")),
                inbox_stability_seconds=float(
                    os.environ.get("VIDEO_INBOX_STABILITY_SECONDS", "3")
                ),
                min_free_bytes=int(os.environ.get("VIDEO_MIN_FREE_BYTES", "268435456")),
                log_level=os.environ.get("VIDEO_LOG_LEVEL", "INFO").upper(),
            )
        except ValueError as exc:
            raise ValueError("Некорректное числовое значение в настройках VIDEO_*") from exc

        if not 1 <= settings.port <= 65535:
            raise ValueError("VIDEO_PORT должен быть в диапазоне 1..65535")
        if settings.segment_seconds <= 0:
            raise ValueError("VIDEO_SEGMENT_SECONDS должен быть больше нуля")
        if settings.source_timeout_seconds <= 0:
            raise ValueError("VIDEO_SOURCE_TIMEOUT_SECONDS должен быть больше нуля")
        if settings.retry_seconds < 0 or settings.inbox_stability_seconds < 0:
            raise ValueError("Задержки VIDEO_RETRY_SECONDS и VIDEO_INBOX_STABILITY_SECONDS не могут быть отрицательными")
        if settings.min_free_bytes < 0:
            raise ValueError("VIDEO_MIN_FREE_BYTES не может быть отрицательным")
        if settings.source_type not in ("none", "synthetic", "ffmpeg", "inbox"):
            raise ValueError("VIDEO_SOURCE должен быть none, synthetic, ffmpeg или inbox")
        if settings.source_type == "ffmpeg" and not settings.source_url:
            raise ValueError("Для VIDEO_SOURCE=ffmpeg укажите VIDEO_SOURCE_URL")
        if settings.camera_id and not _valid_camera_id(settings.camera_id):
            raise ValueError(
                "VIDEO_CAMERA_ID должен содержать 1..64 символа из букв ASCII, цифр, точки, дефиса "
                "и подчёркивания, не оканчиваться точкой и не быть зарезервированным именем Windows"
            )
        return settings

    @staticmethod
    def _parse_cameras(raw: str) -> Tuple[CameraSource, ...]:
        if not raw.strip():
            return ()
        try:
            values = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("VIDEO_CAMERAS должен содержать JSON-массив камер") from exc
        if not isinstance(values, list) or not values:
            raise ValueError("VIDEO_CAMERAS должен быть непустым JSON-массивом камер")

        cameras = []
        seen_ids = set()
        for index, value in enumerate(values, 1):
            if not isinstance(value, dict):
                raise ValueError("Элемент {} в VIDEO_CAMERAS должен быть объектом".format(index))
            camera_id = value.get("id")
            url = value.get("url")
            if not _valid_camera_id(camera_id):
                raise ValueError(
                    "У каждой камеры в VIDEO_CAMERAS должен быть переносимый id: 1..64 символа из букв ASCII, "
                    "цифр, точки, дефиса и подчёркивания; нельзя оканчивать id точкой или использовать "
                    "зарезервированное имя Windows"
                )
            normalized_id = camera_id.casefold()
            if normalized_id in seen_ids:
                raise ValueError(
                    "Идентификаторы камер в VIDEO_CAMERAS должны быть уникальными без учёта регистра"
                )
            if not isinstance(url, str) or not url.strip():
                raise ValueError("У каждой камеры в VIDEO_CAMERAS должен быть непустой url")
            if not url.lower().startswith(("rtsp://", "http://", "https://")):
                raise ValueError("Адрес камеры в VIDEO_CAMERAS должен начинаться с rtsp://, http:// или https://")
            seen_ids.add(normalized_id)
            cameras.append(CameraSource(camera_id=camera_id, url=url.strip()))
        return tuple(cameras)

