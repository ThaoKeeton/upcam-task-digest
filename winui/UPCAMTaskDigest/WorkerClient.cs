using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Upcam.Desktop;

/// <summary>A serialized private stdio protocol. Never log request or response bodies.</summary>
internal sealed class WorkerClient : IDisposable
{
    private readonly Process _process;
    private readonly WorkerLineReader _reader;
    private readonly SemaphoreSlim _requestGate = new(1, 1);
    private readonly TimeSpan _ordinaryDeadline, _collectionDeadline, _silenceDeadline;
    private int _broken, _disposed;
    internal bool IsUsable => Volatile.Read(ref _disposed) == 0 && Volatile.Read(ref _broken) == 0 && !_process.HasExited;

    public WorkerClient(bool synthetic) : this(CreateStartInfo(synthetic), TimeSpan.FromSeconds(30),
        TimeSpan.FromMinutes(125), TimeSpan.FromMinutes(11)) { }

    internal WorkerClient(ProcessStartInfo start, TimeSpan ordinaryDeadline, TimeSpan collectionDeadline, TimeSpan silenceDeadline)
    {
        _ordinaryDeadline = ordinaryDeadline; _collectionDeadline = collectionDeadline; _silenceDeadline = silenceDeadline;
        _process = Process.Start(start) ?? throw new InvalidOperationException("无法启动本机工作进程。");
        _reader = new WorkerLineReader(_process.StandardOutput.BaseStream);
        // Third-party failures can contain URLs and headers. Drain without persisting.
        _process.ErrorDataReceived += (_, _) => { };
        _process.BeginErrorReadLine();
    }

    private static ProcessStartInfo CreateStartInfo(bool synthetic)
    {
        string executable = Path.Combine(AppContext.BaseDirectory, "worker", "UPCAMWorker.exe");
        if (!File.Exists(executable))
            throw new InvalidOperationException("缺少应用工作进程；请完整解压发行包，保留 worker 文件夹。");
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = new UTF8Encoding(false),
            WorkingDirectory = AppContext.BaseDirectory
        };
        if (synthetic) start.ArgumentList.Add("--smoke-test");
        start.Environment["UPCAM_APP_EXECUTABLE"] = Path.Combine(AppContext.BaseDirectory, "UPCAMTaskDigest.exe");
        return start;
    }

    public async Task<JsonObject> SendAsync(string operation, JsonObject? payload = null,
        IProgress<CollectionProgress>? progress = null, CancellationToken cancellationToken = default)
    {
        // Cancellation before admission must not stop another admitted request.
        await _requestGate.WaitAsync(cancellationToken);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!IsUsable) throw new InvalidOperationException("工作进程已退出；请重新打开应用。");
            string id = Guid.NewGuid().ToString("N");
            bool observe = operation is "diagnose" or "save" && progress is not null;
            var request = new JsonObject { ["id"] = id, ["op"] = operation, ["payload"] = payload ?? new JsonObject() };
            if (observe) request["progress"] = true;
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            deadline.CancelAfter(IsUpdate(operation) ? TimeSpan.FromMinutes(6) : IsCollection(operation) ? _collectionDeadline : _ordinaryDeadline);
            using var abort = deadline.Token.Register(Abort);
            try
            {
                await _process.StandardInput.WriteLineAsync(request.ToJsonString(new JsonSerializerOptions { WriteIndented = false }).AsMemory(), deadline.Token);
                await _process.StandardInput.FlushAsync(deadline.Token);
                while (true)
                {
                    using var silence = CancellationTokenSource.CreateLinkedTokenSource(deadline.Token);
                    if (observe) silence.CancelAfter(_silenceDeadline);
                    using var abortSilent = silence.Token.Register(Abort);
                    string? line = await _reader.ReadAsync(silence.Token);
                    if (line is null) throw new InvalidOperationException("工作进程连接已关闭；本次结果需查看运行记录。");
                    JsonObject response;
                    try { response = JsonNode.Parse(line)?.AsObject() ?? throw new JsonException(); }
                    catch (Exception) { throw new InvalidOperationException("本机工作进程返回格式无效；未自动重试。"); }
                    if (response["id"]?.GetValue<string>() != id)
                        throw new InvalidOperationException("本机工作进程响应次序异常；未自动重试。");
                    if (response["kind"] is not null)
                    {
                        if (!observe || response.Count != 3 || response["kind"]!.GetValue<string>() != "progress" ||
                            response["progress"] is not JsonObject status)
                            throw new InvalidOperationException("本机采集进度格式无效；未自动重试。");
                        progress!.Report(CollectionProgress.Parse(status));
                        continue;
                    }
                    if (_reader.HasBufferedData)
                        throw new InvalidOperationException("工作进程返回了多余数据；未自动重试。");
                    deadline.Token.ThrowIfCancellationRequested();
                    if (response["ok"]?.GetValue<bool>() != true)
                        throw new OperationRejectedException(response["error"]?.GetValue<string>() ?? "操作未完成。");
                    return response["result"]?.DeepClone().AsObject() ?? new JsonObject();
                }
            }
            catch (OperationRejectedException error) { throw new InvalidOperationException(error.Message); }
            catch (Exception) when (cancellationToken.IsCancellationRequested)
            { Abort(); throw new OperationCanceledException("只读诊断已停止；未调用 OpenAI、未发送钉钉。", cancellationToken); }
            catch (Exception) when (Volatile.Read(ref _broken) != 0)
            { throw new TimeoutException("工作进程等待超时，已停止本次操作；未自动重试。若操作涉及发送，请先检查运行记录。"); }
            catch (Exception) { Abort(); throw; }
        }
        finally { _requestGate.Release(); }
    }

    private static bool IsCollection(string operation) => operation is "save" or "run_manual" or "run_due" or
        "probe_project" or "diagnose" or "export_detailed_diagnostic_to_file" or "test_teambition";

    private static bool IsUpdate(string operation) => operation is "check_updates" or "download_update" or "prepare_update" or "finish_update";

    private void Abort()
    {
        if (Interlocked.Exchange(ref _broken, 1) != 0) return;
        try { if (!_process.HasExited) _process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
    }

    internal async Task StopAsync()
    {
        Abort();
        await _process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        Abort();
        _process.Dispose();
    }

    private sealed class OperationRejectedException(string message) : Exception(message);
}
