import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from config import Settings
from main import make_handler, parse_range_header
from storage import VideoStorage


class DummyArchive:
    status = {"state": "disabled", "source": "none", "last_error": None}


class VideoRouteTests(unittest.TestCase):
    def test_range_parser_supports_open_and_suffix_ranges(self):
        self.assertEqual(parse_range_header("bytes=5-", 10), (5, 9))
        self.assertEqual(parse_range_header("bytes=-3", 10), (7, 9))
        with self.assertRaises(ValueError):
            parse_range_header("bytes=0-1,3-4", 10)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.storage = VideoStorage(self.root / "recordings", self.root / "temp", min_free_bytes=0)
        self.storage.ensure_directories()
        self.name = "2026-10-15_09-00-00.mp4"
        (self.root / "recordings" / self.name).write_bytes(b"0123456789")
        server_type = type("TestServer", (ThreadingHTTPServer,), {"daemon_threads": True})
        self.server = server_type(("127.0.0.1", 0), make_handler(self.storage, DummyArchive()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_list_and_health_json(self):
        with urlopen(self.base + "/api/videos") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["videos"][0]["name"], self.name)
        with urlopen(self.base + "/api/health") as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertEqual(health["status"], "ok")

    def test_homepage_and_static_assets(self):
        with urlopen(self.base + "/") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIn("Видеоархив".encode("utf-8"), response.read())
        with urlopen(self.base + "/static/styles.css") as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
            self.assertIn(b"@media (max-width: 640px)", response.read())
        with urlopen(self.base + "/static/app.js") as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertIn(b"/api/videos", response.read())
        with urlopen(self.base + "/static/favicon.svg") as response:
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")

    def test_static_routes_do_not_expose_arbitrary_files(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/static/%2e%2e/main.py")
        self.assertEqual(caught.exception.code, 404)

    def test_full_video_response(self):
        with urlopen(self.base + "/api/videos/" + self.name) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "video/mp4")
            self.assertEqual(response.read(), b"0123456789")

    def test_range_response(self):
        request = Request(self.base + "/api/videos/" + self.name, headers={"Range": "bytes=2-5"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.read(), b"2345")

    def test_unsatisfiable_range_and_unsafe_name(self):
        request = Request(self.base + "/api/videos/" + self.name, headers={"Range": "bytes=99-"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 416)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/api/videos/%2e%2e%2fsecret.mp4")
        self.assertEqual(caught.exception.code, 400)

    def test_missing_video(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/api/videos/2026-10-15_10-00-00.mp4")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
