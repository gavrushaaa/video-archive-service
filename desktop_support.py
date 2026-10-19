"""Desktop app data paths and verified FFmpeg bootstrap downloads."""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Tuple


APP_NAME = "VideoArchiveService"
WINDOWS_FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
WINDOWS_CHECKSUM_URL = WINDOWS_FFMPEG_URL + ".sha256"
GITHUB_RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024


def app_data_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_ffmpeg_pair() -> Optional[Tuple[str, str]]:
    """Return an installed ffmpeg/ffprobe pair, or None if either is missing."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe

    executable_suffix = ".exe" if os.name == "nt" else ""
    installed = app_data_dir() / "tools" / "ffmpeg"
    local_pair = (installed / ("ffmpeg" + executable_suffix), installed / ("ffprobe" + executable_suffix))
    if all(path.is_file() for path in local_pair):
        return str(local_pair[0]), str(local_pair[1])
    return None


def _open(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "VideoArchiveService/1.0", "Accept": "*/*"},
    )
    return urllib.request.urlopen(request, timeout=45)


def _read_text(url: str) -> str:
    with _open(url) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise RuntimeError("Ответ сервера загрузки превышает допустимый размер")
    return data.decode("utf-8", errors="strict")


def _download(url: str, destination: Path, progress: Callable[[int, int], None]) -> str:
    digest = hashlib.sha256()
    written = 0
    with _open(url) as response, destination.open("wb") as output:
        try:
            total = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            total = 0
        if total > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("Архив FFmpeg больше допустимого лимита загрузки")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Архив FFmpeg превысил допустимый лимит загрузки")
            digest.update(chunk)
            output.write(chunk)
            progress(written, total)
        output.flush()
        os.fsync(output.fileno())
    if total and written != total:
        raise RuntimeError("Загрузка FFmpeg завершилась не полностью")
    return digest.hexdigest()


def _linux_asset() -> Tuple[str, str]:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        architecture = "linux64"
    elif machine in ("aarch64", "arm64"):
        architecture = "linuxarm64"
    else:
        raise RuntimeError("Автозагрузка FFmpeg не поддерживает Linux " + machine)

    release = json.loads(_read_text(GITHUB_RELEASE_API))
    assets = release.get("assets", [])
    archive_pattern = re.compile(
        r"^ffmpeg-n[0-9]+(?:\.[0-9]+)*-latest-{}-gpl-[0-9]+(?:\.[0-9]+)*\.tar\.xz$".format(
            architecture
        )
    )
    archive = next((item for item in assets if archive_pattern.fullmatch(item.get("name", ""))), None)
    checksums = next((item for item in assets if item.get("name") == "checksums.sha256"), None)
    if not archive or not checksums:
        raise RuntimeError("В последнем релизе FFmpeg не найден подходящий Linux-архив и SHA-256")

    manifest = _read_text(checksums["browser_download_url"])
    expected = None
    for line in manifest.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("* ").strip() == archive["name"]:
            expected = parts[0].lower()
            break
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("В контрольной сумме Linux-архива FFmpeg некорректная запись")
    return archive["browser_download_url"], expected


def _copy_zip_binaries(archive_path: Path, destination: Path) -> None:
    targets = {"ffmpeg.exe": destination / "ffmpeg.exe", "ffprobe.exe": destination / "ffprobe.exe"}
    staging = {}
    with zipfile.ZipFile(str(archive_path)) as archive:
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                continue
            name = path.name.lower()
            if name not in targets or len(path.parts) < 2 or path.parts[-2].lower() != "bin":
                continue
            if info.is_dir():
                continue
            temp_path = destination / ("." + name + ".part")
            with archive.open(info, "r") as source, temp_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            staging[name] = temp_path
    if len(staging) != len(targets):
        for path in staging.values():
            path.unlink(missing_ok=True)
        raise RuntimeError("В архиве FFmpeg не найдены ffmpeg.exe и ffprobe.exe")
    for name, target in targets.items():
        os.replace(staging[name], target)


def _copy_tar_binaries(archive_path: Path, destination: Path) -> None:
    targets = {"ffmpeg": destination / "ffmpeg", "ffprobe": destination / "ffprobe"}
    staging = {}
    with tarfile.open(str(archive_path), mode="r:xz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not member.isfile():
                continue
            name = path.name
            if name not in targets or len(path.parts) < 2 or path.parts[-2] != "bin":
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            temp_path = destination / ("." + name + ".part")
            with source, temp_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            temp_path.chmod(0o755)
            staging[name] = temp_path
    if len(staging) != len(targets):
        for path in staging.values():
            path.unlink(missing_ok=True)
        raise RuntimeError("В архиве FFmpeg не найдены ffmpeg и ffprobe")
    for name, target in targets.items():
        os.replace(staging[name], target)
        target.chmod(0o755)


def install_ffmpeg(progress: Callable[[int, int], None]) -> Tuple[str, str]:
    """Download, verify and install static FFmpeg tools for this OS/user."""
    root = app_data_dir()
    destination = root / "tools" / "ffmpeg"
    destination.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        archive_url = WINDOWS_FFMPEG_URL
        checksum_text = _read_text(WINDOWS_CHECKSUM_URL)
        match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
        if not match:
            raise RuntimeError("Не удалось прочитать SHA-256 архива FFmpeg для Windows")
        expected = match.group(1).lower()
        archive_name = "ffmpeg.zip"
    elif platform.system() == "Linux":
        archive_url, expected = _linux_asset()
        archive_name = "ffmpeg.tar.xz"
    else:
        raise RuntimeError("Автоматическая установка FFmpeg поддерживается на Windows и Linux")

    with tempfile.TemporaryDirectory(prefix="ffmpeg-", dir=str(root)) as temp_dir:
        archive_path = Path(temp_dir) / archive_name
        actual = _download(archive_url, archive_path, progress)
        if actual != expected:
            raise RuntimeError("SHA-256 архива FFmpeg не совпал; архив не установлен")
        if os.name == "nt":
            _copy_zip_binaries(archive_path, destination)
            ffmpeg_path = destination / "ffmpeg.exe"
            ffprobe_path = destination / "ffprobe.exe"
        else:
            _copy_tar_binaries(archive_path, destination)
            ffmpeg_path = destination / "ffmpeg"
            ffprobe_path = destination / "ffprobe"

    for executable in (ffmpeg_path, ffprobe_path):
        result = subprocess.run(
            [str(executable), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Установленный FFmpeg не запускается: {}".format(executable.name))
    return str(ffmpeg_path), str(ffprobe_path)
