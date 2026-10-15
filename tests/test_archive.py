import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from archive import ArchiveService, next_segment_boundary
from config import Settings


class ArchiveTests(unittest.TestCase):
    def test_hourly_boundaries_mid_hour_and_exact_hour(self):
        self.assertEqual(
            next_segment_boundary(datetime(2026, 10, 15, 9, 37, 22)),
            datetime(2026, 10, 15, 10, 0),
        )
        self.assertEqual(
            next_segment_boundary(datetime(2026, 10, 15, 10, 0)),
            datetime(2026, 10, 15, 11, 0),
        )

    def test_configurable_interval_boundary(self):
        boundary = next_segment_boundary(datetime(2026, 10, 15, 9, 37, 22), 10)
        self.assertGreater(boundary, datetime(2026, 10, 15, 9, 37, 22))
        self.assertLessEqual((boundary - datetime(2026, 10, 15, 9, 37, 22)).total_seconds(), 10)

    def test_completed_segment_is_published_with_actual_start(self):
        root = Path(tempfile.mkdtemp())
        settings = Settings(
            recordings_dir=root / "recordings", temp_dir=root / "temp",
            min_free_bytes=0, source_type="none", camera_id="testcam",
        )
        service = ArchiveService(settings)
        temp_file = root / "temporary.mp4"
        temp_file.write_bytes(b"test video payload")
        service._on_segment_completed(temp_file, datetime(2026, 10, 15, 9, 37, 5))
        final = root / "recordings" / "2026-10-15_09-37-05__testcam.mp4"
        self.assertEqual(final.read_bytes(), b"test video payload")
        self.assertFalse(temp_file.exists())

    def test_reconnect_after_source_error_and_stop_cleanly(self):
        class FlakyReceiver:
            def __init__(self):
                self.attempts = 0
                self.recovered = threading.Event()

            def run(self, stop_event, on_started, on_completed):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("simulated lost connection")
                self.recovered.set()
                stop_event.wait(2)

        root = Path(tempfile.mkdtemp())
        receiver = FlakyReceiver()
        settings = Settings(
            recordings_dir=root / "recordings", temp_dir=root / "temp",
            min_free_bytes=0, retry_seconds=0.01, source_type="none",
        )
        service = ArchiveService(settings, receiver=receiver)
        service.start()
        self.assertTrue(receiver.recovered.wait(2))
        service.stop(timeout=2)
        self.assertGreaterEqual(receiver.attempts, 2)
        self.assertEqual(service.status["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
