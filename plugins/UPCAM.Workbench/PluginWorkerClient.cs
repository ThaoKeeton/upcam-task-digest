using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32.SafeHandles;
using Upcam.Desktop;

namespace Upcam.Workbench;

/// <summary>
/// Owns one explicitly bound backend session. Protocol bodies and child stderr
/// must never become diagnostics. A broken request is never replayed.
/// </summary>
internal sealed class PluginWorkerClient : IAsyncDisposable
{
    private const string SingletonName = @"Local\UPCAMTaskDigest.Singleton.v1";
    private const string Unavailable = "The UPCAM backend connection is closed. Check local run history before opening a new session.";
    private const string InvalidResponse = "The UPCAM backend returned an invalid response. Check local run history before trying again.";
    private const string OperationFailed = "The UPCAM backend could not complete this operation. Check local configuration and run history.";
    private const string Cancelled = "The UPCAM backend request was cancelled. Check local run history before trying again.";
    private const string TimedOut = "The UPCAM backend request timed out. Check local run history before trying again.";
    private const int MaxRequestBytes = 2 * 1024 * 1024;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);
    private static readonly TimeSpan OrdinaryDeadline = TimeSpan.FromSeconds(30);
    private static readonly TimeSpan CollectionDeadline = TimeSpan.FromMinutes(125);
    private static readonly TimeSpan ShutdownDeadline = TimeSpan.FromSeconds(2);
    // Exact fixed literals from the shared business boundary. Returning the
    // local literal keeps useful setup guidance without trusting child text.
    private static readonly string[] SafeOperationMessages =
    [
        "请先填写 Tenant ID 和 Project ID。",
        "请先保存 Tenant ID 和 Project ID。",
        "请先保存 Teambition App ID 和 App Secret。",
        "请先保存 OpenAI API Key，再刷新模型。",
        "只读诊断需要已保存的 Teambition App ID 和 App Secret；不需要 OpenAI 或钉钉密钥。",
        "成员或状态映射无效；请核对 ID、姓名和已解决状态。",
        "成员映射内容无效；未导入任何行。",
        "成员映射格式无效。",
        "状态映射格式无效。",
        "项目筛选字段无效。",
        "项目筛选条件格式无效。",
        "请输入项目筛选字段值或 ID。",
        "项目范围设置无效；未保存任何更改。",
        "项目筛选条件无效；未保存任何更改。",
        "请先保存项目范围配置并完成自动只读核验；核验未通过时不会发送。可在项目范围页选择有动态的时间区间后重新保存。",
        "启用自动运行前，请保存项目配置并通过自动只读核验。",
        "启用自动运行前，请保存 Teambition、钉钉、OpenAI 凭据和防重复身份。",
        "尚未保存启用自动发送；请先在设置中明确开启并保存。",
        "自动运行授权请求格式无效；未采集或发送。",
        "自动运行授权已改变；请重新保存设置并明确启动。",
        "自动运行期间不能修改已核验的设置或凭据；未保存任何更改。",
        "模型缓存未匹配。若在更换 Key，请先在“自动运行”关闭并保存，再保存新 Key、刷新模型，最后重新开启自动运行。",
        "所选模型不在当前 API Key 的缓存列表中。",
        "当前模型没有已核验的所选 reasoning effort。",
        "当前模型未核验支持 Fast 服务等级，请选择 Standard。",
        "OpenAI 模型列表刷新失败；原缓存保持不变。检查网络、Key 权限后再试。",
        "返回的模型缓存与当前 API Key 不匹配；原选择未更改。",
        "发送防重账本或本机身份缺失；为避免重复发送，自动运行已停用。",
        "本机发送防重账本无法验证；自动运行已停用。",
        "发送结果不确定或未获确认；请先核实钉钉记录，勿重复发送。",
        "该时间范围仍在发送或结果不确定；请先核实钉钉记录，勿重复发送。",
        "请先保存钉钉 Webhook 和签名密钥。",
        "钉钉 Webhook 或签名密钥格式无效；没有发送测试消息。",
        "钉钉 Webhook 或签名密钥格式无效。",
        "Teambition 只读连接测试失败；请检查本机网络、凭据和项目读取权限。",
        "Teambition 只读查询未完成或翻页不完整；没有更改核验状态。请检查权限后再试。",
        "项目设置或时间范围无效；没有更改核验状态。",
        "已有日报或诊断任务正在运行；当前操作未并行执行。",
        "采集正在进行，暂不能清除进度；请等待完成或退出应用后重试。",
        "本地采集进度无法安全清除；请退出应用后重试。",
        "设置内容无效；未保存任何更改。",
        "设置内容超出本机配置大小限制；未保存任何更改。",
        "保存选项无效；未保存任何更改。",
        "密钥字段无效；未保存任何更改。",
        "手动发送请求包含不支持的选项；未发送消息。",
        "没有找到所选运行记录。",
        "请选择有效的本机 JSON 保存路径。",
        "请选择有效的本机 JSON 文件路径。",
        "请选择应用数据目录及 Git 仓库之外的本机 JSON 文件，避免覆盖配置或提交项目内容。",
        "无法写入所选文件，请检查目标文件夹权限和可用空间。",
        "详细诊断包含项目内容，请先在界面明确选择该导出方式。"
    ];

    private readonly OwnedWorker _worker;
    private readonly Mutex _singleton;
    private readonly CancellationToken _lifetime;
    private readonly CancellationTokenRegistration _lifetimeRegistration;
    private readonly SemaphoreSlim _requestGate = new(1, 1);
    private readonly WorkerLineReader _reader;
    private readonly CancellationTokenSource _stop = new();
    private readonly Task _cleanup;
    private readonly object _disposeLock = new();
    private Task? _disposeTask;
    // 0 = usable, 1 = broken, 2 = disposing, 3 = disposed.
    private int _state;

    private PluginWorkerClient(OwnedWorker worker, Mutex singleton, CancellationToken lifetime)
    {
        _worker = worker;
        _reader = new WorkerLineReader(worker.Output);
        _singleton = singleton;
        _lifetime = lifetime;
        _cleanup = ObserveExitAsync();
        _lifetimeRegistration = lifetime.Register(static state => ((PluginWorkerClient)state!).BreakConnection(), this);
    }

    internal bool IsUsable => Volatile.Read(ref _state) == 0 && !_worker.HasExited;

    /// <summary>The token owns both startup and the returned session lifetime.</summary>
    public static async Task<PluginWorkerClient> StartAsync(string packageDirectory, string dataDirectory,
        bool synthetic, CancellationToken cancellationToken)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException("The UPCAM backend requires Windows.");
        if (cancellationToken.IsCancellationRequested)
            throw new OperationCanceledException(Cancelled, cancellationToken);

        string package = CanonicalizeDirectory(packageDirectory, mustExist: true);
        string profile = CanonicalizeDirectory(dataDirectory);
        string executable = ValidateLocalPath(Path.Combine(package, "worker", "UPCAMPluginWorker.exe"),
            directory: false, mustExist: true);
        if (!executable.StartsWith(package + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("The UPCAM worker must be contained in its plugin package.");

        Mutex? singleton = null;
        OwnedWorker? worker = null;
        PluginWorkerClient? client = null;
        try
        {
            // Standalone 0.4.6 uses the existence of this named kernel object,
            // rather than thread-affine WaitOne/ReleaseMutex ownership.
            singleton = new Mutex(false, SingletonName, out bool createdNew);
            if (!createdNew)
                throw new InvalidOperationException("Another UPCAM session is already running. Close it before opening the plugin backend.");
            worker = OwnedWorker.Start(executable, package, profile, synthetic, singleton, cancellationToken);
            client = new PluginWorkerClient(worker, singleton, cancellationToken);
            worker = null;
            singleton = null;

            string nonce = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
            JsonObject handshake = await client.SendAsync("plugin_handshake", new JsonObject
            {
                ["protocol_version"] = 1, ["backend_version"] = "0.4.6", ["nonce"] = nonce
            }, cancellationToken).ConfigureAwait(false);
            if (!ValidHandshake(handshake, nonce, profile, synthetic))
                throw new InvalidOperationException("The UPCAM backend handshake did not match the requested session.");
            if (cancellationToken.IsCancellationRequested)
                throw new OperationCanceledException(Cancelled, cancellationToken);
            if (!client.IsUsable) throw new InvalidOperationException(Unavailable);
            return client;
        }
        catch (Exception error)
        {
            if (client is not null)
            {
                client.BreakConnection();
                await client.DisposeAsync().ConfigureAwait(false);
            }
            else
            {
                if (worker is not null)
                {
                    worker.Terminate();
                    await worker.WaitForTreeExitAsync().ConfigureAwait(false);
                    worker.Dispose();
                }
                singleton?.Dispose();
            }
            if (cancellationToken.IsCancellationRequested)
                throw new OperationCanceledException(Cancelled, cancellationToken);
            // Only our own fixed diagnostics may cross this boundary.
            if (error is InvalidOperationException && (error.Message == InvalidResponse ||
                error.Message == Unavailable || error.Message == OperationFailed ||
                error.Message == "The UPCAM backend handshake did not match the requested session." ||
                error.Message == "Another UPCAM session is already running. Close it before opening the plugin backend."))
                throw;
            if (error is TimeoutException) throw new TimeoutException(TimedOut);
            throw new InvalidOperationException("The UPCAM backend could not be started safely. Check the complete plugin package and profile binding.");
        }
    }

    public async Task<JsonObject> SendAsync(string operation, JsonObject? payload = null,
        CancellationToken cancellationToken = default, IProgress<CollectionProgress>? progress = null)
    {
        try { await _requestGate.WaitAsync(cancellationToken).ConfigureAwait(false); }
        catch (OperationCanceledException)
        {
            BreakConnection();
            await _cleanup.ConfigureAwait(false);
            throw new OperationCanceledException(Cancelled, cancellationToken);
        }
        try
        {
            if (!IsUsable) throw new InvalidOperationException(Unavailable);
            return await ExchangeAsync(operation, payload, cancellationToken,
                IsCollection(operation) ? CollectionDeadline : OrdinaryDeadline, includeLifetime: true, progress).ConfigureAwait(false);
        }
        finally { _requestGate.Release(); }
    }

    private async Task<JsonObject> ExchangeAsync(string operation, JsonObject? payload, CancellationToken caller,
        TimeSpan timeout, bool includeLifetime, IProgress<CollectionProgress>? progress = null)
    {
        string id = Guid.NewGuid().ToString("N");
        byte[] request;
        try
        {
            if (string.IsNullOrWhiteSpace(operation) || operation.Length > 128)
                throw new ArgumentException();
            var body = new JsonObject { ["id"] = id, ["op"] = operation, ["payload"] = payload?.DeepClone() ?? new JsonObject() };
            if ((operation is "diagnose" or "save") && progress is not null) body["progress"] = true;
            request = JsonSerializer.SerializeToUtf8Bytes(body);
        }
        catch (Exception) { throw new InvalidOperationException("The UPCAM backend request is invalid."); }
        if (request.Length > MaxRequestBytes)
            throw new InvalidOperationException("The UPCAM backend request exceeds the supported size.");

        using var deadline = includeLifetime
            ? CancellationTokenSource.CreateLinkedTokenSource(caller, _lifetime, _stop.Token)
            : CancellationTokenSource.CreateLinkedTokenSource(caller);
        deadline.CancelAfter(timeout);
        using var abort = deadline.Token.Register(static state => ((PluginWorkerClient)state!).BreakConnection(), this);
        bool silenceExpired = false;
        try
        {
            deadline.Token.ThrowIfCancellationRequested();
            await _worker.Input.WriteAsync(request, deadline.Token).ConfigureAwait(false);
            await _worker.Input.WriteAsync(new byte[] { 0x0a }, deadline.Token).ConfigureAwait(false);
            await _worker.Input.FlushAsync(deadline.Token).ConfigureAwait(false);
            while (true)
            {
                using var silence = CancellationTokenSource.CreateLinkedTokenSource(deadline.Token);
                if ((operation is "diagnose" or "save") && progress is not null) silence.CancelAfter(TimeSpan.FromMinutes(11));
                using var abortSilent = silence.Token.Register(() =>
                {
                    if (!deadline.IsCancellationRequested) Volatile.Write(ref silenceExpired, true);
                    BreakConnection();
                });
                string line = await _reader.ReadAsync(silence.Token).ConfigureAwait(false) ?? throw new ProtocolFaultException();
                using var document = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 64 });
                ValidateMembers(document.RootElement);
                if (document.RootElement.TryGetProperty("kind", out var kind))
                {
                    if (operation is not ("diagnose" or "save") || progress is null || kind.GetString() != "progress" ||
                        document.RootElement.EnumerateObject().Count() != 3 ||
                        document.RootElement.GetProperty("id").GetString() != id)
                        throw new ProtocolFaultException();
                    progress.Report(CollectionProgress.Parse(JsonNode.Parse(document.RootElement.GetProperty("progress").GetRawText())!.AsObject()));
                    continue;
                }
                if (_reader.HasBufferedData) throw new ProtocolFaultException();
                var response = DecodeResponse(line, id);
                deadline.Token.ThrowIfCancellationRequested();
                if (response.Result is null) throw new OperationRejectedException(response.Error ?? OperationFailed);
                return response.Result;
            }
        }
        catch (OperationRejectedException rejected)
        {
            // Business validation does not invalidate an otherwise sound stream.
            throw new InvalidOperationException(rejected.Message);
        }
        catch (Exception)
        {
            BreakConnection();
            await _cleanup.ConfigureAwait(false);
            if (caller.IsCancellationRequested || (includeLifetime && (_lifetime.IsCancellationRequested || _stop.IsCancellationRequested)))
                throw new OperationCanceledException(Cancelled,
                    caller.IsCancellationRequested ? caller : includeLifetime && _lifetime.IsCancellationRequested ? _lifetime : _stop.Token);
            if (deadline.IsCancellationRequested || Volatile.Read(ref silenceExpired)) throw new TimeoutException(TimedOut);
            throw new InvalidOperationException(InvalidResponse);
        }
    }

    // Saving an unverified scope performs the same complete read-only collection.
    private static bool IsCollection(string operation) => operation is "save" or "run_manual" or "run_due" or "plugin_run_due_authorized" or "probe_project" or
        "diagnose" or "export_diagnostic" or "export_diagnostic_to_file" or "export_detailed_diagnostic_to_file" or "test_teambition";

    private static bool ValidHandshake(JsonObject response, string nonce, string profile, bool synthetic)
    {
        try
        {
            string? revision = response["ruleset_revision"]?.GetValue<string>();
            return response["protocol_version"]?.GetValue<int>() == 1 &&
                response["backend_version"]?.GetValue<string>() == "0.4.6" &&
                response["nonce"]?.GetValue<string>() == nonce &&
                response["mode"]?.GetValue<string>() == "project-workbench" &&
                string.Equals(response["data_directory"]?.GetValue<string>(), profile, StringComparison.OrdinalIgnoreCase) &&
                response["synthetic"]?.GetValue<bool>() == synthetic &&
                !string.IsNullOrWhiteSpace(revision) && revision.Length <= 256 && !revision.Any(char.IsControl);
        }
        catch (Exception) { return false; }
    }

    private static (JsonObject? Result, string? Error) DecodeResponse(string line, string expectedId)
    {
        using JsonDocument document = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 64 });
        JsonElement root = document.RootElement;
        ValidateMembers(root);
        if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != 3 ||
            !root.TryGetProperty("id", out var id) || id.ValueKind != JsonValueKind.String || id.GetString() != expectedId ||
            !root.TryGetProperty("ok", out var ok) || ok.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            throw new ProtocolFaultException();
        if (ok.GetBoolean())
        {
            if (!root.TryGetProperty("result", out var result) || result.ValueKind != JsonValueKind.Object)
                throw new ProtocolFaultException();
            return (JsonNode.Parse(result.GetRawText())!.AsObject(), null);
        }
        if (!root.TryGetProperty("error", out var error) || error.ValueKind != JsonValueKind.String)
            throw new ProtocolFaultException();
        string? childMessage = error.GetString();
        string safeMessage = childMessage switch
        {
            "Use YYYY-MM-DD HH:MM for both collection boundaries" => "请使用 YYYY-MM-DD HH:MM 填写两个采集边界。",
            "Collection end must be after collection start" => "采集结束时间必须晚于开始时间。",
            _ => SafeOperationMessages.FirstOrDefault(message => string.Equals(message, childMessage, StringComparison.Ordinal)) ?? OperationFailed
        };
        return (null, safeMessage);
    }

    private static void ValidateMembers(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var member in element.EnumerateObject())
            {
                if (!names.Add(member.Name)) throw new ProtocolFaultException();
                ValidateMembers(member.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
            foreach (var item in element.EnumerateArray()) ValidateMembers(item);
    }

    private void BreakConnection()
    {
        Interlocked.CompareExchange(ref _state, 1, 0);
        _worker.Terminate();
    }

    private async Task ObserveExitAsync()
    {
        await _worker.WaitForTreeExitAsync().ConfigureAwait(false);
        Interlocked.CompareExchange(ref _state, 1, 0);
        try { _worker.Dispose(); }
        finally
        {
            // The native job reports zero active processes before this handle closes.
            // Worker also inherits a singleton handle, covering an abrupt host death.
            _singleton.Dispose();
        }
    }

    public ValueTask DisposeAsync()
    {
        lock (_disposeLock) return new ValueTask(_disposeTask ??= DisposeCoreAsync());
    }

    private async Task DisposeCoreAsync()
    {
        int previous = Interlocked.Exchange(ref _state, 2);
        bool acquired = _requestGate.Wait(0);
        if (!acquired)
        {
            _stop.Cancel();
            BreakConnection();
            await _requestGate.WaitAsync().ConfigureAwait(false);
        }
        try
        {
            if (acquired && previous == 0 && !_worker.HasExited)
            {
                try { await ExchangeAsync("shutdown", null, CancellationToken.None, ShutdownDeadline, includeLifetime: false).ConfigureAwait(false); }
                catch (Exception) { /* Shutdown has an independent deadline; cleanup is unconditional. */ }
            }
        }
        finally
        {
            BreakConnection();
            _requestGate.Release();
            await _cleanup.ConfigureAwait(false);
            _lifetimeRegistration.Dispose();
            Volatile.Write(ref _state, 3);
        }
    }

    /// <summary>Shared with profile binding so Windows short aliases share draft and worker identity.</summary>
    internal static string CanonicalizeDirectory(string path, bool mustExist = false) =>
        ValidateLocalPath(path, directory: true, mustExist: mustExist);

    private static string ValidateLocalPath(string value, bool directory, bool mustExist, bool expandAliases = true)
    {
        const string invalidPath = "The UPCAM package and profile must use contained, absolute local paths without links or reparse points.";
        try
        {
            if (string.IsNullOrWhiteSpace(value) || !Path.IsPathFullyQualified(value) || value.StartsWith('\\'))
                throw new IOException();
            string full = Path.GetFullPath(value).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string? root = Path.GetPathRoot(full);
            if (root is null || root.Length != 3 || root[1] != ':' || full.Length <= root.Length ||
                full.AsSpan(2).Contains(':') || Native.GetDriveTypeW(root) is not (2 or 3 or 5 or 6))
                throw new IOException();
            // Windows strips trailing dots/spaces in ordinary paths. Reject these
            // aliases before they can disguise a checked component.
            string[] segments = value[root.Length..].Split(['\\', '/'], StringSplitOptions.RemoveEmptyEntries);
            if (segments.Any(segment => segment is "." or ".." || segment.EndsWith('.') || segment.EndsWith(' ') ||
                segment.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || IsDeviceName(segment)))
                throw new IOException();
            string current = root;
            string[] components = full[root.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries);
            for (int index = 0; index < components.Length; index++)
            {
                string parent = current;
                current = Path.Combine(current, components[index]);
                FileAttributes attributes;
                try { attributes = File.GetAttributes(current); }
                catch (Exception error) when (!mustExist && directory &&
                    (error is FileNotFoundException or DirectoryNotFoundException))
                {
                    if (!expandAliases) return full;
                    // GetLongPathName requires an existing path. Expand the last
                    // verified regular ancestor, then retain each new component.
                    string canonical = ExpandExistingPath(parent);
                    for (int remaining = index; remaining < components.Length; remaining++)
                        canonical = Path.Combine(canonical, components[remaining]);
                    return ValidateLocalPath(canonical, directory, mustExist, expandAliases: false);
                }
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException();
                bool isDirectory = (attributes & FileAttributes.Directory) != 0;
                if ((index < components.Length - 1 || directory) != isDirectory) throw new IOException();
            }
            // Validate the expanded spelling too; resolving an alias never
            // exempts a component from containment/local/reparse checks.
            return expandAliases ? ValidateLocalPath(ExpandExistingPath(full), directory, mustExist, expandAliases: false) : full;
        }
        catch (Exception) { throw new InvalidOperationException(invalidPath); }
    }

    private static string ExpandExistingPath(string path)
    {
        var buffer = new StringBuilder(Math.Max(512, path.Length + 1));
        while (true)
        {
            uint length = Native.GetLongPathNameW(path, buffer, (uint)buffer.Capacity);
            if (length == 0 || length > 32768) throw new IOException();
            if (length < buffer.Capacity) return Path.TrimEndingDirectorySeparator(buffer.ToString());
            buffer = new StringBuilder(checked((int)length));
        }
    }

    private static bool IsDeviceName(string segment)
    {
        string stem = segment.Split('.')[0].ToUpperInvariant();
        return stem is "CON" or "PRN" or "AUX" or "NUL" or "CLOCK$" ||
            (stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) || stem.StartsWith("LPT", StringComparison.Ordinal)) &&
                stem[3] is >= '1' and <= '9');
    }

    private sealed class ProtocolFaultException : Exception { }
    private sealed class OperationRejectedException(string message) : Exception(message) { }

    /// <summary>All handles refer exclusively to the one child/job created here.</summary>
    private sealed class OwnedWorker : IDisposable
    {
        private readonly Native.KernelHandle _process;
        private readonly Native.KernelHandle _job;
        private readonly FileStream _error;
        private readonly Task _errorDrain;
        private readonly Task _exit;
        private int _disposed;
        public FileStream Input { get; }
        public FileStream Output { get; }
        public bool HasExited
        {
            get
            {
                if (Volatile.Read(ref _disposed) != 0) return true;
                try { return Native.WaitForSingleObject(_process, 0) == 0; }
                catch (ObjectDisposedException) { return true; }
            }
        }

        private OwnedWorker(Native.KernelHandle process, Native.KernelHandle job, FileStream input,
            FileStream output, FileStream error)
        {
            _process = process; _job = job; Input = input; Output = output; _error = error;
            _errorDrain = DrainErrorAsync();
            _exit = WaitForProcessAsync();
        }

        public static OwnedWorker Start(string executable, string package, string profile, bool synthetic,
            Mutex singleton, CancellationToken cancellationToken)
        {
            Native.KernelHandle? job = null;
            Native.KernelHandle? process = null;
            FileStream? input = null, output = null, error = null;
            IntPtr attributes = IntPtr.Zero, handleList = IntPtr.Zero, jobList = IntPtr.Zero;
            bool attributesInitialized = false;
            var security = new Native.SecurityAttributes { Length = Marshal.SizeOf<Native.SecurityAttributes>(), InheritHandle = true };
            if (!Native.CreatePipe(out var childInput, out var parentInput, ref security, 0)) throw new InvalidOperationException();
            using (childInput) using (parentInput)
            {
                if (!Native.CreatePipe(out var parentOutput, out var childOutput, ref security, 0)) throw new InvalidOperationException();
                using (parentOutput) using (childOutput)
                {
                    if (!Native.CreatePipe(out var parentError, out var childError, ref security, 0)) throw new InvalidOperationException();
                    using (parentError) using (childError)
                    {
                        try
                        {
                            if (!Native.SetHandleInformation(parentInput, 1, 0) || !Native.SetHandleInformation(parentOutput, 1, 0) ||
                                !Native.SetHandleInformation(parentError, 1, 0)) throw new InvalidOperationException();
                            job = Native.CreateJobObjectW(IntPtr.Zero, null);
                            if (job.IsInvalid) throw new InvalidOperationException();
                            var limits = new Native.JobExtendedLimits();
                            limits.BasicLimitInformation.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                            if (!Native.SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf<Native.JobExtendedLimits>()))
                                throw new InvalidOperationException();
                            if (!Native.DuplicateHandle(Native.GetCurrentProcess(), singleton.SafeWaitHandle,
                                Native.GetCurrentProcess(), out var inheritedSingleton, 0, true, 2)) throw new InvalidOperationException();
                            using (inheritedSingleton)
                            {
                                IntPtr bytes = IntPtr.Zero;
                                Native.InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref bytes);
                                if (bytes == IntPtr.Zero) throw new InvalidOperationException();
                                attributes = Marshal.AllocHGlobal(bytes);
                                if (!Native.InitializeProcThreadAttributeList(attributes, 2, 0, ref bytes)) throw new InvalidOperationException();
                                attributesInitialized = true;
                                IntPtr[] inherited = [childInput.DangerousGetHandle(), childOutput.DangerousGetHandle(),
                                    childError.DangerousGetHandle(), inheritedSingleton.DangerousGetHandle()];
                                handleList = Marshal.AllocHGlobal(IntPtr.Size * inherited.Length);
                                Marshal.Copy(inherited, 0, handleList, inherited.Length);
                                if (!Native.UpdateProcThreadAttribute(attributes, 0, (IntPtr)0x20002, handleList,
                                    (IntPtr)(IntPtr.Size * inherited.Length), IntPtr.Zero, IntPtr.Zero)) throw new InvalidOperationException();
                                // Assign the job atomically during CreateProcess (Windows 10+).
                                // There is no unowned interval even if the host dies at startup.
                                jobList = Marshal.AllocHGlobal(IntPtr.Size);
                                Marshal.WriteIntPtr(jobList, job.DangerousGetHandle());
                                if (!Native.UpdateProcThreadAttribute(attributes, 0, (IntPtr)0x2000d, jobList,
                                    (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero)) throw new InvalidOperationException();
                                var startup = new Native.StartupInfoEx
                                {
                                    StartupInfo = new Native.StartupInfo
                                    {
                                        Size = Marshal.SizeOf<Native.StartupInfoEx>(), Flags = 0x100,
                                        StandardInput = childInput.DangerousGetHandle(), StandardOutput = childOutput.DangerousGetHandle(),
                                        StandardError = childError.DangerousGetHandle()
                                    },
                                    AttributeList = attributes
                                };
                                var command = new StringBuilder(QuoteArgument(executable) + " --data-directory " + QuoteArgument(profile) +
                                    (synthetic ? " --synthetic" : ""));
                                cancellationToken.ThrowIfCancellationRequested();
                                if (!Native.CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                                    0x08000000 | 0x00080000 | 0x00000400 | 0x00000004, IntPtr.Zero, package, ref startup, out var created))
                                    throw new InvalidOperationException();
                                process = new Native.KernelHandle(created.Process);
                                using var thread = new Native.KernelHandle(created.Thread);
                                input = TakeStream(parentInput, FileAccess.Write);
                                output = TakeStream(parentOutput, FileAccess.Read);
                                error = TakeStream(parentError, FileAccess.Read);
                                cancellationToken.ThrowIfCancellationRequested();
                                if (Native.ResumeThread(thread) == uint.MaxValue) throw new InvalidOperationException();
                                var owned = new OwnedWorker(process, job, input, output, error);
                                process = null; job = null; input = null; output = null; error = null;
                                return owned;
                            }
                        }
                        finally
                        {
                            if (process is not null && !process.IsInvalid)
                            {
                                if (job is not null && !job.IsInvalid) Native.TerminateJobObject(job, 1);
                                Native.TerminateProcess(process, 1);
                                Native.WaitForSingleObject(process, uint.MaxValue);
                                if (job is not null && !job.IsInvalid)
                                {
                                    while (Native.QueryInformationJobObject(job, 1, out var accounting,
                                        Marshal.SizeOf<Native.JobAccounting>(), IntPtr.Zero) && accounting.ActiveProcesses != 0)
                                        Thread.Sleep(10);
                                }
                            }
                            input?.Dispose(); output?.Dispose(); error?.Dispose(); process?.Dispose(); job?.Dispose();
                            if (attributesInitialized) Native.DeleteProcThreadAttributeList(attributes);
                            if (attributes != IntPtr.Zero) Marshal.FreeHGlobal(attributes);
                            if (handleList != IntPtr.Zero) Marshal.FreeHGlobal(handleList);
                            if (jobList != IntPtr.Zero) Marshal.FreeHGlobal(jobList);
                        }
                    }
                }
            }
        }

        private static FileStream TakeStream(SafeFileHandle original, FileAccess access)
        {
            var transferred = new SafeFileHandle(original.DangerousGetHandle(), ownsHandle: true);
            original.SetHandleAsInvalid();
            try { return new FileStream(transferred, access, 4096, isAsync: false); }
            catch { transferred.Dispose(); throw; }
        }

        private static string QuoteArgument(string value)
        {
            // Windows command-line quoting, including trailing backslashes.
            var result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                if (character == '"') result.Append('\\', slashes * 2 + 1).Append('"');
                else result.Append('\\', slashes).Append(character);
                slashes = 0;
            }
            return result.Append('\\', slashes * 2).Append('"').ToString();
        }

        public void Terminate()
        {
            if (Volatile.Read(ref _disposed) != 0) return;
            try
            {
                if (!Native.TerminateJobObject(_job, 1)) Native.TerminateProcess(_process, 1);
            }
            catch (ObjectDisposedException) { }
        }

        private async Task WaitForProcessAsync()
        {
            using var wait = new Native.ProcessWaitHandle(_process);
            var completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            RegisteredWaitHandle registration = ThreadPool.RegisterWaitForSingleObject(wait,
                static (state, _) => ((TaskCompletionSource)state!).TrySetResult(), completion, Timeout.Infinite, executeOnlyOnce: true);
            try { await completion.Task.ConfigureAwait(false); }
            finally { registration.Unregister(null); }
        }

        public async Task WaitForTreeExitAsync()
        {
            await _exit.ConfigureAwait(false);
            // Natural root exit must also end any child that retained pipe handles.
            Terminate();
            while (true)
            {
                if (!Native.QueryInformationJobObject(_job, 1, out var accounting,
                    Marshal.SizeOf<Native.JobAccounting>(), IntPtr.Zero))
                    throw new InvalidOperationException("The UPCAM backend process cleanup could not be confirmed.");
                if (accounting.ActiveProcesses == 0) break;
                await Task.Delay(10).ConfigureAwait(false);
            }
        }

        private async Task DrainErrorAsync()
        {
            // Raw bytes: neither invalid text nor an unbounded line can allocate
            // an unbounded string. Nothing is logged, retained or surfaced.
            byte[] buffer = new byte[8192];
            try { while (await _error.ReadAsync(buffer.AsMemory()).ConfigureAwait(false) > 0) { } }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
            // Flushing a cancelled pipe can fail after the child has exited.
            // Continue closing the other owned handles and singleton in that case.
            ClosePipe(Input); ClosePipe(Output); ClosePipe(_error);
            _process.Dispose(); _job.Dispose();
            // Observe completion without synchronously blocking a pipe reader.
            _ = _errorDrain.Exception;
        }

        private static void ClosePipe(Stream stream)
        {
            try { stream.Dispose(); }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
        }
    }

    private static class Native
    {
        [StructLayout(LayoutKind.Sequential)] internal struct SecurityAttributes
        { public int Length; public IntPtr SecurityDescriptor; [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle; }
        [StructLayout(LayoutKind.Sequential)] internal struct StartupInfo
        {
            public int Size; public IntPtr Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
            public ushort ShowWindow, ReservedSize; public IntPtr ReservedBytes, StandardInput, StandardOutput, StandardError;
        }
        [StructLayout(LayoutKind.Sequential)] internal struct StartupInfoEx
        { public StartupInfo StartupInfo; public IntPtr AttributeList; }
        [StructLayout(LayoutKind.Sequential)] internal struct ProcessInformation
        { public IntPtr Process, Thread; public uint ProcessId, ThreadId; }
        [StructLayout(LayoutKind.Sequential)] internal struct JobBasicLimits
        {
            public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit;
            public UIntPtr Affinity; public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)] internal struct IoCounters
        { public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)] internal struct JobExtendedLimits
        {
            public JobBasicLimits BasicLimitInformation; public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }
        [StructLayout(LayoutKind.Sequential)] internal struct JobAccounting
        {
            public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses;
        }

        internal sealed class KernelHandle : SafeHandleZeroOrMinusOneIsInvalid
        {
            public KernelHandle() : base(true) { }
            public KernelHandle(IntPtr handle) : base(true) { SetHandle(handle); }
            protected override bool ReleaseHandle() => CloseHandle(handle);
        }
        internal sealed class ProcessWaitHandle : WaitHandle
        {
            public ProcessWaitHandle(KernelHandle process)
            {
                if (!DuplicateHandle(GetCurrentProcess(), process, GetCurrentProcess(), out var duplicate, 0, false, 2))
                    throw new InvalidOperationException("The UPCAM backend process could not be supervised.");
                using (duplicate)
                {
                    SafeWaitHandle = new SafeWaitHandle(duplicate.DangerousGetHandle(), ownsHandle: true);
                    duplicate.SetHandleAsInvalid();
                }
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CreatePipe(out SafeFileHandle read, out SafeFileHandle write, ref SecurityAttributes attributes, uint size);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetHandleInformation(SafeFileHandle handle, uint mask, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        internal static extern KernelHandle CreateJobObjectW(IntPtr attributes, string? name);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetInformationJobObject(KernelHandle job, int informationClass, ref JobExtendedLimits information, int length);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool QueryInformationJobObject(KernelHandle job, int informationClass, out JobAccounting information, int length, IntPtr returnLength);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool TerminateJobObject(KernelHandle job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool TerminateProcess(KernelHandle process, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint WaitForSingleObject(KernelHandle handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr bytes);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr bytes, IntPtr previous, IntPtr returnedBytes);
        [DllImport("kernel32.dll")] internal static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles, uint flags, IntPtr environment, string directory,
            ref StartupInfoEx startup, out ProcessInformation process);
        [DllImport("kernel32.dll", SetLastError = true)] internal static extern uint ResumeThread(KernelHandle thread);
        [DllImport("kernel32.dll")] internal static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool DuplicateHandle(IntPtr sourceProcess, SafeHandle source, IntPtr targetProcess,
            out KernelHandle target, uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint options);
        [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)] internal static extern uint GetDriveTypeW(string root);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        internal static extern uint GetLongPathNameW(string path, StringBuilder buffer, uint length);
    }
}
