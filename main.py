"""Run the local video archive HTTP service."""

import json
import logging
import re
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from archive import ArchiveService, MultiArchiveService, show
from config import Settings
from storage import VideoStorage, parse_recording_name


LOG = logging.getLogger(__name__)
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def parse_range_header(value, size):
    """Return an inclusive (start, end) byte range, or None if invalid."""
    if not value:
        return None
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise ValueError("Некорректный диапазон байтов")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("Некорректный диапазон байтов")
    if not first:
        suffix_length = int(last)
        if suffix_length <= 0:
            raise ValueError("Некорректный диапазон байтов")
        return max(0, size - suffix_length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Диапазон байтов вне размера файла")
    return start, min(end, size - 1)


def make_handler(storage: VideoStorage, archive):
    class VideoRequestHandler(BaseHTTPRequestHandler):
        server_version = "VideoArchive/1.0"

        def log_message(self, fmt, *args):
            LOG.info("HTTP %s - %s", self.address_string(), fmt % args)

        def _json(self, status, value):
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in _STATIC_FILES:
                self._serve_static(path)
            elif path == "/api/health":
                self._json(200, {"status": "ok", "archive": archive.status})
            elif path == "/api/videos":
                try:
                    self._json(200, {"videos": storage.list_recordings()})
                except OSError as exc:
                    self._json(500, {"error": "Не удалось прочитать каталог записей", "detail": str(exc)})
            elif path.startswith("/api/videos/"):
                raw_name = path[len("/api/videos/"):]
                self._serve_video(unquote(raw_name))
            else:
                self._json(404, {"error": "Маршрут не найден"})

        def do_HEAD(self):
            path = urlsplit(self.path).path
            if path in _STATIC_FILES:
                self._serve_static(path)
            elif path.startswith("/api/videos/"):
                self._serve_video(unquote(path[len("/api/videos/"):]))
            elif path == "/api/health":
                self._json(200, {"status": "ok", "archive": archive.status})
            else:
                self._json(404, {"error": "Маршрут не найден"})

        def _serve_static(self, route):
            filename, content_type = _STATIC_FILES[route]
            try:
                payload = (_STATIC_ROOT / filename).read_bytes()
            except OSError:
                LOG.exception("Не удалось прочитать статический файл %s", filename)
                self._json(500, {"error": "Не удалось загрузить страницу интерфейса"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; media-src 'self'; "
                "img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _serve_video(self, name):
            if not parse_recording_name(name):
                self._json(400, {"error": "Недопустимое имя видео"})
                return
            video_path = show(storage, name)
            if video_path is None:
                self._json(404, {"error": "Видео не найдено"})
                return
            size = video_path.stat().st_size
            try:
                requested = parse_range_header(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */{}".format(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if requested is None:
                start, end, status = 0, size - 1, 200
            else:
                start, end = requested
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with video_path.open("rb") as video:
                    video.seek(start)
                    remaining = length
                    while remaining:
                        chunk = video.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                LOG.debug("Клиент прервал воспроизведение %s", name)
            except OSError:
                LOG.exception("Ошибка чтения видео %s", name)

    return VideoRequestHandler


class VideoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(settings=None):
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    storage = VideoStorage(
        settings.recordings_dir, settings.temp_dir,
        settings.min_free_bytes, settings.ffprobe_bin,
    )
    archive = (
        MultiArchiveService(settings, storage)
        if settings.cameras else ArchiveService(settings, storage)
    )
    server = VideoHTTPServer(
        (settings.host, settings.port), make_handler(storage, archive)
    )
    previous = {}
    try:
        archive.start()
        LOG.info("HTTP API запущен на http://%s:%s", settings.host, settings.port)
        if settings.host not in ("127.0.0.1", "localhost", "::1"):
            LOG.warning("Сервер доступен по сети; ограничьте доступ правилами брандмауэра")

        def request_shutdown(signum, frame):
            raise KeyboardInterrupt()

        previous = {}
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    previous[sig] = signal.signal(sig, request_shutdown)
                except (ValueError, OSError):
                    pass
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        LOG.info("Получен запрос на остановку")
    finally:
        server.server_close()
        archive.stop()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    run()

