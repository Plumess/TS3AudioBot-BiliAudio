import io
import json
import sys
import unittest
from unittest.mock import patch

import extractor


class ExtractorListTests(unittest.TestCase):
    @staticmethod
    def handler(path, url=None):
        handler = object.__new__(extractor.Handler)
        handler.path = path
        replies = []
        handler.reply = lambda status, payload: replies.append((status, payload))
        if url is not None:
            body = json.dumps({"url": url}).encode("utf-8")
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = io.BytesIO(body)
        return handler, replies

    def test_official_zipapp_uses_current_python(self):
        with patch.object(extractor.shutil, "which", return_value="/tmp/yt-dlp"), \
                patch.object(extractor.zipfile, "is_zipfile", return_value=True):
            self.assertEqual(extractor.yt_dlp_command("yt-dlp"), [sys.executable, "/tmp/yt-dlp"])

    def test_supported_list_links(self):
        links = (
            "https://space.bilibili.com/84912/favlist?fid=1103407912",
            "https://www.bilibili.com/medialist/detail/ml1103407912",
            "https://space.bilibili.com/2142762/lists/3662502?type=season",
            "https://space.bilibili.com/1958703906/lists/547718?type=series",
            "https://www.bilibili.com/list/1958703906?sid=547718",
            "https://www.bilibili.com/video/BV1iy411i7ab",
        )
        for link in links:
            with self.subTest(link=link):
                self.assertTrue(extractor.valid_list(link))

    def test_reject_external_links(self):
        self.assertFalse(extractor.valid_list("https://bilibili.com.evil.example/list/123"))
        self.assertFalse(extractor.valid_list("http://www.bilibili.com/list/123"))

    def test_flat_playlist_filters_entries(self):
        handler = object.__new__(extractor.Handler)
        replies = []
        handler.reply = lambda status, payload: replies.append((status, payload))
        metadata = {
            "_type": "playlist", "title": "Test", "entries": [
                {"url": "https://www.bilibili.com/video/BV1iy411i7ab", "id": "BV1iy411i7ab"},
                {"url": "https://evil.example/video/BV1iy411i7ab"},
            ],
        }
        completed = type("Result", (), {"returncode": 0, "stdout": json.dumps(metadata), "stderr": ""})()
        with patch.object(extractor.subprocess, "run", return_value=completed) as run:
            handler.list_entries("https://www.bilibili.com/list/123")
        self.assertIn("--playlist-end", run.call_args.args[0])
        self.assertEqual(replies[0][0], 200)
        self.assertEqual(len(replies[0][1]["entries"]), 1)

    def test_health_checks_yt_dlp(self):
        handler, replies = self.handler("/health")
        with patch.object(extractor, "yt_dlp_available", return_value=False):
            handler.do_GET()
        self.assertEqual(replies[0][0], 503)
        self.assertFalse(replies[0][1]["ok"])
        with patch.object(extractor, "yt_dlp_available", return_value=True):
            handler.do_GET()
        self.assertEqual(replies[1], (200, {"ok": True}))

    def test_missing_yt_dlp_returns_error_for_video_and_list(self):
        requests = (
            ("/resolve", "https://www.bilibili.com/video/BV1iy411i7ab"),
            ("/list", "https://www.bilibili.com/list/1958703906?sid=547718"),
        )
        with patch.object(extractor.subprocess, "run", side_effect=FileNotFoundError):
            for path, url in requests:
                with self.subTest(path=path):
                    handler, replies = self.handler(path, url)
                    handler.do_POST()
                    self.assertEqual(replies[0][0], 503)
                    self.assertIn("yt-dlp", replies[0][1]["error"])


if __name__ == "__main__":
    unittest.main()
