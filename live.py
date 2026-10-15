"""Low-latency MJPEG previews for configured video sources."""

import subprocess
import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class LiveSource:
    source_id: str
    label: str
    input_args: Tuple[str, ...]


class LivePreviewError(Exception):
    """Base error raised while opening a live preview."""


class LiveSourceNotFound(LivePreviewError):
    """The requested source is not configured for live viewing."""


class LivePreviewBusy(LivePreviewError):
    """The maximum number of concurrent previews has been reached."""


class LivePreviewManager:
    """Build FFmpeg streams for synthetic or configured network sources.

    Each viewer gets a short-lived FFmpeg process and a separate source
    connection. The number of concurrent viewers is bounded to avoid
    unbounded FFmpeg processes and camera sessions.
    """

    BOUNDARY = "frame"
    MAX_VIEWERS = 4

    def __init__(self, settings):
        self.ffmpeg_bin = settings.ffmpeg_bin
        self.source_timeout_seconds = settings.source_timeout_seconds
        self._sources: Dict[str, LiveSource] = {}
        self._slots = threading.BoundedSemaphore(self.MAX_VIEWERS)

        if settings.cameras:
            for camera in settings.cameras:
                self._sources[camera.camera_id] = LiveSource(
                    source_id=camera.camera_id,
                    label=camera.camera_id,
                    input_args=self._network_input(camera.url),
                )
        elif settings.source_type == "synthetic":
            self._sources["synthetic"] = LiveSource(
                source_id="synthetic",
                label="Тестовый источник",
                input_args=(
                    "-re", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=15",
                ),
            )
        elif settings.source_type == "ffmpeg" and settings.source_url:
            camera_id = settings.camera_id or "camera"
            self._sources[camera_id] = LiveSource(
                source_id=camera_id,
                label=settings.camera_id or "Камера",
                input_args=self._network_input(settings.source_url),
            )

    def _network_input(self, url: str) -> Tuple[str, ...]:
        args: List[str] = []
        if url.lower().startswith("rtsp://"):
            args.extend(("-rtsp_transport", "tcp"))
        args.extend((
            "-rw_timeout", str(int(self.source_timeout_seconds * 1_000_000)),
            "-i", url,
        ))
        return tuple(args)

    def list_sources(self) -> List[dict]:
        return [
            {"id": source.source_id, "label": source.label}
            for source in self._sources.values()
        ]

    def _command(self, source: LiveSource) -> List[str]:
        return [
            self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            *source.input_args,
            "-map", "0:v:0", "-an",
            "-vf", "fps=8,scale=640:-2:flags=fast_bilinear",
            "-c:v", "mjpeg", "-q:v", "6",
            "-f", "mpjpeg", "-boundary_tag", self.BOUNDARY,
            "pipe:1",
        ]

    def start(self, source_id: str):
        source = self._sources.get(source_id)
        if source is None:
            raise LiveSourceNotFound(source_id)
        if not self._slots.acquire(blocking=False):
            raise LivePreviewBusy("Слишком много одновременных live-просмотров")
        try:
            return subprocess.Popen(
                self._command(source),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception:
            self._slots.release()
            raise

    def stop(self, process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
        finally:
            self._slots.release()
