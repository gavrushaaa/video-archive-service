"""Extensible video input implementations.

FFmpeg performs segmentation in one process so the input connection remains
open across calendar boundaries. InboxReceiver handles cameras that deliver
already-segmented MP4 files.
"""

import csv
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

from storage import parse_recording_name


LOG = logging.getLogger(__name__)
_TEMP_SEGMENT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})__(?P<run>[0-9a-f]{12})\.mp4$"
)


class SourceError(Exception):
    """Raised when an input source cannot provide video."""


class FfmpegSegmentReceiver:
    def __init__(self, ffmpeg_bin: str, temp_dir: Path, segment_seconds: float,
                 input_args: List[str], output_args: Optional[List[str]] = None):
        self.ffmpeg_bin = ffmpeg_bin
        self.temp_dir = Path(temp_dir).resolve()
        self.segment_seconds = segment_seconds
        self.input_args = input_args
        self.output_args = output_args or ["-map", "0", "-c", "copy"]

    def _command(self, run_id: str, list_path: Path) -> List[str]:
        pattern = str(self.temp_dir / ("%Y-%m-%d_%H-%M-%S__{}.mp4".format(run_id)))
        return [
            self.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
            *self.input_args,
            *self.output_args,
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-segment_atclocktime", "1",
            "-segment_time_delta", "0.05",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-segment_list", str(list_path),
            "-segment_list_type", "csv",
            "-segment_list_size", "0",
            "-segment_format", "mp4",
            pattern,
        ]

    @staticmethod
    def _read_completed(list_path: Path, temp_dir: Path, run_id: str) -> List[Tuple[Path, datetime]]:
        if not list_path.exists():
            return []
        results = []
        try:
            with list_path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.reader(handle):
                    if not row:
                        continue
                    path = Path(row[0])
                    if not path.is_absolute():
                        path = temp_dir / path
                    try:
                        path = path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if path.parent != temp_dir or run_id not in path.name:
                        continue
                    match = _TEMP_SEGMENT_RE.fullmatch(path.name)
                    if not match or not path.is_file() or path.stat().st_size == 0:
                        continue
                    try:
                        started = datetime.strptime(
                            match.group("date") + "_" + match.group("time"),
                            "%Y-%m-%d_%H-%M-%S",
                        )
                    except ValueError:
                        continue
                    results.append((path, started))
        except (OSError, csv.Error, UnicodeError):
            return []
        return results

    def run(self, stop_event: threading.Event,
            on_started: Callable[[datetime], None],
            on_completed: Callable[[Path, datetime], None]) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:12]
        list_path = self.temp_dir / ("segments-{}.csv".format(run_id))
        command = self._command(run_id, list_path)
        delivered: Set[str] = set()
        announced: Set[str] = set()
        process = None

        def drain_completed():
            for path, started in self._read_completed(list_path, self.temp_dir, run_id):
                key = str(path)
                if key in delivered:
                    continue
                on_completed(path, started)
                delivered.add(key)

        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            LOG.info("Источник FFmpeg запущен; идентификатор процесса записи: %s", run_id)
            while process.poll() is None and not stop_event.wait(0.25):
                for path in self.temp_dir.glob("*__{}.mp4".format(run_id)):
                    match = _TEMP_SEGMENT_RE.fullmatch(path.name)
                    if match and path.name not in announced:
                        try:
                            started = datetime.strptime(
                                match.group("date") + "_" + match.group("time"),
                                "%Y-%m-%d_%H-%M-%S",
                            )
                        except ValueError:
                            continue
                        on_started(started)
                        announced.add(path.name)
                drain_completed()

            if stop_event.is_set() and process.poll() is None:
                try:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                except (OSError, AttributeError):
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            drain_completed()
            return_code = process.wait()
            if not stop_event.is_set():
                raise SourceError("FFmpeg завершился (код {})".format(return_code))
        except OSError as exc:
            raise SourceError("Не удалось запустить FFmpeg: {}".format(exc)) from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for path in self.temp_dir.glob("*__{}.mp4".format(run_id)):
                if str(path) not in delivered:
                    try:
                        path.unlink()
                    except OSError:
                        LOG.warning("Не удалось удалить незавершённый сегмент: %s", path)
            try:
                list_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOG.warning("Не удалось удалить список сегментов: %s", list_path)


class FfmpegUrlSource(FfmpegSegmentReceiver):
    def __init__(self, url: str, ffmpeg_bin: str, temp_dir: Path, segment_seconds: float,
                 timeout_seconds: float = 15.0):
        input_args = []
        if url.lower().startswith("rtsp://"):
            input_args.extend(["-rtsp_transport", "tcp"])
        input_args.extend(["-rw_timeout", str(int(timeout_seconds * 1000000))])
        input_args.extend(["-i", url])
        super().__init__(ffmpeg_bin, temp_dir, segment_seconds, input_args)


class SyntheticSource(FfmpegSegmentReceiver):
    """Generate a local test pattern; requires FFmpeg built with libx264/AAC."""

    def __init__(self, ffmpeg_bin: str, temp_dir: Path, segment_seconds: float):
        input_args = [
            "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
            "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        ]
        output_args = [
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
            "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-c:a", "aac",
        ]
        super().__init__(ffmpeg_bin, temp_dir, segment_seconds, input_args, output_args)


class InboxReceiver:
    """Yield stable, already-finished MP4 files from an inbox directory."""

    def __init__(self, inbox_dir: Path, stability_seconds: float = 3.0,
                 poll_seconds: float = 0.5):
        self.inbox_dir = Path(inbox_dir).resolve()
        self.stability_seconds = stability_seconds
        self.poll_seconds = poll_seconds
        self._observed = {}
        self._warned = set()

    def next_file(self, stop_event: threading.Event) -> Optional[Path]:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        while not stop_event.is_set():
            now = time.monotonic()
            candidates = []
            current = set()
            try:
                entries = list(self.inbox_dir.iterdir())
            except OSError as exc:
                LOG.error("Не удалось прочитать inbox: %s", exc)
                stop_event.wait(self.poll_seconds)
                continue
            for path in entries:
                if not path.is_file() or path.name.endswith(".ready") or path.name.startswith("."):
                    continue
                if path.suffix.lower() != ".mp4":
                    continue
                if not parse_recording_name(path.name):
                    if path.name not in self._warned:
                        LOG.warning("Файл inbox пропущен: имя не соответствует формату архива: %s", path.name)
                        self._warned.add(path.name)
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                current.add(path.name)
                signature = (stat.st_size, stat.st_mtime_ns)
                ready_marker = Path(str(path) + ".ready").is_file()
                previous = self._observed.get(path.name)
                if previous and previous[0] == signature:
                    first_seen = previous[1]
                else:
                    first_seen = now
                self._observed[path.name] = (signature, first_seen)
                if ready_marker or now - first_seen >= self.stability_seconds:
                    candidates.append(path)
            for stale_name in set(self._observed) - current:
                self._observed.pop(stale_name, None)
            if candidates:
                candidates.sort(key=lambda item: item.name)
                return candidates[0]
            stop_event.wait(self.poll_seconds)
        return None


def build_receiver(settings):
    if settings.source_type == "none":
        return None
    if settings.source_type == "inbox":
        return InboxReceiver(settings.inbox_dir, settings.inbox_stability_seconds)
    if settings.source_type == "synthetic":
        return SyntheticSource(settings.ffmpeg_bin, settings.temp_dir, settings.segment_seconds)
    if settings.source_type == "ffmpeg":
        return FfmpegUrlSource(
            settings.source_url, settings.ffmpeg_bin, settings.temp_dir,
            settings.segment_seconds, settings.source_timeout_seconds,
        )
    raise ValueError("Неизвестный тип источника: {}".format(settings.source_type))

