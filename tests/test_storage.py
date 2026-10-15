import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from storage import StorageError, VideoStorage, format_recording_name, parse_recording_name


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.recordings = self.root / "recordings"
        self.temp = self.root / "temp"
        self.storage = VideoStorage(self.recordings, self.temp, min_free_bytes=0)

    def test_ensure_directories(self):
        self.storage.ensure_directories()
        self.assertTrue(self.recordings.is_dir())
        self.assertTrue(self.temp.is_dir())

    def test_filename_round_trip_and_camera_id(self):
        started = datetime(2026, 10, 15, 9, 37, 4)
        name = format_recording_name(started, "front-door")
        self.assertEqual(name, "2026-10-15_09-37-04__front-door.mp4")
        self.assertEqual(parse_recording_name(name), (started, "front-door"))
        self.assertIsNone(parse_recording_name("../2026-10-15_09-37-04.mp4"))

    def test_publish_and_prevent_overwrite(self):
        self.temp.mkdir()
        source = self.temp / "segment.mp4"
        source.write_bytes(b"first")
        started = datetime(2026, 10, 15, 9, 37)
        path = self.storage.publish_segment(source, started)
        self.assertEqual(path.name, "2026-10-15_09-37-00.mp4")
        self.assertEqual(path.read_bytes(), b"first")
        self.assertFalse(source.exists())

        duplicate = self.temp / "duplicate.mp4"
        duplicate.write_bytes(b"second")
        with self.assertRaises(StorageError):
            self.storage.publish_segment(duplicate, started)
        self.assertEqual(path.read_bytes(), b"first")

    def test_empty_segment_is_not_published(self):
        self.temp.mkdir()
        empty = self.temp / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(StorageError):
            self.storage.publish_segment(empty, datetime(2026, 10, 15, 9))
        self.assertFalse((self.recordings / "2026-10-15_09-00-00.mp4").exists())

    def test_list_is_newest_first_and_ignores_other_files(self):
        self.recordings.mkdir()
        (self.recordings / "2026-10-15_09-00-00.mp4").write_bytes(b"a")
        (self.recordings / "2026-10-15_11-00-00.mp4").write_bytes(b"bbb")
        (self.recordings / "unfinished.mp4").write_bytes(b"ignore")
        results = self.storage.list_recordings()
        self.assertEqual([item["name"] for item in results], [
            "2026-10-15_11-00-00.mp4", "2026-10-15_09-00-00.mp4"
        ])
        self.assertIsNone(results[0]["duration_seconds"])

    def test_path_for_rejects_traversal_and_invalid_calendar_dates(self):
        self.recordings.mkdir()
        self.assertIsNone(self.storage.path_for("../secret.mp4"))
        self.assertIsNone(self.storage.path_for("2026-99-15_09-00-00.mp4"))

    def test_import_ready_inbox_file(self):
        self.root.joinpath("inbox").mkdir()
        incoming = self.root / "inbox" / "2026-10-15_10-00-00.mp4"
        incoming.write_bytes(b"video")
        Path(str(incoming) + ".ready").write_text("", encoding="utf-8")
        final = self.storage.import_file(incoming)
        self.assertEqual(final.read_bytes(), b"video")
        self.assertFalse(incoming.exists())
        self.assertFalse(Path(str(incoming) + ".ready").exists())


if __name__ == "__main__":
    unittest.main()
