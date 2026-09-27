import json
import hashlib
from argparse import Namespace
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import install


class InstallerTests(unittest.TestCase):
    def test_permissions_are_scoped_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            rights = Path(directory) / "rights.toml"
            rights.write_text('[[rule]]\nuseruid = "other"\n"+" = ["cmd.help"]\n')
            changed = install.permission_change(rights, 12, None)
            parsed = tomllib.loads(changed.decode())
            self.assertEqual(parsed["rule"][1]["groupid"], 12)
            self.assertEqual(set(parsed["rule"][1]["+"]), install.PERMISSIONS)
            rights.write_bytes(changed)
            self.assertIsNone(install.permission_change(rights, 12, None))
            user = install.permission_change(rights, None, "uA0U7t4PBxdJ5TLnarsOHQh4/tY=")
            self.assertEqual(tomllib.loads(user.decode())["rule"][2]["useruid"],
                             "uA0U7t4PBxdJ5TLnarsOHQh4/tY=")

    def test_invalid_rights_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            rights = Path(directory) / "rights.toml"
            rights.write_text("not valid toml =")
            with self.assertRaises(tomllib.TOMLDecodeError):
                install.permission_change(rights, 12, None)

    def test_interactive_grant_requires_a_choice(self):
        args = Namespace(grant_group_id=None, grant_user_uid=None, no_grant=False)
        with patch.object(install.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["g", "42"]):
            self.assertEqual(install.choose_grant(args), (42, None))
        with patch.object(install.sys.stdin, "isatty", return_value=False):
            self.assertEqual(install.choose_grant(args), (None, None))

    def test_archive_reads_only_expected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "plugin.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("BiliAudio.dll", b"dll")
                package.writestr("extractor.py", b"script")
                package.writestr("TS3AudioBot.sha256", hashlib.sha256(b"bot").hexdigest() + "\n")
                package.writestr("../unwanted", b"ignore")
            self.assertEqual(install.archive_files(archive),
                             {"BiliAudio.dll": b"dll", "extractor.py": b"script",
                              "TS3AudioBot.sha256": (hashlib.sha256(b"bot").hexdigest() + "\n").encode()})

    def test_release_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = b"test zip bytes"
            name = "BiliAudio-stable-0.12.0.zip"
            manifest = f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode()
            metadata = json.dumps({"tag_name": "v0.1.0", "assets": [
                {"name": name, "browser_download_url": "https://example.invalid/zip"},
                {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/checksums"},
            ]}).encode()
            payloads = {"latest": metadata, "zip": archive, "checksums": manifest}

            def fake_download(url, destination, *_args):
                destination.write_bytes(payloads[url.rsplit("/", 1)[-1]])

            with patch.object(install, "download", side_effect=fake_download):
                archives = install.release_archives(["stable-0.12.0"], root)
                self.assertEqual(archives["stable-0.12.0"].read_bytes(), archive)
                payloads["zip"] = b"changed"
                with self.assertRaises(install.InstallError):
                    install.release_archives(["stable-0.12.0"], root)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is unavailable")
    def test_default_override_is_loaded_by_compose(self):
        for base_name, override_name in install.OVERRIDES.items():
            with self.subTest(base_name=base_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / base_name).write_text("services:\n  ts3audiobot:\n    image: alpine:3.20\n")
                (root / override_name).write_text(install.render_override("ts3audiobot", root / "data"))
                result = subprocess.run(["docker", "compose", "config", "--format", "json"],
                                        cwd=root, capture_output=True, text=True, check=True)
                services = json.loads(result.stdout)["services"]
                self.assertIn("bili-extractor", services)
                self.assertEqual(services["bili-extractor"]["network_mode"], "service:ts3audiobot")


if __name__ == "__main__":
    unittest.main()
