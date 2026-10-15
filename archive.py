"""Background orchestration for receiving, segmenting and storing video."""

import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from receiver import InboxReceiver, SourceError, build_receiver
from storage import VideoStorage


LOG = logging.getLogger(__name__)


def get(receiver, stop_event, on_started, on_completed):
    """Run a video source until it stops, forwarding finalized segments."""
    return receiver.run(stop_event, on_started, on_completed)


def push(storage: VideoStorage, temporary_path: Path, started: datetime,
         camera_id: Optional[str] = None) -> Path:
    """Publish one completed segment into the archive."""
    return storage.publish_segment(temporary_path, started, camera_id)


def show(storage: VideoStorage, name: str):
    """Return a safe path to a finished archive item, if it exists."""
    return storage.path_for(name)


def next_segment_boundary(now: datetime, segment_seconds: float = 3600.0) -> datetime:
    """Return the next local wall-clock boundary for a segment interval."""
    if segment_seconds == 3600:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    epoch = now.timestamp()
    boundary = (int(epoch // segment_seconds) + 1) * segment_seconds
    return datetime.fromtimestamp(boundary)


class ArchiveService:
    def __init__(self, settings, storage: Optional[VideoStorage] = None, receiver=None):
        self.settings = settings
        self.storage = storage or VideoStorage(
            settings.recordings_dir, settings.temp_dir,
            settings.min_free_bytes, settings.ffprobe_bin,
        )
        self.receiver = receiver if receiver is not None else build_receiver(settings)
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_error = None
        self._started = False

    def start(self) -> None:
        self.storage.ensure_directories()
        if self.receiver is None:
            LOG.info("Источник видео не настроен; доступен только HTTP API")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="video-archive", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        if self._thread and self._thread.is_alive():
            LOG.warning("Поток архива не завершился за %.1f секунд", timeout)
        else:
            LOG.info("Сервис архива остановлен")

    @property
    def status(self) -> dict:
        if self.receiver is None:
            state = "disabled"
        elif self._stop_event.is_set() and (not self._thread or not self._thread.is_alive()):
            state = "stopped"
        elif self._thread and self._thread.is_alive():
            state = "degraded" if self._last_error else "running"
        else:
            state = "stopped"
        result = {
            "state": state,
            "source": self.settings.source_type,
            "last_error": self._last_error,
        }
        result["cameras"] = ([{
            "id": self.settings.camera_id,
            "state": state,
            "last_error": self._last_error,
        }] if self.receiver is not None else [])
        return result

    def _mark_error(self, error: Optional[str]) -> None:
        with self._lock:
            self._last_error = error

    def _on_segment_started(self, started: datetime) -> None:
        self._mark_error(None)
        LOG.info("Начата запись сегмента с %s", started.isoformat(timespec="seconds"))

    def _on_segment_completed(self, path: Path, started: datetime) -> None:
        destination = push(self.storage, path, started, self.settings.camera_id)
        self._mark_error(None)
        LOG.info("Сегмент сохранён: %s", destination.name)

    def _run(self) -> None:
        self._started = True
        if isinstance(self.receiver, InboxReceiver):
            self._run_inbox()
            return
        while not self._stop_event.is_set():
            try:
                get(
                    self.receiver,
                    self._stop_event, self._on_segment_started, self._on_segment_completed
                )
                if not self._stop_event.is_set():
                    raise SourceError("Источник завершился неожиданно")
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                message = "{}: {}".format(type(exc).__name__, exc)
                self._mark_error(message)
                LOG.error("Ошибка источника видео: %s", message)
                self._stop_event.wait(self.settings.retry_seconds)

    def _run_inbox(self) -> None:
        while not self._stop_event.is_set():
            try:
                path = self.receiver.next_file(self._stop_event)
                if path is None or self._stop_event.is_set():
                    continue
                LOG.info("Получен готовый файл из inbox: %s", path.name)
                destination = self.storage.import_file(path)
                self._mark_error(None)
                LOG.info("Готовый файл сохранён: %s", destination.name)
            except Exception as exc:
                message = "{}: {}".format(type(exc).__name__, exc)
                self._mark_error(message)
                LOG.error("Ошибка импорта файла из inbox: %s", message)
                self._stop_event.wait(self.settings.retry_seconds)


class MultiArchiveService:
    """Run several camera receivers while publishing into one shared archive."""

    def __init__(self, settings, storage: Optional[VideoStorage] = None):
        self.settings = settings
        self.storage = storage or VideoStorage(
            settings.recordings_dir, settings.temp_dir,
            settings.min_free_bytes, settings.ffprobe_bin,
        )
        self.services = []
        for camera in settings.cameras:
            camera_settings = replace(
                settings,
                source_type="ffmpeg",
                source_url=camera.url,
                camera_id=camera.camera_id,
                temp_dir=settings.temp_dir / camera.camera_id,
                cameras=(),
            )
            self.services.append((camera.camera_id, ArchiveService(camera_settings, self.storage)))

    def start(self) -> None:
        self.storage.ensure_directories()
        for _, service in self.services:
            service.start()

    def stop(self, timeout: float = 15.0) -> None:
        for _, service in self.services:
            service._stop_event.set()
        for _, service in self.services:
            service.stop(timeout=timeout)

    @property
    def status(self) -> dict:
        camera_statuses = []
        for camera_id, service in self.services:
            status = service.status
            camera_statuses.append({
                "id": camera_id,
                "state": status["state"],
                "last_error": status["last_error"],
            })

        states = [camera["state"] for camera in camera_statuses]
        if states and all(state == "running" for state in states):
            state = "running"
        elif states and all(state == "stopped" for state in states):
            state = "stopped"
        elif states and all(state == "disabled" for state in states):
            state = "disabled"
        else:
            state = "degraded"

        errors = [
            "{}: {}".format(camera["id"], camera["last_error"])
            for camera in camera_statuses if camera["last_error"]
        ]
        return {
            "state": state,
            "source": "ffmpeg",
            "last_error": "; ".join(errors) or None,
            "cameras": camera_statuses,
        }

