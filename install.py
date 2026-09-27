#!/usr/bin/env python3
"""为已有的 Docker Compose TS3AudioBot 安装可选 BiliAudio 插件。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.request import Request, urlopen
import zipfile


REPO = "Plumess/TS3AudioBot-BiliAudio"
MARKER = "# BiliAudio installer managed file; do not edit by hand.\n"
OVERRIDES = {
    "compose.yaml": "compose.override.yaml",
    "compose.yml": "compose.override.yml",
    "docker-compose.yaml": "docker-compose.override.yaml",
    "docker-compose.yml": "docker-compose.override.yml",
}
PERMISSIONS = {"cmd.bili.*", "cmd.bplay"}


class InstallError(Exception):
    pass


def run(*args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise InstallError(f"{' '.join(map(str, args))}: {result.stderr.strip() or result.stdout.strip()}")
    return result


def download(url, destination, limit=40 * 1024 * 1024):
    request = Request(url, headers={"User-Agent": "BiliAudio-installer"})
    with urlopen(request, timeout=30) as response, destination.open("wb") as output:
        size = 0
        while block := response.read(64 * 1024):
            size += len(block)
            if size > limit:
                raise InstallError("下载文件超过大小限制")
            output.write(block)


def release_archives(variants, directory):
    metadata = directory / "release.json"
    download(f"https://api.github.com/repos/{REPO}/releases/latest", metadata, 1024 * 1024)
    release = json.loads(metadata.read_text())
    assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
    if "SHA256SUMS" not in assets:
        raise InstallError(f"Release {release['tag_name']} 缺少校验清单")
    checksums = directory / "SHA256SUMS"
    download(assets["SHA256SUMS"], checksums, 16 * 1024)
    expected = {}
    for line in checksums.read_text().splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(\S+)", line)
        if match:
            expected[match[2]] = match[1].lower()
    archives = {}
    for variant in variants:
        name = f"BiliAudio-{variant}.zip"
        if name not in assets:
            raise InstallError(f"Release {release['tag_name']} 缺少 {name}")
        archive = directory / name
        download(assets[name], archive)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected.get(name) != actual:
            raise InstallError(f"{name} SHA256 校验失败")
        archives[variant] = archive
    print(f"Release {release['tag_name']}：下载文件校验通过")
    return archives


def archive_files(archive):
    with zipfile.ZipFile(archive) as package:
        files = {}
        for name, limit in (("BiliAudio.dll", 8 * 1024 * 1024),
                            ("extractor.py", 1024 * 1024), ("TS3AudioBot.sha256", 128)):
            info = package.getinfo(name)
            if info.file_size > limit:
                raise InstallError(f"{name} 超过大小限制")
            files[name] = package.read(name)
        if not re.fullmatch(rb"[a-fA-F0-9]{64}\n?", files["TS3AudioBot.sha256"]):
            raise InstallError("ZIP 中的机器人 DLL 指纹无效")
        return files


def compose_state(base, service):
    cwd = base.parent
    config = json.loads(run("docker", "compose", "-f", str(base), "config", "--format", "json", cwd=cwd).stdout)
    if service not in config["services"]:
        raise InstallError(f"Compose 中不存在服务 {service}")
    container = run("docker", "compose", "-f", str(base), "ps", "-q", service, cwd=cwd).stdout.strip()
    if not container:
        raise InstallError("机器人服务必须先运行，才能核对数据挂载目录")
    info = json.loads(run("docker", "inspect", container).stdout)[0]
    if not info["State"]["Running"]:
        raise InstallError("机器人容器没有运行")
    return config["services"][service], container, info


def unknown_sidecar(info):
    project = info.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
    if not project:
        return False
    result = run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=bili-extractor")
    return bool(result.stdout.strip())


def data_directory(info, supplied):
    mounts = [mount for mount in info["Mounts"] if mount["Type"] == "bind"]
    if supplied:
        data = supplied.resolve()
        if not any(Path(mount["Source"]).resolve() == data for mount in mounts):
            raise InstallError("--data-dir 不是该机器人容器的 bind mount 来源目录")
        return data
    candidates = [mount for mount in mounts if mount["Destination"] in ("/app/data", "/data")]
    if len(candidates) != 1:
        raise InstallError("无法唯一识别数据目录；请用 --data-dir 指定已有的 bind mount")
    return Path(candidates[0]["Source"]).resolve()


def running_bot_hash(container, path, directory):
    if not path.startswith("/") or ".." in Path(path).parts:
        raise InstallError("--bot-dll 必须是容器内的绝对文件路径")
    destination = directory / "running-TS3AudioBot.dll"
    run("docker", "cp", f"{container}:{path}", str(destination))
    if not destination.is_file():
        raise InstallError("找不到运行中机器人的 TS3AudioBot.dll")
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def render_override(service, data):
    plugins = data / "plugins"
    q = json.dumps
    return (MARKER + "services:\n  bili-extractor:\n"
            "    image: python:3.13-alpine\n    restart: unless-stopped\n"
            "    command: [\"python\", \"/app/extractor.py\"]\n"
            f"    network_mode: {q('service:' + service)}\n"
            "    read_only: true\n"
            "    environment:\n      PYTHONDONTWRITEBYTECODE: '1'\n"
            "      BILI_YTDLP_BIN: /app/yt-dlp\n"
            "    tmpfs:\n      - /tmp:size=32m,mode=1777\n"
            "    healthcheck:\n"
            "      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:18944/health', timeout=2).read()\"]\n"
            "      interval: 10s\n      timeout: 3s\n      retries: 3\n      start_period: 5s\n"
            "    volumes:\n"
            f"      - {q(str(plugins / 'bili-extractor.py') + ':/app/extractor.py:ro')}\n"
            f"      - {q(str(plugins / 'tools' / 'yt-dlp') + ':/app/yt-dlp:ro')}\n")


def permission_change(rights, group_id, user_uid):
    if not group_id and not user_uid:
        return None
    if not rights.is_file():
        raise InstallError(f"找不到权限文件 {rights}；不会创建可能放开权限的新文件")
    original = rights.read_bytes()
    parsed = tomllib.loads(original.decode("utf-8"))
    key = "groupid" if group_id else "useruid"
    value = group_id if group_id else user_uid
    for rule in parsed.get("rule", []):
        matcher = rule.get(key)
        grants = rule.get("+", [])
        if (matcher == value or isinstance(matcher, list) and value in matcher) and \
                PERMISSIONS.issubset(set(grants if isinstance(grants, list) else [grants])):
            return None
    # 追加独立顶层规则，不改写原有注释和规则。
    fragment = ("\n[[rule]]\n" + f"{key} = {json.dumps(value)}\n" +
                '"+" = ["cmd.bili.*", "cmd.bplay"]\n')
    updated = original + (b"" if original.endswith(b"\n") else b"\n") + fragment.encode()
    tomllib.loads(updated.decode("utf-8"))
    return updated


def write_atomic(path, content, mode, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".biliaudio-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.chmod(temporary, mode)
        if owner is None and path.exists():
            current = path.stat()
            owner = (current.st_uid, current.st_gid)
        if owner is not None:
            current = os.stat(temporary)
            if (current.st_uid, current.st_gid) != owner:
                os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wait_healthy(base, override, service, bot_id, recreate):
    command = ("docker", "compose", "-f", str(base), "-f", str(override))
    existing = run(*command, "ps", "-q", "bili-extractor", cwd=base.parent).stdout.strip()
    up = (*command, "up", "-d")
    if existing and recreate:
        # 单文件 bind mount 会保留旧 inode；更新脚本后必须重建容器。
        up += ("--force-recreate",)
    run(*up, "bili-extractor", cwd=base.parent)
    actual_bot = run("docker", "compose", "-f", str(base), "ps", "-q", service, cwd=base.parent).stdout.strip()
    if actual_bot != bot_id:
        raise InstallError("机器人容器在安装期间发生变化")
    current = run(*command, "ps", "-q", "bili-extractor", cwd=base.parent).stdout.strip()
    if not current:
        raise InstallError("解析服务没有启动")
    for _ in range(45):
        info = json.loads(run("docker", "inspect", current).stdout)[0]
        if info["State"].get("Health", {}).get("Status") == "healthy":
            return
        if info["State"].get("Health", {}).get("Status") == "unhealthy" or not info["State"]["Running"]:
            checks = info["State"].get("Health", {}).get("Log", [])
            detail = checks[-1].get("Output", "").strip()[-300:] if checks else ""
            raise InstallError(f"解析服务未通过健康检查（退出码 {info['State'].get('ExitCode')}）：{detail}")
        time.sleep(1)
    raise InstallError("解析服务健康检查超时")


def choose_grant(args):
    if args.grant_group_id or args.grant_user_uid or args.no_grant or not sys.stdin.isatty():
        return args.grant_group_id, args.grant_user_uid
    print("授权范围：默认不修改 rights.toml。可按服务器组 ID 或用户 UID 授权。")
    choice = input("选择 [回车跳过/g 服务器组/u 用户 UID]: ").strip().lower()
    if choice == "g":
        return int(input("TeamSpeak 服务器组 ID: ").strip()), None
    if choice == "u":
        return None, input("TeamSpeak 用户 UID: ").strip()
    if choice:
        raise InstallError("未知的授权选项")
    return None, None


def install(args):
    if args.compose is None:
        found = [Path(name) for name in OVERRIDES if Path(name).is_file()]
        if len(found) != 1:
            raise InstallError("请用 --compose 指定机器人主 Compose 文件")
        args.compose = found[0]
    base = args.compose.resolve()
    if not base.is_file() or base.name not in OVERRIDES:
        raise InstallError("请指定已有的 compose.yaml/compose.yml/docker-compose.yml 文件")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.service):
        raise InstallError("机器人服务名无效")
    _, bot_id, info = compose_state(base, args.service)
    data = data_directory(info, args.data_dir)
    if not data.is_dir():
        raise InstallError(f"数据目录不存在：{data}")
    settings = data / "ts3audiobot.toml"
    if settings.is_file():
        config = tomllib.loads(settings.read_text())
        if config.get("plugins", {}).get("path", "plugins") != "plugins":
            raise InstallError("机器人使用自定义插件目录，目前不支持自动安装")
        rights_path = config.get("rights", {}).get("path", "rights.toml")
        if Path(rights_path).is_absolute():
            raise InstallError("机器人使用绝对权限文件路径，目前不支持自动授权")
    else:
        rights_path = "rights.toml"
    rights = (data / rights_path).resolve()
    if not rights.is_relative_to(data):
        raise InstallError("权限文件不在机器人数据目录内")
    override = base.with_name(OVERRIDES[base.name])
    if override.exists() and not override.read_text().startswith(MARKER):
        raise InstallError(f"已有用户维护的 {override.name}；安装器不会覆盖它")
    if not override.exists() and unknown_sidecar(info):
        raise InstallError("已发现手工部署的 bili-extractor；请先迁移或停用旧附加 Compose 配置")
    group_id, user_uid = choose_grant(args)
    if group_id is not None and group_id <= 0:
        raise InstallError("服务器组 ID 必须大于零")
    if user_uid is not None and not re.fullmatch(r"[A-Za-z0-9+/=]{8,80}", user_uid):
        raise InstallError("用户 UID 格式无效")
    grant = permission_change(rights, group_id, user_uid)
    with tempfile.TemporaryDirectory(prefix="biliaudio-install-") as workspace:
        staging = Path(workspace)
        bot_hash = running_bot_hash(bot_id, args.bot_dll, staging)
        if args.archive:
            if not args.variant:
                raise InstallError("使用本地 ZIP 时请指定 --variant")
            archives = {args.variant: args.archive}
        else:
            variants = [args.variant] if args.variant else ["stable-0.12.0", "master-nightly"]
            archives = release_archives(variants, staging)
        matches = []
        for candidate, archive in archives.items():
            content = archive_files(archive)
            if content["TS3AudioBot.sha256"].decode().strip().lower() == bot_hash:
                matches.append((candidate, content))
        if len(matches) != 1:
            raise InstallError("没有与运行中 TS3AudioBot.dll 完全匹配的插件 ZIP；未改动现有文件")
        variant, files = matches[0]
        yt_dlp = args.yt_dlp or staging / "yt-dlp"
        if not args.yt_dlp:
            download("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", yt_dlp)
        if not zipfile.is_zipfile(yt_dlp):
            raise InstallError("yt-dlp 不是官方 Python zipapp 发布文件")
        run(sys.executable, str(yt_dlp), "--version")
        plugins = data / "plugins"
        changes = {
            plugins / "BiliAudio.dll": (files["BiliAudio.dll"], 0o644),
            plugins / "bili-extractor.py": (files["extractor.py"], 0o644),
            plugins / "tools" / "yt-dlp": (yt_dlp.read_bytes(), 0o755),
            override: (render_override(args.service, data).encode(), 0o644),
        }
        if grant is not None:
            changes[rights] = (grant, rights.stat().st_mode & 0o777)
        if any(path.is_symlink() for path in changes):
            raise InstallError("目标包含符号链接，安装器不会覆盖")
        # 先让 Compose 解析将要写入的文件，避免部署后才发现配置无效。
        preview = staging / override.name
        preview.write_bytes(changes[override][0])
        run("docker", "compose", "-f", str(base), "-f", str(preview), "config", "--quiet", cwd=base.parent)
        changed = {path: item for path, item in changes.items()
                   if not path.is_file() or path.read_bytes() != item[0]}
        plugin_was_present = (plugins / "BiliAudio.dll").exists()
        plugin_changed = plugins / "BiliAudio.dll" in changed
        recreate_extractor = any(path in changed for path in
                                 (plugins / "bili-extractor.py", plugins / "tools" / "yt-dlp", override))
        backup = data / ".biliaudio-backups" / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}"
        originals = {}
        had_override = override.exists()
        try:
            for path, (content, mode) in changed.items():
                if path.exists():
                    original_stat = path.stat()
                    originals[path] = (path.read_bytes(), original_stat.st_mode & 0o777,
                                       (original_stat.st_uid, original_stat.st_gid))
                else:
                    originals[path] = None
                if originals[path] is not None:
                    backup.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup / path.name)
                write_atomic(path, content, mode)
            default_config = json.loads(run("docker", "compose", "config", "--format", "json", cwd=base.parent).stdout)
            default_bot = run("docker", "compose", "ps", "-q", args.service, cwd=base.parent).stdout.strip()
            if "bili-extractor" not in default_config["services"] or default_bot != bot_id:
                raise InstallError("常规 docker compose 命令不会持续加载新侧车配置；请检查 COMPOSE_FILE 或项目目录")
            wait_healthy(base, override, args.service, bot_id, recreate_extractor)
        except BaseException as original_error:
            try:
                # 新建服务先移除，再恢复原文件；已有服务用原配置重新创建。
                if override.exists() and not had_override:
                    rollback_command = ("docker", "compose", "-f", str(base), "-f", str(override))
                    run(*rollback_command, "stop", "bili-extractor", cwd=base.parent)
                    run(*rollback_command, "rm", "-f", "bili-extractor", cwd=base.parent)
                for path, previous in reversed(list(originals.items())):
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, *previous)
                if had_override:
                    run("docker", "compose", "-f", str(base), "-f", str(override),
                        "up", "-d", "--force-recreate", "bili-extractor", cwd=base.parent)
            except Exception as rollback_error:
                raise InstallError(f"自动回退不完整；旧文件备份在 {backup}：{rollback_error}") from original_error
            raise
    print(f"安装完成：{variant}；解析服务 healthy。机器人未重启。")
    if plugin_was_present and plugin_changed:
        print("插件 DLL 已更新：在机器人聊天中依次执行 !plugin unload BiliAudio.dll、!plugin load BiliAudio.dll。")
    else:
        print("首次使用时在机器人聊天中执行 !plugin load BiliAudio.dll。")
    if grant is not None:
        print("权限已更新：在机器人聊天中执行 !rights reload。")
    if not group_id and not user_uid:
        print(f"未改动权限：请在 {rights} 中为需要使用的人授予 cmd.bili.* 和 cmd.bplay。")


def main():
    parser = argparse.ArgumentParser(description="安装可选 BiliAudio 插件到现有 Docker Compose 机器人")
    parser.add_argument("--compose", type=Path, help="机器人主 Compose 文件；默认识别当前目录")
    parser.add_argument("--service", default="ts3audiobot", help="机器人服务名")
    parser.add_argument("--variant", choices=("stable-0.12.0", "master-nightly"), help="插件适配的机器人版本")
    parser.add_argument("--bot-dll", default="/app/TS3AudioBot.dll", help="机器人容器内 TS3AudioBot.dll 的路径")
    parser.add_argument("--data-dir", type=Path, help="无法自动识别时指定机器人数据 bind mount 来源")
    parser.add_argument("--grant-group-id", type=int, help="仅授权给指定 TeamSpeak 服务器组")
    parser.add_argument("--grant-user-uid", help="仅授权给指定 TeamSpeak 用户 UID")
    parser.add_argument("--no-grant", action="store_true", help="不修改权限文件，也不询问")
    parser.add_argument("--archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--yt-dlp", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.grant_group_id and args.grant_user_uid:
        parser.error("只能选择一种授权范围")
    try:
        install(args)
    except (InstallError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"安装失败：{exc}\n")


if __name__ == "__main__":
    main()
