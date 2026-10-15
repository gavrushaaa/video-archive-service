"""Safe filesystem operations for finished video recordings."""

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


LOG = logging.getLogger(__name__)
_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
    r"(?:__(?P<camera>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?\.mp4$"
)


class StorageError(Exception):
    """Raised when an archive operation cannot be completed safely."""


def parse_recording_name(name: str) -> Optional[Tuple[datetime, Optional[str]]]:
    match = _NAME_RE.fullmatch(name)
    if not match:
        return None
    try:
        started = datetime.strptime(
            match.group("date") + "_" + match.group("time"), "%Y-%m-%d_%H-%M-%S"
        )
    except ValueError:
        return None
    return started, match.group("camera")


def format_recording_name(started: datetime, camera_id: Optional[str] = None) -> str:
    name = started.strftime("%Y-%m-%d_%H-%M-%S")
    if camera_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", camera_id):
            raise ValueError("Недопустимый идентификатор камеры")
        name += "__" + camera_id
    return name + ".mp4"


class VideoStorage:
    def __init__(self, recordings_dir: Path, temp_dir: Path, min_free_bytes: int = 0,
                 ffprobe_bin: str = "ffprobe"):
        self.recordings_dir = Path(recordings_dir).resolve()
        self.temp_dir = Path(temp_dir).resolve()
        self.min_free_bytes = min_free_bytes
        self.ffprobe_bin = ffprobe_bin
        self._duration_cache: Dict[Tuple[str, int, int], Optional[float]] = {}

    def ensure_directories(self) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def new_temp_path(self, suffix: str = ".mp4") -> Path:
        self.ensure_directories()
        fd, path = tempfile.mkstemp(prefix="segment-", suffix=suffix, dir=str(self.temp_dir))
        os.close(fd)
        return Path(path)

    def _check_space(self, required_bytes: int) -> None:
        free = shutil.disk_usage(str(self.recordings_dir)).free
        if free - required_bytes < self.min_free_bytes:
            raise StorageError(
                "Недостаточно свободного места: после записи должен остаться запас "
                "не менее {} байт".format(self.min_free_bytes)
            )

    def _publish(self, source: Path, name: str) -> Path:
        parsed = parse_recording_name(name)
        if not parsed:
            raise StorageError("Имя записи не соответствует формату архива")
        source = Path(source)
        if not source.is_file() or source.stat().st_size <= 0:
            raise StorageError("Источник готовой записи отсутствует или пуст")

        self.ensure_directories()
        size = source.stat().st_size
        self._check_space(size)
        destination = self.recordings_dir / name
        staging = None
        try:
            fd, staging_text = tempfile.mkstemp(
                prefix=".incoming-", suffix=".mp4", dir=str(self.recordings_dir)
            )
            staging = Path(staging_text)
            with os.fdopen(fd, "wb") as target, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            # A same-directory hard link makes publication atomic and refuses to
            # replace an existing recording on both Windows and POSIX systems.
            os.link(str(staging), str(destination))
            staging.unlink()
            staging = None
            return destination
        except FileExistsError:
            raise StorageError("Запись уже существует; перезапись запрещена: {}".format(name))
        except OSError as exc:
            raise StorageError("Не удалось сохранить запись {}: {}".format(name, exc)) from exc
        finally:
            if staging is not None:
                try:
                    staging.unlink()
                except OSError:
                    LOG.warning("Не удалось удалить временный файл публикации: %s", staging)

    def publish_segment(self, temporary_path: Path, started: datetime,
                        camera_id: Optional[str] = None) -> Path:
        name = format_recording_name(started, camera_id)
        destination = self._publish(temporary_path, name)
        try:
            Path(temporary_path).unlink()
        except OSError:
            LOG.warning("Запись опубликована, но временный файл не удалён: %s", temporary_path)
        return destination

    def import_file(self, source_path: Path) -> Path:
        source_path = Path(source_path)
        name = source_path.name
        if not parse_recording_name(name):
            raise StorageError("Имя входящего файла не соответствует формату YYYY-MM-DD_HH-MM-SS[__camera].mp4")
        destination = self._publish(source_path, name)
        try:
            source_path.unlink()
        except OSError:
            LOG.warning("Файл сохранён, но исходник inbox не удалён: %s", source_path)
        ready_marker = Path(str(source_path) + ".ready")
        try:
            ready_marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning("Не удалось удалить маркер готовности: %s", ready_marker)
        return destination

    def path_for(self, name: str) -> Optional[Path]:
        if not parse_recording_name(name):
            return None
        candidate = self.recordings_dir / name
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.parent != self.recordings_dir or not resolved.is_file():
                return None
            return resolved
        except (OSError, RuntimeError):
            return None

    def list_recordings(self) -> List[dict]:
        self.ensure_directories()
        recordings = []
        for path in self.recordings_dir.iterdir():
            parsed = parse_recording_name(path.name)
            if not parsed:
                continue
            safe_path = self.path_for(path.name)
            if safe_path is None:
                continue
            started, camera_id = parsed
            stat = safe_path.stat()
            recordings.append({
                "name": path.name,
                "started_at": started.isoformat(timespec="seconds"),
                "size_bytes": stat.st_size,
                "duration_seconds": self._duration(safe_path, stat.st_mtime_ns, stat.st_size),
                "camera_id": camera_id,
            })
        recordings.sort(key=lambda item: item["started_at"], reverse=True)
        return recordings

    def _duration(self, path: Path, modified_ns: int, size: int) -> Optional[float]:
        key = (str(path), modified_ns, size)
        if key in self._duration_cache:
            return self._duration_cache[key]
        duration = None
        try:
            completed = subprocess.run(
                [self.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                timeout=5, check=False,
            )
            if completed.returncode == 0:
                value = float(completed.stdout.strip())
                if math.isfinite(value) and value >= 0:
                    duration = value
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._duration_cache[key] = duration
        return duration

