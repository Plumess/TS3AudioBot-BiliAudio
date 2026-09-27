# BiliAudio for TS3AudioBot

Optional Bilibili audio playback for TS3AudioBot. Plays public videos, favorites, creator collections/series, and multi-part videos. Lists can play in order or shuffled order. The extractor uses the official `yt-dlp` release; audio is streamed through a small local relay without storing media files.

## Compatibility

The build workflow targets TS3AudioBot 0.12.0 and the master nightly build. Choose the archive matching your bot when a release is available. FFmpeg, Python 3.10+, and a current `yt-dlp` executable are required. This plugin does not install or modify YunPlugin.

## Install without Docker

1. Download the matching `BiliAudio-*.zip` release artifact. Put `BiliAudio.dll` into the bot's configured `plugins` directory.
2. Install the official [yt-dlp release](https://github.com/yt-dlp/yt-dlp/releases) so `yt-dlp` is on `PATH`. Run `python extractor.py` from the archive on the same machine as the bot, using Python 3.10 or newer. It listens on `127.0.0.1:18944` by default. Official Python zipapps run with the same interpreter as the extractor, even if the system's default `python3` is older.
3. Add `cmd.bili.*` and `cmd.bplay` to the appropriate `rights.toml` rule. Load the plugin with `!plugin load BiliAudio.dll`, or add that command to the bot's on-connect actions.

Set `BILI_YTDLP_BIN` for a custom yt-dlp path or `BILI_EXTRACTOR_URL` in the bot process for a non-default extractor address. Keep the extractor private; it has no user authentication.

## Install with Docker Compose

For a Compose service named `ts3audiobot`, put `BiliAudio.dll` in the bot's `data/plugins/` directory and copy the archive's `extractor.py` there as `bili-extractor.py`. Download the official yt-dlp release executable to `data/plugins/tools/yt-dlp` and make it executable. The sample [Compose overlay](examples/docker-compose.bili.yml) shares the bot's network namespace, so the extractor remains on loopback and no new port is published. Use it alongside your bot Compose file, then grant `cmd.bili.*` and `cmd.bplay` and load the plugin as above.

```sh
docker compose -f docker-compose.yml -f /path/to/examples/docker-compose.bili.yml up -d
```

Compose resolves the overlay's `./data/...` bind paths relative to the first Compose file. If your bot service has a different name, change `network_mode` accordingly. Do not use the host-only installation steps inside a container unless both processes share a network namespace.

## Commands

```text
!bili play https://www.bilibili.com/video/BV...
!bplay https://www.bilibili.com/video/BV...
!bili list https://space.bilibili.com/2142762/lists/3662502?type=season
!bili shuffle https://www.bilibili.com/medialist/detail/ml...
!bili mode seq
!bili mode random
!bili next
!bili clear
```

`!bili play` detects list links and plays them in order. `!bili list` also accepts a multi-part video URL. `!bili mode` changes the order of remaining entries; `!bili clear` leaves the current track playing. Pasted text containing a Bilibili URL is accepted. Each request loads at most the first 100 list entries, then resolves an audio URL only when its track starts. Private favorites, paid/DRM videos, and login-only content are not supported.

## Related work and thanks

- [Splamy/TS3AudioBot](https://github.com/Splamy/TS3AudioBot) provides the bot and plugin API (OSL-3.0).
- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) and its Bilibili extractor contributors provide the site parsing logic. This project runs its official release as a separate tool; it does not reimplement their extractor.
- [xxmod/TS3AudioBot-BiliBiliPlugin](https://github.com/xxmod/TS3AudioBot-BiliBiliPlugin) is an existing community plugin with account login, history, queue, multi-part selection, and collection playback (MPL-2.0). Choose it for account-based features. BiliAudio is a separate implementation focused on public favorite/list links, sequential or shuffled playback, Docker deployment, and low disk usage; no code from that project was copied.
- [577fkj/TS3AudioBot-CloudMusic-plugin](https://github.com/577fkj/TS3AudioBot-CloudMusic-plugin) demonstrates the release-and-install pattern used by TS3AudioBot plugins.

## Development

Place the matching TS3AudioBot, TSLib, and NLog DLLs in `src/lib/`, then build `src/BiliAudio.csproj` with the .NET Core 3.1 SDK. Run `python3 -m unittest discover -s src -p 'test_*.py'` for extractor checks. The reference DLLs and build output are intentionally ignored by Git.
