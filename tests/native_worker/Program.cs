using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json.Nodes;
using Upcam.Desktop;

if (args.Contains("--child"))
{
    string? line;
    while ((line = Console.ReadLine()) is not null)
    {
        var request = JsonNode.Parse(line)!;
        string id = request["id"]!.GetValue<string>();
        string mode = request["payload"]?["mode"]?.GetValue<string>() ?? "normal";
        void Progress() => Console.WriteLine(new JsonObject { ["id"] = id, ["kind"] = "progress",
            ["progress"] = new JsonObject { ["phase"] = "collecting", ["completed"] = 1, ["total"] = 3,
                ["requests"] = 2, ["batches"] = 1, ["passes"] = 0 } }.ToJsonString());
        if (mode == "hang") { await Task.Delay(10000); return; }
        if (mode == "heartbeat") { while (true) { Progress(); await Task.Delay(20); } }
        if (mode == "progress") { Progress(); Progress(); }
        if (mode == "delayed") await Task.Delay(350);
        if (mode == "bad-progress")
        { Console.WriteLine(new JsonObject { ["id"] = id, ["kind"] = "progress", ["progress"] = new JsonObject { ["phase"] = "private task title" } }); continue; }
        Console.WriteLine(new JsonObject { ["id"] = id, ["ok"] = true,
            ["result"] = new JsonObject { ["done"] = true } }.ToJsonString());
    }
    return;
}

void Check(bool condition, string name) { if (!condition) throw new Exception(name); Console.WriteLine("PASS " + name); }
ProcessStartInfo Child()
{
    var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true,
        RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
        StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = new UTF8Encoding(false) };
    if (Path.GetFileNameWithoutExtension(Environment.ProcessPath).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        start.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
    start.ArgumentList.Add("--child");
    return start;
}
WorkerClient Client(double overall = 3, double silence = 1) => new(Child(),
    TimeSpan.FromSeconds(overall), TimeSpan.FromSeconds(overall), TimeSpan.FromSeconds(silence));

var framed = new WorkerLineReader(new MemoryStream(Encoding.UTF8.GetBytes("一\r\nsecond\n")));
Check(await framed.ReadAsync(default) == "一", "UTF-8 CRLF");
Check(await framed.ReadAsync(default) == "second", "coalesced next line retained");
Check(await framed.ReadAsync(default) is null, "clean EOF");
try { await new WorkerLineReader(new MemoryStream(Encoding.UTF8.GetBytes("incomplete"))).ReadAsync(default); throw new Exception("missing truncation failure"); }
catch (InvalidOperationException) { Check(true, "truncated EOF rejected"); }
try { await new WorkerLineReader(new MemoryStream(Encoding.UTF8.GetBytes("12345\n")), 4).ReadAsync(default); throw new Exception("missing bound failure"); }
catch (InvalidOperationException) { Check(true, "line size bounded"); }
using (var client = Client())
{
    var capture = new CaptureProgress();
    var result = await client.SendAsync("diagnose", new JsonObject { ["mode"] = "progress" }, capture);
    Check(result["done"]!.GetValue<bool>() && capture.Values.Count == 2, "real child progress and final response");
    Check((await client.SendAsync("initialize"))["done"]!.GetValue<bool>(), "subsequent request framed correctly");
    using var cancelled = new CancellationTokenSource(); cancelled.Cancel();
    try { await client.SendAsync("diagnose", cancellationToken: cancelled.Token); throw new Exception("missing cancel failure"); }
    catch (OperationCanceledException) { Check(client.IsUsable, "cancel before send leaves process untouched"); }
}
using (var client = Client())
{
    using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
    try { await client.SendAsync("diagnose", new JsonObject { ["mode"] = "hang" }, new CaptureProgress(), cancel.Token); throw new Exception("missing cancel failure"); }
    catch (OperationCanceledException) { Check(!client.IsUsable, "cancel aborts child and invalidates connection"); }
    try { await client.SendAsync("initialize"); throw new Exception("missing closed failure"); }
    catch (InvalidOperationException) { Check(true, "cancelled request not replayed"); }
}
using (var client = Client(overall: 2, silence: 0.3))
{
    var watch = Stopwatch.StartNew();
    try { await client.SendAsync("diagnose", new JsonObject { ["mode"] = "hang" }, new CaptureProgress()); throw new Exception("missing timeout"); }
    catch (TimeoutException) { Check(watch.Elapsed < TimeSpan.FromSeconds(2) && !client.IsUsable, "silence timeout kills unresponsive child"); }
}
using (var client = Client(overall: 0.6, silence: 0.4))
{
    try { await client.SendAsync("diagnose", new JsonObject { ["mode"] = "heartbeat" }, new CaptureProgress()); throw new Exception("missing timeout"); }
    catch (TimeoutException) { Check(!client.IsUsable, "overall timeout cannot be extended by progress"); }
}
using (var client = Client())
{
    try { await client.SendAsync("diagnose", new JsonObject { ["mode"] = "bad-progress" }, new CaptureProgress()); throw new Exception("missing invalid response"); }
    catch (InvalidOperationException error) { Check(!error.Message.Contains("private task") && !client.IsUsable, "invalid progress fails closed without private text"); }
}
Check(CollectionProgress.Parse(new JsonObject { ["phase"] = "collecting", ["completed"] = 2, ["total"] = 3,
    ["requests"] = 4, ["batches"] = 1, ["passes"] = 0 }).Completed == 2, "numeric progress parsed");
using (var client = Client(overall: 0.2, silence: 0.1))
{
    Check((await client.SendAsync("download_update", new JsonObject { ["mode"] = "delayed" }))["done"]!.GetValue<bool>(),
        "update operation may exceed the ordinary request deadline");
}
Console.WriteLine("SUCCESS");

sealed class CaptureProgress : IProgress<CollectionProgress>
{
    public List<CollectionProgress> Values { get; } = new();
    public void Report(CollectionProgress value) => Values.Add(value);
}
