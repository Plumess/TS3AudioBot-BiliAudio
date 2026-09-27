"""隔离 Docker Compose 项目中的安装、重复运行与失败回退实测。"""

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src/bin/Release/netcoreapp3.1/BiliAudio.dll"
BOT_DLL = ROOT / "src/lib/TS3AudioBot.dll"
YTDLP = Path(os.environ.get("BILI_TEST_YTDLP", ""))


def command(*args, cwd=None, success=True):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if success and result.returncode:
        raise RuntimeError(f"{args}: {result.stderr}\n{result.stdout}")
    return result


def archive(path, extractor):
    with zipfile.ZipFile(path, "w") as package:
        package.write(PLUGIN, "BiliAudio.dll")
        package.writestr("extractor.py", extractor)
        package.writestr("TS3AudioBot.sha256", hashlib.sha256(BOT_DLL.read_bytes()).hexdigest() + "\n")


def mismatched_archive(path):
    with zipfile.ZipFile(path, "w") as package:
        package.write(PLUGIN, "BiliAudio.dll")
        package.writestr("extractor.py", (ROOT / "src/extractor.py").read_bytes())
        package.writestr("TS3AudioBot.sha256", "0" * 64 + "\n")


def main():
    if not PLUGIN.is_file() or not BOT_DLL.is_file() or not YTDLP.is_file():
        raise RuntimeError("请先构建插件并设置 BILI_TEST_YTDLP 为官方 yt-dlp 路径")
    with tempfile.TemporaryDirectory(prefix="biliaudio-integration-") as workspace:
        root = Path(workspace)
        data = root / "data"
        data.mkdir()
        (data / "rights.toml").write_text('[[rule]]\nuseruid = "other"\n"+" = ["cmd.help"]\n')
        (root / "TS3AudioBot.dll").write_bytes(BOT_DLL.read_bytes())
        base = root / "docker-compose.yml"
        base.write_text("services:\n  ts3audiobot:\n    image: python:3.13-alpine\n"
                        "    command: [\"sleep\", \"300\"]\n"
                        "    volumes:\n      - ./data:/app/data\n"
                        "      - ./TS3AudioBot.dll:/app/TS3AudioBot.dll:ro\n")
        good = root / "good.zip"
        bad = root / "bad.zip"
        mismatch = root / "mismatch.zip"
        archive(good, (ROOT / "src/extractor.py").read_bytes())
        archive(bad, b'raise RuntimeError("intentional test failure")\n')
        mismatched_archive(mismatch)
        compose = ("docker", "compose", "-f", str(base))
        command(*compose, "up", "-d", "ts3audiobot", cwd=root)
        try:
            bot_id = command(*compose, "ps", "-q", "ts3audiobot", cwd=root).stdout.strip()
            common = (sys.executable, str(ROOT / "install.py"), "--compose", str(base),
                      "--variant", "stable-0.12.0", "--yt-dlp", str(YTDLP),
                      "--grant-group-id", "42")
            rejected = command(*common, "--archive", str(mismatch), cwd=root, success=False)
            assert rejected.returncode != 0 and not (root / "docker-compose.override.yml").exists(), \
                "版本不匹配时不应落盘"
            rejected_first = command(*common, "--archive", str(bad), cwd=root, success=False)
            assert rejected_first.returncode != 0 and not (root / "docker-compose.override.yml").exists(), \
                "首次启动失败应移除附加配置"
            assert not (data / "plugins/BiliAudio.dll").exists(), "首次失败未回退插件文件"
            assert len(tomllib.loads((data / "rights.toml").read_text())["rule"]) == 1, \
                "首次失败未回退权限规则"
            first = command(*common, "--archive", str(good), cwd=root)
            assert "安装完成" in first.stdout
            after = command("docker", "compose", "ps", "-q", "ts3audiobot", cwd=root).stdout.strip()
            assert bot_id == after, "安装过程重建了机器人"
            config = json.loads(command("docker", "compose", "config", "--format", "json", cwd=root).stdout)
            assert "bili-extractor" in config["services"], "常规启动未包含解析服务"
            second = command(*common, "--archive", str(good), cwd=root)
            assert "安装完成" in second.stdout
            rules = tomllib.loads((data / "rights.toml").read_text())["rule"]
            assert len(rules) == 2 and rules[1]["groupid"] == 42, "重复安装增加了权限规则"
            failed = command(*common, "--archive", str(bad), cwd=root, success=False)
            assert failed.returncode != 0, "损坏的解析服务未触发回退"
            assert (data / "plugins/bili-extractor.py").read_bytes() == (ROOT / "src/extractor.py").read_bytes(), "解析脚本未回退"
            sidecar = command("docker", "compose", "ps", "-q", "bili-extractor", cwd=root).stdout.strip()
            for _ in range(30):
                state = json.loads(command("docker", "inspect", sidecar).stdout)[0]["State"]
                if state.get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(1)
            else:
                raise AssertionError("回退后解析服务未恢复健康")
            print("安装、重复运行、权限范围、失败回退和后续 Compose 加载均通过")
        except Exception:
            result = command("docker", "compose", "logs", "--tail", "25", "bili-extractor", cwd=root, success=False)
            print(result.stdout + result.stderr, file=sys.stderr)
            raise
        finally:
            command("docker", "compose", "down", "--remove-orphans", cwd=root, success=False)


if __name__ == "__main__":
    main()
