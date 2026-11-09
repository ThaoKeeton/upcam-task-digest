using System.Text.Json.Nodes;
using System.Diagnostics;
using Upcam.Desktop;
using Workbench.Sdk;

namespace Upcam.Workbench;

/// <summary>One profile, one owned backend and one serialized business operation at a time.</summary>
internal sealed class PluginSession : IAsyncDisposable
{
    private readonly IPluginRuntimeContext _context;
    private readonly string _packageDirectory;
    internal string DefaultProfile { get; }
    private readonly PluginState _state;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly SemaphoreSlim _configurationGate = new(1, 1);
    private readonly CancellationTokenSource _stop;
    private CancellationTokenSource? _sessionLifetime;
    private CancellationTokenSource? _activeOperation;
    private PluginWorkerClient? _worker;
    internal ManagedDigestScheduler Scheduler { get; }
    private int _stopping;

    internal PluginSession(IPluginRuntimeContext context, string packageDirectory, string defaultProfile, PluginState state)
    {
        _context = context;
        _packageDirectory = packageDirectory;
        DefaultProfile = defaultProfile;
        _state = state;
        _stop = CancellationTokenSource.CreateLinkedTokenSource(context.Lifetime);
        Scheduler = new ManagedDigestScheduler(context, this, state);
    }

    internal Task<bool> ActivateAsync(CancellationToken token = default) => PerformAsync("open-backend", (_, _, _) =>
        Task.FromResult("The local profile is open. " + ActivationNotice), token);

    internal const string ActivationNotice = "打开页面只连接本机数据，不启动自动任务。自动任务由 Workbench 0.2 管理；须通过已保存的业务授权，启动恢复须单独明确选择。";
    internal static string ReadyStatus => DigestPlugin.Synthetic ? "Ready · CI synthetic data" : "Ready · local profile";

    private async Task EnsureStartedAsync(CancellationToken operationToken)
    {
        lock (_state.Sync)
            if (_state.BindingError is string error) throw new InvalidOperationException(error);
        if (_worker?.IsUsable == true) return;
        await CloseWorkerAsync();
        string path;
        lock (_state.Sync) { path = _state.Current.Directory; _state.Ready = false; _state.SavedAutoEnabled = false; }
        // The shared worker belongs only to plugin lifetime. A completed task/user token must never kill a later manual RPC.
        _sessionLifetime = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        using var startupCancellation = operationToken.Register(static value => ((CancellationTokenSource)value!).Cancel(), _sessionLifetime);
        try
        {
            _worker = await PluginWorkerClient.StartAsync(_packageDirectory, path, DigestPlugin.Synthetic, _sessionLifetime.Token);
            var result = await _worker.SendAsync("initialize", null, operationToken);
            lock (_state.Sync)
            {
                _state.Current.ApplyInitialization(result);
                _state.SavedAutoEnabled = result["settings"]?["auto_send_enabled"]?.GetValue<bool>() == true;
                _state.Ready = true;
            }
        }
        catch
        {
            await CloseWorkerAsync();
            throw;
        }
    }

    internal Task<bool> PerformAsync(string operation,
        Func<PluginWorkerClient, ProfileDraft, CancellationToken, Task<string>> action,
        CancellationToken token = default, bool requireActivation = true, bool wait = true, bool background = false) =>
        PerformCoreAsync(operation, (worker, profile, cancellation) => action(worker ??
            throw new InvalidOperationException("Open the backend before running this operation."), profile, cancellation),
            token, requireActivation, wait, background);

    private async Task<bool> PerformCoreAsync(string operation,
        Func<PluginWorkerClient?, ProfileDraft, CancellationToken, Task<string>> action,
        CancellationToken token, bool requireActivation, bool wait, bool background = false)
    {
        bool entered = false;
        bool succeeded = false;
        string message = "Operation cancelled. If delivery was interrupted, check history before sending again; no request was replayed.";
        using var operationCancellation = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, token);
        try
        {
            if (wait) { await _gate.WaitAsync(operationCancellation.Token); entered = true; }
            else entered = await _gate.WaitAsync(0, operationCancellation.Token);
            if (!entered) return false;
            operationCancellation.Token.ThrowIfCancellationRequested();
            lock (_state.Sync)
            {
                _activeOperation = operationCancellation;
                _state.Busy = true;
                _state.BackgroundActivity = background;
                _state.ActiveOperation = operation;
                if (!background) _state.Status = "Working · " + operation;
                else _state.ScheduleCheckResult = $"{_state.ScheduleCheckRevision} · 正在检查已保存的自动计划；可取消当前操作。";
            }
            _state.Notify(false);
            if (requireActivation) await EnsureStartedAsync(operationCancellation.Token);
            ProfileDraft profile;
            lock (_state.Sync) profile = _state.Current;
            message = await action(_worker, profile, operationCancellation.Token);
            succeeded = true;
            return true;
        }
        catch (OperationCanceledException) { return false; }
        catch (Exception error)
        {
            // The worker boundary's InvalidOperationException messages are fixed/sanitized. Other failures never echo paths/protocol.
            message = error is InvalidOperationException ? error.Message :
                "The local operation did not complete. Check the profile and package; credentials and service responses were not logged.";
            return false;
        }
        finally
        {
            if (entered)
            {
                if (_worker?.IsUsable == false || operationCancellation.IsCancellationRequested)
                {
                    try { await CloseWorkerAsync(); }
                    catch
                    {
                        succeeded = false;
                        message = "工作进程清理未完成；请退出 Workbench 后检查本机运行记录，不会自动重放请求。";
                    }
                    finally
                    {
                        lock (_state.Sync) { _state.Ready = false; _state.SavedAutoEnabled = false; }
                    }
                }
                lock (_state.Sync)
                {
                    _activeOperation = null;
                    _state.Busy = false;
                    _state.BackgroundActivity = false;
                    _state.ActiveOperation = "";
                    _state.Status = _state.Ready ? ReadyStatus : _state.BindingError ?? "工作进程已停止；请重新连接，不会自动重放请求。";
                    if (!background)
                        _state.OperationResult = $"{++_state.OperationRevision} · {operation} · {(succeeded ? "succeeded" : "failed")} · {message}";
                    if (background)
                    {
                        _state.LastScheduleCheckSucceeded = succeeded;
                        _state.LastScheduleOperation = operation;
                        _state.ScheduleCheckResult = $"{++_state.ScheduleCheckRevision} · {DateTime.Now:yyyy-MM-dd HH:mm:ss} · {(succeeded ? "succeeded" : "failed")} · {message}";
                    }
                }
                _gate.Release();
                _state.Notify(rebuild: !background);
            }
        }
    }

    internal void CancelCurrent()
    {
        lock (_state.Sync) _activeOperation?.Cancel();
    }

    internal async Task CancelCurrentAsync(CancellationToken token = default)
    {
        bool managed;
        lock (_state.Sync) managed = _state.BackgroundActivity && (_state.ActiveOperation is "scheduled-run" or "authorize-scheduler");
        if (managed)
        {
            var result = await Scheduler.StopUserAsync(token);
            RecordForeground("cancel-operation", result.Succeeded, result.Message);
        }
        else
        {
            CancelCurrent();
            var result = await Scheduler.StopUserAsync(token);
            RecordForeground("cancel-operation", result.Succeeded, result.Message);
        }
    }

    internal Task<bool> StartSchedulerAsync(CancellationToken token = default) =>
        RunRuntimeControlAsync("scheduler-start", Scheduler.StartUserAsync, token);
    internal Task<bool> StopSchedulerAsync(CancellationToken token = default) =>
        RunRuntimeControlAsync("scheduler-stop", Scheduler.StopUserAsync, token);
    internal Task<bool> SetSchedulerResumeAsync(bool enabled, CancellationToken token = default) =>
        RunRuntimeControlAsync("scheduler-resume", cancellation => Scheduler.SetResumeAsync(enabled, cancellation), token);

    private async Task<bool> RunRuntimeControlAsync(string operation,
        Func<CancellationToken, Task<PluginTaskOperationResult>> action, CancellationToken token)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, token);
        bool entered = false;
        try
        {
            await _configurationGate.WaitAsync(linked.Token); entered = true;
            var result = await action(linked.Token);
            RecordForeground(operation, result.Succeeded, result.Message);
            return result.Succeeded;
        }
        catch (OperationCanceledException) { RecordForeground(operation, false, "主机任务操作已取消；请刷新状态核对已保存的运行意图。" ); return false; }
        catch (Exception error) { RecordForeground(operation, false, SafeControlError(error)); return false; }
        finally { if (entered) _configurationGate.Release(); }
    }

    private void RecordForeground(string operation, bool succeeded, string message)
    {
        lock (_state.Sync) _state.OperationResult = $"{++_state.OperationRevision} · {operation} · {(succeeded ? "succeeded" : "failed")} · {message}";
        _state.Notify(false);
    }

    private static string SafeControlError(Exception error) => error is InvalidOperationException ? error.Message :
        "主机任务 / 安全绑定操作未完成；请检查本机状态目录和安全凭据存储，再刷新任务状态。业务数据没有迁移。";

    private async Task<bool> MutateBusinessAsync(bool revokeResume, Func<Task<bool>> action, CancellationToken token)
    {
        using var mutation = Scheduler.BeginConfigurationChange();
        try
        {
            await Scheduler.PrepareProfileOrSettingsChangeAsync(revokeResume, token);
            return await action();
        }
        finally { await Scheduler.FinishConfigurationChangeAsync(revokeResume, _stop.Token); }
    }

    internal async Task<bool> SaveAsync(CancellationToken token = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, token);
        token = linked.Token;
        await _configurationGate.WaitAsync(token);
        try
        {
            bool requestedAuto;
            lock (_state.Sync) requestedAuto = _state.Current.Data.Flag("auto_send_enabled");
            bool saved = await MutateBusinessAsync(!requestedAuto, () => PerformAsync("save-settings", async (worker, profile, cancellation) =>
            {
                JsonObject payload;
                lock (_state.Sync) payload = profile.Data.SavePayload();
                JsonObject result = await DiagnoseWithProgressAsync(worker, profile, "save-settings", "save", payload, cancellation);
                lock (_state.Sync)
                {
                    profile.Data.Apply(result);
                    _state.SavedAutoEnabled = profile.Data.Flag("auto_send_enabled");
                }
                return JsonText(result, "status", "Business settings saved; credentials remain protected by current-user DPAPI.");
            }, token), token);
            if (!saved) return false;
            bool auto;
            lock (_state.Sync) auto = _state.SavedAutoEnabled;
            PluginTaskOperationResult task = auto ? await Scheduler.StartUserAsync(token) : await Scheduler.SetResumeAsync(false, token);
            if (!task.Succeeded) { RecordForeground("save-settings", false, "业务设置已保存，但主机自动任务 / 恢复偏好未完成。" + task.Message); return false; }
            return true;
        }
        catch (OperationCanceledException) { RecordForeground("save-settings", false, "保存操作已取消；请核对本机设置和主机停止意图，未自动重试。" ); return false; }
        catch (Exception error) { RecordForeground("save-settings", false, SafeControlError(error)); return false; }
        finally { _configurationGate.Release(); }
    }

    internal async Task<bool> ClearCredentialsAsync(Func<ProfileDraft, JsonObject?> payload,
        Action<ProfileDraft, JsonObject> apply, CancellationToken token = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, token);
        token = linked.Token;
        await _configurationGate.WaitAsync(token);
        try
        {
            return await MutateBusinessAsync(true,
                () => CallAsync("clear-credentials", "clear_credentials", payload, apply, token: token), token);
        }
        catch (OperationCanceledException) { RecordForeground("clear-credentials", false, "清除操作已取消；请核对当前凭据和自动任务状态。" ); return false; }
        catch (Exception error) { RecordForeground("clear-credentials", false, SafeControlError(error)); return false; }
        finally { _configurationGate.Release(); }
    }

    internal Task<bool> PreviewSampleAsync(CancellationToken token = default) => PerformAsync("preview-sample", async (worker, profile, cancellation) =>
    {
        JsonObject payload;
        lock (_state.Sync) payload = profile.Data.WindowPayload();
        JsonObject result = await worker.SendAsync("preview_sample", payload, cancellation);
        lock (_state.Sync) profile.Data.Output = JsonText(result, "markdown");
        return JsonText(result, "status", "Fictional sample rendered locally.");
    }, token);

    internal Task<bool> CallAsync(string name, string operation, Func<ProfileDraft, JsonObject?> payload,
        Action<ProfileDraft, JsonObject>? apply = null, bool requireSaved = false, CancellationToken token = default) =>
        PerformAsync(name, async (worker, profile, cancellation) =>
        {
            JsonObject? captured;
            lock (_state.Sync)
            {
                if (requireSaved) RequireSaved(profile);
                captured = payload(profile);
            }
            JsonObject result = operation == "diagnose"
                ? await DiagnoseWithProgressAsync(worker, profile, name, "diagnose", captured, cancellation)
                : await worker.SendAsync(operation, captured, cancellation);
            lock (_state.Sync) apply?.Invoke(profile, result);
            return JsonText(result, "status", JsonText(result, "safety", "Operation completed."));
        }, token);

    private async Task<JsonObject> DiagnoseWithProgressAsync(PluginWorkerClient worker, ProfileDraft profile,
        string operation, string workerOperation, JsonObject? payload, CancellationToken cancellation)
    {
        var elapsed = Stopwatch.StartNew();
        CollectionProgress current = new("starting", 0, 0, 0, 0, 0);
        using var timerStop = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        var progress = new InlineProgress(value => Volatile.Write(ref current, value));
        async Task PublishAsync()
        {
            try
            {
                while (true)
                {
                    timerStop.Token.ThrowIfCancellationRequested();
                    var value = Volatile.Read(ref current);
                    string phase = UiStrings.T(value.Phase switch
                    {
                        "listing" => "列出项目任务", "loading_cache" => "恢复完整任务进度",
                        "collecting" => "读取任务及动态", "reconciling" => "重新核对项目变化",
                        "validating" => "校验诊断结果", _ => "准备只读采集"
                    });
                    lock (_state.Sync)
                    {
                        if (!_state.Busy || _state.ActiveOperation != operation || !ReferenceEquals(profile, _state.Current)) return;
                        _state.Status = UiStrings.F("{0} · 已耗时 {1}:{2} · 完整任务 {3}/{4} · 请求 {5}", phase,
                            (int)elapsed.Elapsed.TotalMinutes, elapsed.Elapsed.Seconds.ToString("00"),
                            value.Completed, value.Total, value.Requests);
                    }
                    _state.Notify(false);
                    await Task.Delay(TimeSpan.FromSeconds(1), timerStop.Token);
                }
            }
            catch (OperationCanceledException) when (timerStop.IsCancellationRequested) { }
        }
        Task publish = PublishAsync();
        try { return await worker.SendAsync(workerOperation, payload, cancellation, progress); }
        finally { timerStop.Cancel(); await publish; }
    }

    private sealed class InlineProgress(Action<CollectionProgress> report) : IProgress<CollectionProgress>
    {
        public void Report(CollectionProgress value) => report(value);
    }

    internal static void RequireSaved(ProfileDraft profile)
    {
        if (profile.Data.Dirty || profile.Data.SecretDrafts.Any(x => x.Value.Length > 0))
            throw new InvalidOperationException("Save business settings explicitly before this operation. Your edits have been retained.");
    }

    internal async Task<bool> SwitchProfileAsync(string requestedPath, bool adopt, CancellationToken token = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, token);
        token = linked.Token;
        // Validation and consent are local; no secret/profile data is copied or converted.
        string canonical;
        try { canonical = ProfileBinding.Canonicalize(requestedPath, requireExistingProfile: adopt); }
        catch (InvalidOperationException error)
        {
            lock (_state.Sync)
                _state.OperationResult = $"{++_state.OperationRevision} · bind-profile · failed · {error.Message}";
            _state.Notify(false);
            return false;
        }
        await _configurationGate.WaitAsync(token);
        try
        {
        return await MutateBusinessAsync(true, () => PerformCoreAsync("bind-profile", async (_, _, cancellation) =>
        {
            string previous;
            string? previousError;
            lock (_state.Sync) { previous = _state.Current.Directory; previousError = _state.BindingError; }
            await CloseWorkerAsync();
            lock (_state.Sync)
            {
                _state.Ready = false;
                _state.SavedAutoEnabled = false;
                _state.SelectProfile(canonical);
                _state.BindingError = null;
            }
            try
            {
                // Initialize locally and commit only the path. A profile change never starts the host task.
                await EnsureStartedAsync(cancellation);
                await _context.Settings.SetAsync(ProfileBinding.SettingsKey, canonical, cancellation);
                lock (_state.Sync) _state.AdoptionPath = canonical;
            }
            catch
            {
                await CloseWorkerAsync();
                lock (_state.Sync)
                {
                    _state.SelectProfile(previous);
                    _state.BindingError = previousError;
                    _state.Ready = false;
                    _state.SavedAutoEnabled = false;
                }
                throw;
            }
            return "Bound to the same original profile and delivery ledger. Per-profile unsaved edits are retained for this session.";
        }, token, requireActivation: false, wait: true), token);
        }
        catch (OperationCanceledException) { RecordForeground("bind-profile", false, "目录切换已取消；请核对原目录和主机任务状态。" ); return false; }
        catch (Exception error) { RecordForeground("bind-profile", false, SafeControlError(error)); return false; }
        finally { _configurationGate.Release(); }
    }

    private async Task CloseWorkerAsync()
    {
        var worker = _worker;
        _worker = null;
        try { if (worker is not null) await worker.DisposeAsync(); }
        finally
        {
            _sessionLifetime?.Dispose();
            _sessionLifetime = null;
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _stopping, 1) != 0) return;
        _stop.Cancel();
        CancelCurrent();
        await _gate.WaitAsync();
        try
        {
            await CloseWorkerAsync();
            lock (_state.Sync) { _state.Ready = false; _state.SavedAutoEnabled = false; }
        }
        finally { _gate.Release(); }
        if (_context.StopReason is not PluginStopReason.HostShutdown)
            await Scheduler.ClearBindingAsync(CancellationToken.None);
        _state.Notify(false);
    }

    internal static string JsonText(JsonNode? node, string key, string fallback = "") => node?[key]?.GetValue<string>() ?? fallback;
}
