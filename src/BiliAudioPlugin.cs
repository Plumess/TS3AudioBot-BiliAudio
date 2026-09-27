using System;
using System.IO;
using System.Collections.Generic;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using TS3AudioBot;
using TS3AudioBot.Audio;
using TS3AudioBot.CommandSystem;
using TS3AudioBot.Plugins;
using TS3AudioBot.ResourceFactories;

namespace BiliAudio
{
    public sealed class BiliAudioPlugin : IBotPlugin
    {
        private static readonly NLog.Logger Log = NLog.LogManager.GetCurrentClassLogger();
        private readonly PlayManager playManager;
        private readonly AudioRelay relay = new AudioRelay();
        private readonly SemaphoreSlim queueGate = new SemaphoreSlim(1, 1);
        private readonly object stateLock = new object();
        private BiliQueue? queue;

        public BiliAudioPlugin(PlayManager playManager) => this.playManager = playManager;

        public void Initialize()
        {
            relay.Start();
            playManager.ResourceStopped += OnResourceStopped;
            playManager.AfterResourceStarted += OnResourceStarted;
        }

        [Command("bili play")]
        public async Task<string> Play(InvokerData invoker, string input)
        {
            try
            {
                await queueGate.WaitAsync();
                try
                {
                    if (BiliLink.IsList(input))
                        return await StartList(invoker, BiliLink.ParseList(input), false);
                    lock (stateLock) queue = null;
                    return await PlayVideo(invoker, BiliLink.ParseVideo(input));
                }
                finally { queueGate.Release(); }
            }
            catch (Exception ex)
            {
                Log.Warn(ex, "Bilibili playback failed");
                return $"Bilibili 播放失败：{ex.Message}";
            }
        }

        [Command("bplay")]
        public Task<string> Direct(InvokerData invoker, string input) => Play(invoker, input);

        [Command("bili list")]
        public async Task<string> List(InvokerData invoker, string input) => await StartListCommand(invoker, input, false);

        [Command("bili shuffle")]
        public async Task<string> Shuffle(InvokerData invoker, string input) => await StartListCommand(invoker, input, true);

        [Command("bili mode")]
        public string Mode(string mode)
        {
            if (mode != "seq" && mode != "random")
                return "模式只能是 seq 或 random";
            lock (stateLock)
            {
                if (queue == null) return "当前没有 Bilibili 列表";
                queue.SetRandom(mode == "random");
            }
            return mode == "random" ? "已改为随机播放剩余条目" : "已改为顺序播放剩余条目";
        }

        [Command("bili next")]
        public async Task<string> Next(InvokerData invoker)
        {
            await queueGate.WaitAsync();
            try
            {
                lock (stateLock) { if (queue == null) return "当前没有 Bilibili 列表"; }
                return await PlayNext(invoker, false) ? "已切换到下一条" : "列表已经播完";
            }
            finally { queueGate.Release(); }
        }

        [Command("bili clear")]
        public string Clear()
        {
            lock (stateLock) queue = null;
            return "已清空 Bilibili 待播列表，当前歌曲继续播放";
        }

        private async Task<string> StartListCommand(InvokerData invoker, string input, bool random)
        {
            try
            {
                await queueGate.WaitAsync();
                try { return await StartList(invoker, BiliLink.ParseList(input), random); }
                finally { queueGate.Release(); }
            }
            catch (Exception ex)
            {
                Log.Warn(ex, "Bilibili list failed");
                return $"Bilibili 列表失败：{ex.Message}";
            }
        }

        private async Task<string> StartList(InvokerData invoker, string url, bool random)
        {
            var list = await BiliExtractor.List(url);
            if (list.Entries.Count == 0) return "这个列表没有可播放的视频（私密收藏夹需要登录）";
            var next = new BiliQueue(list, random);
            lock (stateLock) queue = next;
            if (!await PlayNext(invoker, false)) return "列表中的视频都无法播放";
            return $"已开始{(random ? "随机" : "顺序")}播放《{list.Title}》，共载入 {list.Entries.Count} 条{(list.Truncated ? "（仅前 100 条）" : "")}";
        }

        private async Task<string> PlayVideo(InvokerData invoker, string url)
        {
            var audio = await BiliExtractor.Resolve(url);
            var localUrl = relay.Publish(audio.Url, audio.Headers);
            var resource = new AudioResource(localUrl, audio.Title, "media");
            await playManager.Play(invoker, new MediaPlayResource(localUrl, resource, null, false));
            return $"正在播放：{audio.Title}";
        }

        private async Task<bool> PlayNext(InvokerData invoker, bool enqueue)
        {
            for (var failures = 0; failures < 5; failures++)
            {
                BiliQueue? current;
                string? url;
                lock (stateLock)
                {
                    current = queue;
                    url = current?.TakeNext();
                }
                if (url == null)
                {
                    lock (stateLock) { if (queue == current) queue = null; }
                    return false;
                }
                try
                {
                    var audio = await BiliExtractor.Resolve(url);
                    lock (stateLock) { if (queue != current) return false; }
                    var localUrl = relay.Publish(audio.Url, audio.Headers);
                    lock (stateLock) current!.CurrentUrl = localUrl;
                    var resource = new AudioResource(localUrl, audio.Title, "media");
                    if (enqueue)
                        await playManager.Enqueue(invoker, resource);
                    else
                        await playManager.Play(invoker, new MediaPlayResource(localUrl, resource, null, false));
                    return true;
                }
                catch (Exception ex) { Log.Warn(ex, "Skipping Bilibili list entry"); }
            }
            lock (stateLock) queue = null;
            return false;
        }

        private async Task OnResourceStopped(object? sender, SongEndEventArgs e)
        {
            await queueGate.WaitAsync();
            try
            {
                BiliQueue? current;
                lock (stateLock) current = queue;
                if (current == null || playManager.CurrentPlayData?.ResourceData.ResourceId != current.CurrentUrl)
                    return;
                if (!e.SongEndedByCallback)
                {
                    lock (stateLock) queue = null;
                    return;
                }
                await PlayNext(playManager.CurrentPlayData.Invoker, true);
            }
            finally { queueGate.Release(); }
        }

        private Task OnResourceStarted(object? sender, PlayInfoEventArgs e)
        {
            lock (stateLock)
            {
                if (queue != null && e.ResourceData.ResourceId != queue.CurrentUrl)
                    queue = null;
            }
            return Task.CompletedTask;
        }

        public void Dispose()
        {
            playManager.ResourceStopped -= OnResourceStopped;
            playManager.AfterResourceStarted -= OnResourceStarted;
            relay.Dispose();
            queueGate.Dispose();
        }
    }

    internal sealed class BiliQueue
    {
        private readonly List<(int Index, string Url)> entries = new List<(int, string)>();
        private readonly Random random = new Random();
        private int position;
        public string CurrentUrl { get; set; } = "";

        public BiliQueue(BiliList list, bool shuffle)
        {
            for (var i = 0; i < list.Entries.Count; i++) entries.Add((i, list.Entries[i].Url));
            if (shuffle) SetRandom(true);
        }

        public string? TakeNext() => position < entries.Count ? entries[position++].Url : null;

        public void SetRandom(bool shuffle)
        {
            if (!shuffle)
            {
                entries.Sort(position, entries.Count - position, Comparer<(int Index, string Url)>.Create((a, b) => a.Index.CompareTo(b.Index)));
                return;
            }
            for (var i = entries.Count - 1; i > position; i--)
            {
                var j = random.Next(position, i + 1);
                (entries[i], entries[j]) = (entries[j], entries[i]);
            }
        }
    }

    internal static class BiliLink
    {
        private static Uri Parse(string input)
        {
            var value = input.Replace("\\&", "&").Replace('\u00a0', ' ').Trim();
            var match = System.Text.RegularExpressions.Regex.Match(value, @"https://[^\s<>\]\)]+", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            if (match.Success) value = match.Value;
            value = value.TrimEnd('。', '，', ',', '.', ';', '；');
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
                throw new ArgumentException("请提供 Bilibili 的 https 链接");
            return uri;
        }

        public static bool IsList(string input)
        {
            var uri = Parse(input);
            return IsListUri(uri);
        }

        public static string ParseVideo(string input)
        {
            var uri = Parse(input);
            var host = uri.IdnHost.ToLowerInvariant();
            if ((host == "www.bilibili.com" || host == "bilibili.com" || host == "m.bilibili.com") &&
                uri.AbsolutePath.StartsWith("/video/", StringComparison.OrdinalIgnoreCase))
                return uri.AbsoluteUri;
            if (host == "b23.tv")
                return uri.AbsoluteUri;
            throw new ArgumentException("仅支持 Bilibili 视频链接和 b23.tv 短链接");
        }

        public static string ParseList(string input)
        {
            var uri = Parse(input);
            if (!IsListUri(uri) && !((uri.Host == "www.bilibili.com" || uri.Host == "bilibili.com") &&
                uri.AbsolutePath.StartsWith("/video/", StringComparison.OrdinalIgnoreCase)))
                throw new ArgumentException("请提供 Bilibili 收藏夹、合集或分 P 视频链接");
            return uri.AbsoluteUri;
        }

        private static bool IsListUri(Uri uri)
        {
            var host = uri.IdnHost.ToLowerInvariant();
            var path = uri.AbsolutePath;
            if (host == "space.bilibili.com")
                return System.Text.RegularExpressions.Regex.IsMatch(path, @"^/\d+/(favlist|lists/\d+|channel/(collectiondetail|seriesdetail))/?$");
            return (host == "www.bilibili.com" || host == "bilibili.com") &&
                (path.StartsWith("/list/", StringComparison.OrdinalIgnoreCase) ||
                 path.StartsWith("/medialist/detail/ml", StringComparison.OrdinalIgnoreCase) ||
                 path.StartsWith("/medialist/play/", StringComparison.OrdinalIgnoreCase));
        }
    }

    internal sealed class BiliList
    {
        public string Title { get; set; } = "";
        public List<BiliListEntry> Entries { get; set; } = new List<BiliListEntry>();
        public bool Truncated { get; set; }
    }

    internal sealed class BiliListEntry
    {
        public string Url { get; set; } = "";
    }

    internal sealed class AudioSource
    {
        public string Url { get; set; } = "";
        public string Title { get; set; } = "";
        public System.Collections.Generic.Dictionary<string, string> Headers { get; set; } = new System.Collections.Generic.Dictionary<string, string>();
    }

    internal static class BiliExtractor
    {
        private static readonly HttpClient Client = new HttpClient { Timeout = TimeSpan.FromSeconds(35) };
        private static readonly HttpClient ListClient = new HttpClient { Timeout = TimeSpan.FromSeconds(55) };
        private static readonly string BaseUrl = (Environment.GetEnvironmentVariable("BILI_EXTRACTOR_URL") ?? "http://127.0.0.1:18944").TrimEnd('/');

        public static async Task<BiliList> List(string url)
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/list")
            {
                Content = new StringContent(JsonSerializer.Serialize(new { url }), Encoding.UTF8, "application/json")
            };
            HttpResponseMessage response;
            try { response = await ListClient.SendAsync(request); }
            catch (HttpRequestException) { throw new InvalidOperationException("Bilibili 解析服务未运行"); }
            using (response)
            {
                var json = await response.Content.ReadAsStringAsync();
                if (!response.IsSuccessStatusCode)
                    throw new InvalidOperationException(json.Length > 400 ? json.Substring(0, 400) : json);
                if (json.Length > 128 * 1024) throw new InvalidOperationException("列表结果过大");
                return JsonSerializer.Deserialize<BiliList>(json, new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                    ?? throw new InvalidOperationException("列表结果无效");
            }
        }

        public static async Task<AudioSource> Resolve(string videoUrl)
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/resolve")
            {
                Content = new StringContent(JsonSerializer.Serialize(new { url = videoUrl }), Encoding.UTF8, "application/json")
            };
            HttpResponseMessage response;
            try { response = await Client.SendAsync(request); }
            catch (HttpRequestException) { throw new InvalidOperationException("Bilibili 解析服务未运行"); }
            using (response)
            {
                var json = await response.Content.ReadAsStringAsync();
                if (!response.IsSuccessStatusCode)
                    throw new InvalidOperationException(json.Length > 400 ? json.Substring(0, 400) : json);
                if (json.Length > 64 * 1024)
                    throw new InvalidOperationException("解析结果过大");
                return Parse(json);
            }
        }

        private static AudioSource Parse(string json)
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (!root.TryGetProperty("url", out var urlValue) || urlValue.ValueKind != JsonValueKind.String)
                throw new InvalidOperationException("没有找到可用的独立音频流");
            var url = urlValue.GetString() ?? "";
            if (!Uri.TryCreate(url, UriKind.Absolute, out var streamUri) || streamUri.Scheme != Uri.UriSchemeHttps)
                throw new InvalidOperationException("音频流地址无效");

            var source = new AudioSource { Url = url, Title = root.GetProperty("title").GetString() ?? "Bilibili 视频" };
            if (root.TryGetProperty("http_headers", out var headers) && headers.ValueKind == JsonValueKind.Object)
            {
                foreach (var header in headers.EnumerateObject())
                {
                    if (header.Value.ValueKind == JsonValueKind.String &&
                        (header.Name.Equals("User-Agent", StringComparison.OrdinalIgnoreCase) ||
                         header.Name.Equals("Referer", StringComparison.OrdinalIgnoreCase)))
                        source.Headers[header.Name] = header.Value.GetString() ?? "";
                }
            }
            return source;
        }
    }

    internal sealed class AudioRelay : IDisposable
    {
        private static readonly NLog.Logger Log = NLog.LogManager.GetCurrentClassLogger();
        private const int Port = 18943;
        private readonly HttpClient client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = true })
        {
            Timeout = TimeSpan.FromMinutes(10)
        };
        private readonly HttpListener listener = new HttpListener();
        private readonly SemaphoreSlim slots = new SemaphoreSlim(2, 2);
        private volatile bool disposed;
        private RelayEntry? current;

        private sealed class RelayEntry
        {
            public string Token { get; set; } = "";
            public AudioSource Source { get; set; } = new AudioSource();
        }

        public void Start()
        {
            listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
            listener.Start();
            _ = AcceptLoop();
        }

        public string Publish(string url, System.Collections.Generic.Dictionary<string, string> headers)
        {
            var next = Guid.NewGuid().ToString("N");
            Volatile.Write(ref current, new RelayEntry
            {
                Token = next,
                Source = new AudioSource { Url = url, Headers = headers }
            });
            return $"http://127.0.0.1:{Port}/{next}/audio";
        }

        private async Task AcceptLoop()
        {
            while (!disposed)
            {
                HttpListenerContext context;
                try { context = await listener.GetContextAsync(); }
                catch (HttpListenerException) when (disposed) { return; }
                catch (ObjectDisposedException) when (disposed) { return; }
                _ = Serve(context);
            }
        }

        private async Task Serve(HttpListenerContext context)
        {
            try
            {
                var entry = Volatile.Read(ref current);
                if (entry == null || context.Request.Url?.AbsolutePath != $"/{entry.Token}/audio" ||
                    (context.Request.HttpMethod != "GET" && context.Request.HttpMethod != "HEAD"))
                {
                    context.Response.StatusCode = 404;
                    return;
                }
                await slots.WaitAsync();
                try
                {
                    using var request = new HttpRequestMessage(HttpMethod.Get, entry.Source.Url);
                    foreach (var header in entry.Source.Headers)
                        request.Headers.TryAddWithoutValidation(header.Key, header.Value);
                    var range = context.Request.Headers["Range"];
                    if (!string.IsNullOrEmpty(range))
                        request.Headers.TryAddWithoutValidation("Range", range);
                    using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
                    context.Response.StatusCode = (int)response.StatusCode;
                    context.Response.ContentType = response.Content.Headers.ContentType?.ToString() ?? "application/octet-stream";
                    if (response.Content.Headers.ContentLength.HasValue)
                        context.Response.ContentLength64 = response.Content.Headers.ContentLength.Value;
                    if (response.Content.Headers.ContentRange != null)
                        context.Response.Headers["Content-Range"] = response.Content.Headers.ContentRange.ToString();
                    context.Response.Headers["Accept-Ranges"] = "bytes";
                    if (context.Request.HttpMethod == "HEAD" || !response.IsSuccessStatusCode)
                        return;
                    using var body = await response.Content.ReadAsStreamAsync();
                    var buffer = new byte[64 * 1024];
                    int count;
                    while ((count = await body.ReadAsync(buffer, 0, buffer.Length)) > 0)
                        await context.Response.OutputStream.WriteAsync(buffer, 0, count);
                }
                finally { slots.Release(); }
            }
            catch (Exception ex) when (ex is HttpRequestException || ex is IOException || ex is ObjectDisposedException)
            {
                Log.Debug(ex, "Audio stream closed");
            }
            finally
            {
                try { context.Response.Close(); } catch (ObjectDisposedException) { }
            }
        }

        public void Dispose()
        {
            disposed = true;
            listener.Stop();
            listener.Close();
            client.Dispose();
        }
    }
}
