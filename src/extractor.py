"""Small, optional bridge from the legacy bot to the official yt-dlp release."""

import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


SLOTS = threading.BoundedSemaphore(2)
LIST_LIMIT = 100


def yt_dlp_command(binary):
    path = shutil.which(binary) or binary
    return [sys.executable, path] if zipfile.is_zipfile(path) else [path]


YTDLP_COMMAND = yt_dlp_command(os.environ.get("BILI_YTDLP_BIN", "yt-dlp"))


def valid_video(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and
            ((parsed.hostname in ("www.bilibili.com", "bilibili.com", "m.bilibili.com") and
              parsed.path.startswith("/video/")) or parsed.hostname == "b23.tv"))


def valid_list(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        return False
    path = parsed.path
    if parsed.hostname == "space.bilibili.com":
        parts = path.strip("/").split("/")
        return (len(parts) >= 3 and parts[0].isdigit() and
                (parts[1] == "lists" and parts[2].isdigit() or
                 parts[1] == "channel" and parts[2] in ("collectiondetail", "seriesdetail") or
                 parts[1] == "favlist")) or (len(parts) == 2 and parts[0].isdigit() and parts[1] == "favlist")
    return parsed.hostname in ("www.bilibili.com", "bilibili.com") and (
        path.startswith(("/list/", "/medialist/detail/ml", "/medialist/play/", "/video/")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/resolve", "/list"):
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                return self.reply(400, {"error": "invalid request size"})
            url = json.loads(self.rfile.read(length))["url"]
            if not isinstance(url, str) or not (valid_video(url) if self.path == "/resolve" else valid_list(url)):
                return self.reply(400, {"error": "invalid Bilibili URL"})
        except (ValueError, TypeError, KeyError, AttributeError):
            return self.reply(400, {"error": "invalid request"})

        if not SLOTS.acquire(blocking=False):
            return self.reply(503, {"error": "extractor busy"})
        try:
            if self.path == "/list":
                return self.list_entries(url)
            result = subprocess.run(
                [*YTDLP_COMMAND, "--no-cache-dir", "--no-playlist", "--no-warnings", "--skip-download",
                 "--dump-json", "--format", "bestaudio", "--", url],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode:
                return self.reply(422, {"error": result.stderr.strip()[-300:] or "video unavailable"})
            if len(result.stdout) > 4 * 1024 * 1024:
                return self.reply(422, {"error": "metadata too large"})
            info = json.loads(result.stdout)
            audio_url = info["url"]
            if urlsplit(audio_url).scheme != "https":
                return self.reply(422, {"error": "invalid audio URL"})
            headers = {
                key: value for key, value in info.get("http_headers", {}).items()
                if key.lower() in ("user-agent", "referer") and isinstance(value, str)
            }
            self.reply(200, {"url": audio_url, "title": info.get("title") or "Bilibili video", "http_headers": headers})
        except subprocess.TimeoutExpired:
            self.reply(504, {"error": "video extraction timed out"})
        except (ValueError, KeyError, TypeError):
            self.reply(422, {"error": "invalid video metadata"})
        finally:
            SLOTS.release()

    def list_entries(self, url):
        result = subprocess.run(
            [*YTDLP_COMMAND, "--no-cache-dir", "--yes-playlist", "--flat-playlist",
             "--playlist-end", str(LIST_LIMIT), "--dump-single-json", "--no-warnings",
             "--", url],
            capture_output=True, text=True, timeout=45, check=False,
        )
        if result.returncode:
            return self.reply(422, {"error": result.stderr.strip()[-300:] or "playlist unavailable"})
        if len(result.stdout) > 1024 * 1024:
            return self.reply(422, {"error": "playlist metadata too large"})
        info = json.loads(result.stdout)
        if info.get("_type") != "playlist":
            return self.reply(422, {"error": "not a playlist"})
        entries = []
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            video_url = entry.get("url")
            if isinstance(video_url, str) and valid_video(video_url):
                entries.append({"url": video_url, "title": entry.get("title") or entry.get("id") or "Bilibili video"})
        self.reply(200, {"title": info.get("title") or "Bilibili 列表", "entries": entries,
                         "limit": LIST_LIMIT, "truncated": len(entries) == LIST_LIMIT})


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("BILI_LISTEN_HOST", "127.0.0.1"), 18944), Handler).serve_forever()
