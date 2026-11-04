using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Upcam.Desktop;

/// <summary>A private, serialized stdio protocol. Never log request or response bodies.</summary>
internal sealed class WorkerClient : IDisposable
{
    private readonly Process _process;
    private readonly SemaphoreSlim _requestGate = new(1, 1);
    private bool _disposed;
    private const int MaxResponseChars = 32 * 1024 * 1024;

    public WorkerClient(bool synthetic)
    {
        string executable = Path.Combine(AppContext.BaseDirectory, "worker", "UPCAMWorker.exe");
        if (!File.Exists(executable))
            throw new InvalidOperationException("缺少应用工作进程；请完整解压发行包，保留 worker 文件夹。");
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            WorkingDirectory = AppContext.BaseDirectory
        };
        if (synthetic) start.ArgumentList.Add("--smoke-test");
        start.Environment["UPCAM_APP_EXECUTABLE"] = Path.Combine(AppContext.BaseDirectory, "UPCAMTaskDigest.exe");
        _process = Process.Start(start) ?? throw new InvalidOperationException("无法启动本机工作进程。");
        // Drain stderr without persisting it: third-party errors can contain URLs or headers.
        _process.ErrorDataReceived += (_, _) => { };
        _process.BeginErrorReadLine();
    }

    public async Task<JsonObject> SendAsync(string operation, JsonObject? payload = null)
    {
        await _requestGate.WaitAsync();
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_process.HasExited) throw new InvalidOperationException("工作进程已退出；请重新打开应用。");
            string id = Guid.NewGuid().ToString("N");
            var request = new JsonObject { ["id"] = id, ["op"] = operation, ["payload"] = payload ?? new JsonObject() };
            await _process.StandardInput.WriteLineAsync(request.ToJsonString(new JsonSerializerOptions { WriteIndented = false }));
            await _process.StandardInput.FlushAsync();
            string? line = await ReadBoundedLineAsync(_process.StandardOutput.BaseStream);
            if (line is null) throw new InvalidOperationException("工作进程连接已关闭；本次结果需查看运行记录。");
            JsonObject response;
            try { response = JsonNode.Parse(line)?.AsObject() ?? throw new JsonException(); }
            catch (JsonException) { throw new InvalidOperationException("本机工作进程返回格式无效；未自动重试。"); }
            if (response["id"]?.GetValue<string>() != id)
                throw new InvalidOperationException("本机工作进程响应次序异常；未自动重试。");
            if (response["ok"]?.GetValue<bool>() != true)
                throw new InvalidOperationException(response["error"]?.GetValue<string>() ?? "操作未完成。");
            return response["result"]?.DeepClone().AsObject() ?? new JsonObject();
        }
        finally { _requestGate.Release(); }
    }

    internal static async Task<string?> ReadBoundedLineAsync(Stream stream)
    {
        using var result = new MemoryStream();
        byte[] buffer = new byte[8192];
        // Read bytes: a UTF-8 line can fill a pipe byte buffer without filling a
        // StreamReader character buffer. Detect LF before attempting another read.
        while (true)
        {
            int count = await stream.ReadAsync(buffer.AsMemory());
            if (count == 0)
            {
                if (result.Length == 0) return null;
                throw new InvalidOperationException("工作进程响应中断；未自动重试，请查看本机记录。");
            }
            int newline = Array.IndexOf(buffer, (byte)0x0A, 0, count);
            result.Write(buffer, 0, newline >= 0 ? newline : count);
            if (result.Length > MaxResponseChars)
                throw new InvalidOperationException("本机响应超过显示上限；未自动重试。请使用更小的诊断范围。");
            if (newline >= 0)
            {
                if (newline != count - 1)
                    throw new InvalidOperationException("工作进程返回了多余数据；未自动重试。");
                try { return new UTF8Encoding(false, true).GetString(result.ToArray()).TrimEnd('\r'); }
                catch (DecoderFallbackException)
                { throw new InvalidOperationException("工作进程响应编码无效；未自动重试。"); }
            }
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try { _process.StandardInput.Close(); } catch (InvalidOperationException) { }
        try { if (!_process.HasExited) _process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        _process.Dispose();
    }
}
