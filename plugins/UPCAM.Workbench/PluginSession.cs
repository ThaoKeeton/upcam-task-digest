using System.Text.Json.Nodes;
using Workbench.Sdk;

namespace Upcam.Workbench;

/// <summary>One profile, one owned backend and one serialized business operation at a time.</summary>
internal sealed class PluginSession : IAsyncDisposable
{
    private readonly IPluginContext _context;
    private readonly string _packageDirectory;
    internal string DefaultProfile { get; }
    private readonly PluginState _state;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly CancellationTokenSource _stop;
    private CancellationTokenSource? _sessionLifetime;
    private CancellationTokenSource? _activeOperation;
    private PluginWorkerClient? _worker;
    private Task? _scheduler;
    private int _stopping;

    internal PluginSession(IPluginContext context, string packageDirectory, string defaultProfile, PluginState state)
    {
        _context = context;
        _packageDirectory = packageDirectory;
        DefaultProfile = defaultProfile;
        _state = state;
        _stop = CancellationTokenSource.CreateLinkedTokenSource(context.Lifetime);
    }

    internal Task<bool> ActivateAsync(CancellationToken token = default) => PerformAsync("open-backend", (_, _, _) =>
        Task.FromResult("The local profile is open. " + ActivationNotice), token);

    internal const string ActivationNotice = "API 1 starts saved authorized schedules when the first page or business command opens. The host must remain running; language, theme, tray, startup and updates are controlled by Workbench.";
    internal static string ReadyStatus => DigestPlugin.Synthetic ? "Ready · CI synthetic data" : "Ready · local profile";

    private async Task EnsureStartedAsync(CancellationToken operationToken)
    {
        lock (_state.Sync)
            if (_state.BindingError is string error) throw new InvalidOperationException(error);
        if (_worker?.IsUsable == true) return;
        await CloseWorkerAsync();
        string path;
        lock (_state.Sync) { path = _state.Current.Directory; _state.Ready = false; _state.SavedAutoEnabled = false; }
        // Retain this linked source until the backend exits; the client owns its lifetime registration.
        _sessionLifetime = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token, operationToken);
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
        _scheduler ??= SchedulerLoopAsync();
    }

    internal Task<bool> PerformAsync(string operation,
        Func<PluginWorkerClient, ProfileDraft, CancellationToken, Task<string>> action,
        CancellationToken token = default, bool requireActivation = true, bool wait = true) =>
        PerformCoreAsync(operation, (worker, profile, cancellation) => action(worker ??
            throw new InvalidOperationException("Open the backend before running this operation."), profile, cancellation),
            token, requireActivation, wait);

    private async Task<bool> PerformCoreAsync(string operation,
        Func<PluginWorkerClient?, ProfileDraft, CancellationToken, Task<string>> action,
        CancellationToken token, bool requireActivation, bool wait)
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
                _state.Status = "Working · " + operation;
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
                    _state.Status = _state.Ready ? ReadyStatus : _state.BindingError ?? "工作进程已停止；请重新连接，不会自动重放请求。";
                    _state.OperationResult = $"{++_state.OperationRevision} · {operation} · {(succeeded ? "succeeded" : "failed")} · {message}";
                }
                _gate.Release();
                _state.Notify();
            }
        }
    }

    internal void CancelCurrent()
    {
        lock (_state.Sync) _activeOperation?.Cancel();
    }

    internal Task<bool> SaveAsync(CancellationToken token = default) => PerformAsync("save-settings", async (worker, profile, cancellation) =>
    {
        JsonObject payload;
        lock (_state.Sync) payload = profile.Data.SavePayload();
        JsonObject result = await worker.SendAsync("save", payload, cancellation);
        lock (_state.Sync)
        {
            profile.Data.Apply(result);
            _state.SavedAutoEnabled = profile.Data.Flag("auto_send_enabled");
        }
        return JsonText(result, "status", "Business settings saved; credentials remain protected by current-user DPAPI.");
    }, token);

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
            JsonObject result = await worker.SendAsync(operation, captured, cancellation);
            lock (_state.Sync) apply?.Invoke(profile, result);
            return JsonText(result, "status", JsonText(result, "safety", "Operation completed."));
        }, token);

    internal static void RequireSaved(ProfileDraft profile)
    {
        if (profile.Data.Dirty || profile.Data.SecretDrafts.Any(x => x.Value.Length > 0))
            throw new InvalidOperationException("Save business settings explicitly before this operation. Your edits have been retained.");
    }

    internal async Task<bool> SwitchProfileAsync(string requestedPath, bool adopt, CancellationToken token = default)
    {
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
        return await PerformCoreAsync("bind-profile", async (_, _, cancellation) =>
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
                // Initialize locally, commit only the binding, then allow the scheduler to observe it.
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
        }, token, requireActivation: false, wait: true);
    }

    private async Task SchedulerLoopAsync()
    {
        try
        {
            using var timer = new PeriodicTimer(TimeSpan.FromSeconds(15));
            while (await timer.WaitForNextTickAsync(_stop.Token))
            {
                bool check;
                lock (_state.Sync) check = _state.Ready && _state.SavedAutoEnabled && !_state.Busy;
                if (!check) continue;
                await PerformAsync("scheduled-run", async (worker, profile, cancellation) =>
                {
                    JsonObject result = await worker.SendAsync("run_due", null, cancellation);
                    if (result["events"] is JsonArray events && events.Count > 0)
                    {
                        var history = await worker.SendAsync("history", null, cancellation);
                        lock (_state.Sync)
                        {
                            profile.Data.Output = string.Join("\n", events.Select(x => JsonText(x, "message")));
                            profile.Data.History = history["runs"]?.DeepClone().AsArray() ?? new JsonArray();
                        }
                        return "Saved schedule checked. Review the local run history for delivery state.";
                    }
                    return "Saved authorized schedule checked; no due slot started.";
                }, _stop.Token, requireActivation: false, wait: false);
            }
        }
        catch (OperationCanceledException) { }
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
        if (_scheduler is not null) await _scheduler;
        _state.Notify(false);
    }

    internal static string JsonText(JsonNode? node, string key, string fallback = "") => node?[key]?.GetValue<string>() ?? fallback;
}
