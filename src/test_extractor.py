import json
import sys
import unittest
from unittest.mock import patch

import extractor


class ExtractorListTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
