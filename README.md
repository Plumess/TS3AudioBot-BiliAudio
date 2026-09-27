# BiliAudio：TS3AudioBot 的 B 站音频插件

BiliAudio 是可选安装的独立插件，不会修改云音乐插件。它支持播放 B 站公开视频、公开收藏夹、UP 主合集/系列及多 P 视频；列表可顺序或随机播放。站点解析交给 [yt-dlp](https://github.com/yt-dlp/yt-dlp)，音频通过本地服务流式转发，不保存媒体文件。

## 适用范围

- 支持完整视频链接和 `b23.tv` 单视频短链接；多 P 视频可用链接中的 `?p=2` 指定分 P，也可用 `!bili list` 连播。
- 每次最多读取列表前 100 条，只在轮到某条视频时解析其音频地址。
- 不支持直接输入 BV 号、单条视频加入待播列表或交互式选分 P。账号登录、观看历史、私密收藏夹、付费及 DRM 内容也不在支持范围内。
- 需要 TS3AudioBot、FFmpeg、Python 3.10+ 和官方发布的 `yt-dlp`。构建流程分别适配 TS3AudioBot 0.12.0 与 master nightly，请使用与机器人版本匹配的插件 DLL。

仓库只提交源码，不提交机器人依赖 DLL 或编译产物。正式 Release 发布前，可以从成功的 [Actions 构建](https://github.com/Plumess/TS3AudioBot-BiliAudio/actions/workflows/build.yml)下载对应 ZIP；发布后也可从 Releases 下载。

## Docker Compose 一行安装

正式 Release 发布后，在**已经运行的机器人 Compose 目录**执行（需 Docker Compose 和宿主机 Python 3.11+）：

```sh
curl -fsSLo biliaudio-install.py https://github.com/Plumess/TS3AudioBot-BiliAudio/releases/latest/download/install.py && python3.11 biliaudio-install.py --compose ./docker-compose.yml
```

安装器会识别机器人容器的 `./data` bind mount，下载官方 yt-dlp 和匹配的插件 ZIP，核对 Release 校验值与**运行中机器人的 DLL 指纹**，备份旧文件、生成可自动加载的 Compose 附加文件，启动解析服务并检查健康状态。重复执行可升级；不匹配的稳定版或 nightly 会在改动文件前停止。若主文件名不是 `docker-compose.yml`，替换 `--compose` 参数；使用非标准机器人镜像时可用 `--bot-dll` 指定容器内 DLL 路径。

安装器只支持常见的 Compose 文件名、运行中的机器人服务和 bind mount 数据目录；已有手工部署的侧车、用户维护的同名 override 文件或自定义插件目录会停止并给出原因，不会强行覆盖。可用 `--service` 指定机器人服务名。交互运行时可选择按 TeamSpeak 服务器组 ID 或用户 UID 授权，也可跳过；非交互运行默认不改 `rights.toml`。之后在机器人聊天中执行 `!plugin load BiliAudio.dll`，更新已有插件时先 `!plugin unload BiliAudio.dll` 再加载，修改了权限则再执行 `!rights reload`。远程脚本会在本机执行；可先检查下载的 `biliaudio-install.py` 再运行。

## Docker Compose 手动安装

以下示例假设机器人服务名为 `ts3audiobot`，数据目录为第一个 Compose 文件旁的 `./data`：

1. 将 ZIP 中的 `BiliAudio.dll` 放到 `data/plugins/`，将 `extractor.py` 复制为 `data/plugins/bili-extractor.py`。
2. 将官方 [yt-dlp 发布文件](https://github.com/yt-dlp/yt-dlp/releases)放到 `data/plugins/tools/yt-dlp`，并赋予执行权限。ZIP 不包含 yt-dlp 或 FFmpeg。
3. 将 ZIP 根目录中的 `docker-compose.bili.yml` 放到机器人 Compose 文件旁（仓库中也有[同一示例](examples/docker-compose.bili.yml)），然后一起启动：

```sh
docker compose -f docker-compose.yml -f ./docker-compose.bili.yml up -d
```

附加服务与机器人共用网络命名空间，解析服务只监听 `127.0.0.1:18944`，不会对外开放新端口。健康检查会在 yt-dlp 缺失时标记服务异常。若机器人服务名不同，需修改附加文件中的 `network_mode`。附加文件的 `./data/...` 路径相对于第一个 Compose 文件解析。

最后，在机器人的 `rights.toml` 中授予相应用户 `cmd.bili.*` 和 `cmd.bplay` 权限；执行 `!plugin load BiliAudio.dll` 加载插件，或将它加入机器人的连接后执行命令。

## 非 Docker 安装

1. 将匹配机器人版本的 ZIP 中的 `BiliAudio.dll` 放入机器人配置的插件目录。
2. 安装 Python 3.10+ 与官方 yt-dlp，并确保 `yt-dlp` 可从 `PATH` 找到。用同一台机器上的 Python 运行 ZIP 中的 `extractor.py`，默认监听 `127.0.0.1:18944`。如果 yt-dlp 是 Python zipapp，解析服务会使用自己的 Python 解释器运行它。
3. 按上述方式配置权限并加载插件。FFmpeg 仍由机器人播放链路使用。

可设置解析服务的 `BILI_YTDLP_BIN` 指定 yt-dlp 路径；可在机器人进程中设置 `BILI_EXTRACTOR_URL` 指定解析服务地址。解析服务没有用户认证，不要将它暴露到公网。

## 命令

| 命令 | 作用 |
| --- | --- |
| `!bili play <视频或列表链接>` | 立即播放；列表链接按顺序播放 |
| `!bplay <视频或列表链接>` | `!bili play` 的简写 |
| `!bili list <列表或多P视频链接>` | 顺序播放整个列表 |
| `!bili shuffle <列表或多P视频链接>` | 随机播放列表 |
| `!bili mode seq` / `!bili mode random` | 调整当前列表剩余条目的播放顺序 |
| `!bili next` | 播放当前列表的下一条 |
| `!bili clear` | 清空待播列表，当前音频继续播放 |

例如：

```text
!bili play https://www.bilibili.com/video/BV1LVbu64Ewi
!bili play https://www.bilibili.com/video/BV1bK411W797?p=2
!bili list https://space.bilibili.com/2142762/lists/3662502?type=season
!bili shuffle https://www.bilibili.com/medialist/detail/ml1103407912
```

粘贴的文字中只要含有可识别的 B 站链接，也可以直接交给播放命令。`!bili play` 遇到列表链接时会按顺序播放；`!bili mode` 只影响尚未播放的条目。

## 相关项目与致谢

- [Splamy/TS3AudioBot](https://github.com/Splamy/TS3AudioBot) 提供机器人和插件 API（OSL-3.0）。
- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) 及其 B 站提取器贡献者维护站点解析逻辑。本项目调用其独立发布文件，不复制提取器源码。
- [xxmod/TS3AudioBot-BiliBiliPlugin](https://github.com/xxmod/TS3AudioBot-BiliBiliPlugin) 是已有的社区插件，提供登录、历史、单条队列、分 P 选择和合集播放等功能（MPL-2.0）。BiliAudio 侧重公开链接、列表顺序/随机播放及 Docker 部署，没有复制该项目的代码。
- [577fkj/TS3AudioBot-CloudMusic-plugin](https://github.com/577fkj/TS3AudioBot-CloudMusic-plugin) 提供了 TS3AudioBot 插件发布与安装方式的参考。

## 开发与验证

将对应版本的 `TS3AudioBot.dll`、`TSLib.dll` 和 `NLog.dll` 放入 `src/lib/`，使用 .NET Core 3.1 SDK 执行 `dotnet build src/BiliAudio.csproj -t:Rebuild -c Release`，再运行 `dotnet run --project tests/BiliAudio.Tests.csproj -c Release` 检查依赖版本、队列与音频中继。更换参照 DLL 后务必重新构建，避免沿用旧版本产物。运行 `python3 -m unittest discover -s src -p 'test_*.py'` 检查解析服务，运行 `python3.11 -m unittest discover -s tests -p 'test_*.py'` 检查安装器。CI 会分别对稳定版和 nightly 构建、测试并打包；依赖 DLL 和构建产物不会提交到 Git。
