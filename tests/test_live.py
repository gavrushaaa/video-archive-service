import unittest

from config import CameraSource, Settings
from live import LivePreviewManager


class LivePreviewManagerTests(unittest.TestCase):
    def test_multi_camera_settings_are_available_for_live_view(self):
        settings = Settings(cameras=(
            CameraSource("entrance", "rtsp://camera.local/entrance"),
            CameraSource("yard", "http://camera.local/yard"),
        ))
        manager = LivePreviewManager(settings)

        self.assertEqual(manager.list_sources(), [
            {"id": "entrance", "label": "entrance"},
            {"id": "yard", "label": "yard"},
        ])
        command = manager._command(manager._sources["entrance"])
        self.assertIn("-rtsp_transport", command)
        self.assertIn("rtsp://camera.local/entrance", command)

    def test_inbox_mode_has_no_live_source(self):
        manager = LivePreviewManager(Settings(source_type="inbox"))
        self.assertEqual(manager.list_sources(), [])


if __name__ == "__main__":
    unittest.main()
