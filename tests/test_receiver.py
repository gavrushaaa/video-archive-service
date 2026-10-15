import tempfile
import threading
import unittest
from pathlib import Path

from receiver import FfmpegSegmentReceiver, FfmpegUrlSource, InboxReceiver


class ReceiverTests(unittest.TestCase):
    def test_segmenter_command_uses_calendar_aligned_segments(self):
        root = Path(tempfile.mkdtemp())
        receiver = FfmpegSegmentReceiver("ffmpeg", root, 3600, ["-i", "rtsp://example/stream"])
        command = receiver._command("0123456789ab", root / "list.csv")
        self.assertIn("-segment_atclocktime", command)
        self.assertIn("-segment_list_type", command)
        self.assertIn("-strftime", command)
        self.assertIn("3600", command)

    def test_network_source_has_read_timeout_for_reconnects(self):
        source = FfmpegUrlSource("rtsp://example/stream", "ffmpeg", Path(tempfile.mkdtemp()), 3600, 12)
        self.assertIn("-rw_timeout", source.input_args)
        timeout_index = source.input_args.index("-rw_timeout") + 1
        self.assertEqual(source.input_args[timeout_index], "12000000")

    def test_inbox_yields_ready_file(self):
        root = Path(tempfile.mkdtemp())
        inbox = root / "inbox"
        inbox.mkdir()
        video = inbox / "2026-10-15_09-00-00.mp4"
        video.write_bytes(b"video")
        Path(str(video) + ".ready").write_text("", encoding="utf-8")
        receiver = InboxReceiver(inbox, stability_seconds=10)
        result = receiver.next_file(threading.Event())
        self.assertEqual(result, video)

    def test_inbox_skips_non_archive_names(self):
        root = Path(tempfile.mkdtemp())
        inbox = root / "inbox"
        inbox.mkdir()
        (inbox / "camera-export.mp4").write_bytes(b"video")
        receiver = InboxReceiver(inbox, stability_seconds=0, poll_seconds=0.01)
        stop = threading.Event()
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        self.assertIsNone(receiver.next_file(stop))
        timer.join()


if __name__ == "__main__":
    unittest.main()
