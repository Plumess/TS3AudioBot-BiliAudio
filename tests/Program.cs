using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using BiliAudio;

internal static class Program
{
    private static async Task<int> Main()
    {
        try
        {
            TestAssemblyCompatibility();
            TestQueueAndLinks();
            await TestRelayCancellation();
            Console.WriteLine("BiliAudio C# tests passed");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static void TestAssemblyCompatibility()
    {
        var root = Directory.GetCurrentDirectory();
        var reference = AssemblyName.GetAssemblyName(Path.Combine(root, "src/lib/TS3AudioBot.dll"));
        var plugin = Assembly.LoadFile(Path.Combine(root, "src/bin/Release/netcoreapp3.1/BiliAudio.dll"));
        var referenced = Array.Find(plugin.GetReferencedAssemblies(), assembly => assembly.Name == "TS3AudioBot");
        Check(referenced?.Version == reference.Version,
              $"插件依赖版本不匹配：插件 {referenced?.Version}，参照 {reference.Version}");
        Console.WriteLine($"TS3AudioBot dependency: {reference.Version}");
    }

    private static void TestQueueAndLinks()
    {
        var list = new BiliList
        {
            Entries = new List<BiliListEntry>
            {
                new BiliListEntry { Url = "first" },
                new BiliListEntry { Url = "second" },
                new BiliListEntry { Url = "third" }
            }
        };
        var queue = new BiliQueue(list, false);
        Check(queue.TakeNext() == "first", "列表应从第一条开始");
        queue.SetRandom(true);
        queue.SetRandom(false);
        Check(queue.TakeNext() == "second" && queue.TakeNext() == "third" && queue.TakeNext() == null,
              "切回顺序模式不应重播或漏播");

        Check(BiliLink.IsList("https://space.bilibili.com/2142762/lists/3662502?type=season"), "合集链接应识别为列表");
        Check(BiliLink.ParseVideo("标题 https://www.bilibili.com/video/BV1LVbu64Ewi?p=2")
              .Contains("?p=2"), "粘贴文本应保留分 P 参数");
        try
        {
            BiliLink.ParseVideo("https://bilibili.com.evil.example/video/BV1LVbu64Ewi");
            throw new InvalidOperationException("外部域名不应通过校验");
        }
        catch (ArgumentException) { }
        Check(BiliExtractor.ErrorMessage("{\"error\":\"yt-dlp 未安装或不可执行\"}") == "yt-dlp 未安装或不可执行",
              "应显示解析服务的明确错误");
        Check(BiliExtractor.ErrorMessage("[]") == "[]", "非对象错误结果不应再次抛错");
    }

    private static async Task TestRelayCancellation()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        var port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();

        using var handler = new FakeAudioHandler();
        using var relay = new AudioRelay(handler, port, TimeSpan.FromMilliseconds(500));
        using var client = new HttpClient(new HttpClientHandler { UseProxy = false })
        {
            Timeout = TimeSpan.FromSeconds(5)
        };
        relay.Start();
        var oldUrl = relay.Publish("https://audio.invalid/slow", new Dictionary<string, string>());
        var oldResponses = await Within(Task.WhenAll(
            client.GetAsync(oldUrl, HttpCompletionOption.ResponseHeadersRead),
            client.GetAsync(oldUrl, HttpCompletionOption.ResponseHeadersRead)), 4000);
        using var oldFirst = oldResponses[0];
        using var oldSecond = oldResponses[1];
        await Within(handler.BothWaiting.Task, 4000);

        using var full = await Within(client.GetAsync(oldUrl, HttpCompletionOption.ResponseHeadersRead), 4000);
        Check(full.StatusCode == HttpStatusCode.ServiceUnavailable, "转发槽位满时应及时返回 503");

        var newUrl = relay.Publish("https://audio.invalid/fresh", new Dictionary<string, string>());
        using var fresh = await Within(client.GetAsync(newUrl), 4000);
        var bytes = await fresh.Content.ReadAsByteArrayAsync();
        Check(fresh.IsSuccessStatusCode && bytes.Length == 2 && bytes[0] == 2 && bytes[1] == 3,
              "切歌后应取消旧流并播放新流");
        using var stale = await Within(client.GetAsync(oldUrl), 4000);
        Check(stale.StatusCode == HttpStatusCode.NotFound, "旧转发地址应失效");
    }

    private static async Task<T> Within<T>(Task<T> task, int milliseconds)
    {
        if (await Task.WhenAny(task, Task.Delay(milliseconds)) != task)
            throw new TimeoutException("测试等待超时");
        return await task;
    }

    private static void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private sealed class FakeAudioHandler : HttpMessageHandler
    {
        private int waitingCount;
        public TaskCompletionSource<bool> BothWaiting { get; } =
            new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            HttpContent content = request.RequestUri?.AbsolutePath == "/slow"
                ? (HttpContent)new StreamContent(new SlowStream(() =>
                {
                    if (Interlocked.Increment(ref waitingCount) == 2)
                        BothWaiting.TrySetResult(true);
                }))
                : new ByteArrayContent(new byte[] { 2, 3 });
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = content });
        }
    }

    private sealed class SlowStream : Stream
    {
        private readonly Action onWaiting;
        private int sentFirst;

        public SlowStream(Action onWaiting) => this.onWaiting = onWaiting;
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

        public override async Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken)
        {
            if (Interlocked.Exchange(ref sentFirst, 1) == 0)
            {
                buffer[offset] = 1;
                return 1;
            }
            onWaiting();
            await Task.Delay(Timeout.Infinite, cancellationToken);
            return 0;
        }
    }
}
