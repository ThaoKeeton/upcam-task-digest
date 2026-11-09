using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Workbench.Sdk;

namespace Upcam.Workbench;

/// <summary>The host owns this execution. Only the local validated profile authorizes business work.</summary>
internal sealed class ManagedDigestScheduler(IPluginRuntimeContext context, PluginSession session, PluginState state)
{
    internal const string TaskId = "digest-scheduler";
    internal const string SecretId = "upcam.resume-binding-key";
    internal const string BindingKey = "schedule-binding-v1";
    private readonly SchedulerAdmission _admission = new();

    internal void Register()
    {
        context.Secrets.Register(new PluginSecretDefinition(SecretId, "日报自动恢复校验密钥",
            "只保护自动恢复的目录 / 授权绑定。由插件生成，不保存、复制或迁移 Teambition、OpenAI 或钉钉业务密钥；业务凭据仍以原目录 DPAPI 为唯一来源。"));
        context.BackgroundTasks.Register(new PluginBackgroundTask(TaskId, "UPCAM 自动日报", RunAsync)
        {
            RequiresPluginStart = false
        });
    }

    internal void RefreshSnapshot()
    {
        PluginBackgroundTaskSnapshot? snapshot = context.BackgroundTasks.GetSnapshots().SingleOrDefault(x => x.Id == TaskId);
        lock (state.Sync)
        {
            state.ResumeOnStartup = snapshot?.ResumeOnStartup == true;
            state.SchedulerStatus = snapshot is null ? "主机自动任务尚不可用。" :
                $"主机任务 {snapshot.State} · 运行意图 {(snapshot.DesiredRunning ? "开启" : "关闭")} · 启动恢复 {(snapshot.ResumeOnStartup ? "已选择" : "未选择")}";
            if (state.LastSchedulerError.Length > 0) state.SchedulerStatus += "\n" + state.LastSchedulerError;
        }
        state.Notify(false);
    }

    internal string LocalStatus()
    {
        PluginBackgroundTaskSnapshot? snapshot = context.BackgroundTasks.GetSnapshots().SingleOrDefault(x => x.Id == TaskId);
        lock (state.Sync)
            return new JsonObject
            {
                ["state"] = snapshot?.State.ToString() ?? "Unavailable",
                ["resumeOnStartup"] = snapshot?.ResumeOnStartup == true,
                ["desiredRunning"] = snapshot?.DesiredRunning == true,
                ["checkRevision"] = state.ScheduleCheckRevision,
                ["lastCheckSucceeded"] = state.LastScheduleCheckSucceeded,
                ["lastCheckOperation"] = state.LastScheduleOperation
            }.ToJsonString();
    }

    internal async Task<PluginTaskOperationResult> StartUserAsync(CancellationToken token)
    {
        // SDK Start is idempotent and may return an existing execution. Never attach a caller-owned waiter to its callback.
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token, context.Lifetime);
        deadline.CancelAfter(TimeSpan.FromSeconds(120));
        long epoch = CaptureAuthorizationEpoch();
        bool authorized = await session.PerformAsync("validate-scheduler-start", async (worker, profile, cancellation) =>
        {
            ValidateAuthorizationEpoch(epoch);
            await ReadAuthorizationAsync(worker, profile, cancellation);
            ValidateAuthorizationEpoch(epoch);
            return "已保存的本机业务授权已复核；尚未由此预检创建恢复绑定。";
        }, deadline.Token, background: true);
        if (!authorized) return new(false, "启动预检未通过或等待已结束；请核对已保存的自动授权及主机状态，未创建替代绑定。" );
        try
        {
            ValidateAuthorizationEpoch(epoch);
            PluginTaskOperationResult result = await context.BackgroundTasks.StartAsync(TaskId, deadline.Token);
            if (!result.Succeeded) return new(false, "主机未能启动自动任务；请检查任务是否仍在停止以及运行意图是否可保存。" );
            return new(true, "主机已接受启动请求；回调将验证恢复绑定。请以主机任务状态和授权检查结果确认就绪，启动恢复偏好保持原选择。" );
        }
        finally { RefreshSnapshot(); }
    }

    internal async Task<PluginTaskOperationResult> StopUserAsync(CancellationToken token)
    {
        PluginTaskOperationResult result = await context.BackgroundTasks.StopAsync(TaskId, token);
        RefreshSnapshot();
        return result.Succeeded ? new(true, "主机自动任务已停止；手动业务操作仍可使用原本机工作进程。") :
            new(false, "主机未能确认任务停止或保存停止意图；请刷新状态后重试。" );
    }

    internal async Task PrepareProfileOrSettingsChangeAsync(bool revokeResume, CancellationToken token)
    {
        // Never hold the session RPC gate here: Stop waits for the execution to leave that gate.
        PluginTaskOperationResult stopped = await context.BackgroundTasks.StopAsync(TaskId, token);
        if (!stopped.Succeeded) throw new InvalidOperationException("无法确认自动任务停止或保存停止意图；本次业务配置 / 目录操作未继续。" );
        if (revokeResume)
        {
            PluginTaskOperationResult revoked = await context.BackgroundTasks.SetResumeOnStartupAsync(TaskId, false, token);
            if (!revoked.Succeeded) throw new InvalidOperationException("自动任务已停止，但无法撤销启动恢复；本次配置 / 目录操作未继续。" );
        }
        await ClearBindingAsync(token);
        RefreshSnapshot();
    }

    internal IDisposable BeginConfigurationChange() => _admission.BeginMutation();

    internal async Task FinishConfigurationChangeAsync(bool revokeResume, CancellationToken token)
    {
        // Drain SDK starts rejected during the transition and remove any grant completed before the first stop.
        await PrepareProfileOrSettingsChangeAsync(revokeResume, token);
    }

    private long CaptureAuthorizationEpoch() => _admission.Capture();
    private void ValidateAuthorizationEpoch(long epoch) => _admission.Validate(epoch);

    internal async Task<PluginTaskOperationResult> SetResumeAsync(bool enabled, CancellationToken token)
    {
        if (enabled)
        {
            bool validated = await session.PerformAsync("validate-resume", async (worker, profile, cancellation) =>
            {
                var authorization = await ReadAuthorizationAsync(worker, profile, cancellation);
                await VerifyBindingAsync(profile.Directory, authorization, cancellation);
                return "现有恢复绑定和已保存的业务授权验证通过。";
            }, token, background: true);
            if (!validated) return new(false, "启动恢复未开启：请先保存有效的自动发送配置并明确启动一次自动任务。" );
        }
        PluginTaskOperationResult result = await context.BackgroundTasks.SetResumeOnStartupAsync(TaskId, enabled, token);
        RefreshSnapshot();
        return result.Succeeded ? new(true, enabled ? "已明确选择主机启动恢复；只有仍有运行意图且绑定一致时才能恢复。" : "已关闭主机启动恢复。") :
            new(false, "无法保存主机启动恢复偏好；请检查本机主机状态目录后重试。" );
    }

    internal async Task ClearBindingAsync(CancellationToken token)
    {
        await context.Settings.SetAsync<JsonObject?>(BindingKey, null, token);
        if (context.Secrets.IsSupported) await context.Secrets.DeleteAsync(SecretId, token);
    }

    private async Task RunAsync(IPluginBackgroundExecution execution)
    {
        CancellationToken token = execution.CancellationToken;
        PluginWorkerClient? authorizedWorker = null;
        try
        {
            token.ThrowIfCancellationRequested();
            long epoch = CaptureAuthorizationEpoch();
            bool authorized = await session.PerformAsync("authorize-scheduler", async (worker, profile, cancellation) =>
            {
                ValidateAuthorizationEpoch(epoch);
                var authorization = await ReadAuthorizationAsync(worker, profile, cancellation);
                ValidateAuthorizationEpoch(epoch);
                if (execution.StartReason == PluginTaskStartReason.UserRequest)
                    await CreateBindingAsync(profile.Directory, authorization, cancellation);
                else
                    await VerifyBindingAsync(profile.Directory, authorization, cancellation);
                ValidateAuthorizationEpoch(epoch);
                authorizedWorker = worker;
                return execution.StartReason == PluginTaskStartReason.Restore ? "已核对既有恢复绑定和本机业务授权。" : "本机业务授权已通过；恢复绑定已保存，未自动选择启动恢复。";
            }, token, background: true);
            if (!authorized)
            {
                token.ThrowIfCancellationRequested();
                if (context.Lifetime.IsCancellationRequested && execution.StopReason != PluginStopReason.None) return;
                throw new InvalidOperationException("自动任务未通过本机授权或恢复绑定验证；没有开始采集或发送。" );
            }
            token.ThrowIfCancellationRequested();
            lock (state.Sync) state.LastSchedulerError = "";
            RefreshSnapshot();
            using var timer = new PeriodicTimer(TimeSpan.FromSeconds(15));
            while (await timer.WaitForNextTickAsync(token))
            {
                token.ThrowIfCancellationRequested();
                bool completed = await session.PerformAsync("scheduled-run", async (worker, profile, cancellation) =>
                {
                    ValidateAuthorizationEpoch(epoch);
                    if (!ReferenceEquals(worker, authorizedWorker) || !worker.IsUsable)
                        throw new InvalidOperationException("已授权的工作进程连接已中断或改变；请停止后明确启动自动任务，不会自动重连。" );
                    var authorization = await ReadAuthorizationAsync(worker, profile, cancellation);
                    await VerifyBindingAsync(profile.Directory, authorization, cancellation);
                    ValidateAuthorizationEpoch(epoch);
                    cancellation.ThrowIfCancellationRequested();
                    JsonObject result = await worker.SendAsync("plugin_run_due_authorized",
                        new JsonObject { ["authorization_fingerprint"] = authorization }, cancellation);
                    if (result["events"] is JsonArray events && events.Count > 0)
                    {
                        var history = await worker.SendAsync("history", null, cancellation);
                        lock (state.Sync)
                        {
                            profile.Data.Output = string.Join("\n", events.Select(x => PluginSession.JsonText(x, "message")));
                            profile.Data.History = history["runs"]?.DeepClone().AsArray() ?? new JsonArray();
                        }
                        return "已检查保存的自动计划；请在本机运行记录中核对发送状态。";
                    }
                    return "已检查保存的自动计划；本次没有启动到期任务。";
                }, token, requireActivation: false, background: true);
                if (!completed)
                {
                    token.ThrowIfCancellationRequested();
                    if (context.Lifetime.IsCancellationRequested && execution.StopReason != PluginStopReason.None) return;
                    throw new InvalidOperationException("自动任务检查未完成或本机授权发生变化；已停止自动执行，不会重放请求。" );
                }
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception) when (context.Lifetime.IsCancellationRequested && execution.StopReason != PluginStopReason.None)
        {
            // Core publishes StopReason before concurrently cancelling plugin and execution lifetimes.
            token.ThrowIfCancellationRequested();
        }
        catch
        {
            const string safe = "自动任务失败：请核对已保存的自动授权、范围、模型缓存和恢复绑定，再明确启动；未自动创建替代授权。";
            lock (state.Sync) state.LastSchedulerError = safe;
            throw new InvalidOperationException(safe);
        }
        finally
        {
            lock (state.Sync)
                state.SchedulerStatus = token.IsCancellationRequested ? $"主机自动任务已结束 · {execution.StopReason}" : "主机自动任务执行已结束；请刷新主机任务状态。";
            state.Notify(false);
        }
    }

    private static async Task<string> ReadAuthorizationAsync(PluginWorkerClient worker, ProfileDraft profile, CancellationToken token)
    {
        JsonObject result = await worker.SendAsync("plugin_auto_authorization", null, token);
        string fingerprint = PluginSession.JsonText(result, "authorization_fingerprint");
        if (result["authorized"]?.GetValue<bool>() != true || !IsHex(fingerprint) ||
            PluginSession.JsonText(result, "application_version") != "0.4.4" ||
            PluginSession.JsonText(result, "ruleset_revision") != profile.Data.RulesetRevision ||
            !string.Equals(ProfileBinding.Canonicalize(PluginSession.JsonText(result, "data_directory")), profile.Directory, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("本机自动授权证明无效；没有开始采集、模型调用或发送。" );
        return fingerprint;
    }

    private async Task CreateBindingAsync(string profile, string fingerprint, CancellationToken token)
    {
        if (!context.Secrets.IsSupported) throw new InvalidOperationException("主机安全凭据存储不可用；不能启动自动任务，不使用明文替代。" );
        byte[] key = RandomNumberGenerator.GetBytes(32);
        try
        {
            // A corrupt protected value is an error, never an implicit storage reset.
            string? previous = await context.Secrets.ReadAsync(SecretId, token);
            if (previous is not null)
            {
                byte[] old = Convert.FromBase64String(previous);
                try { if (old.Length != 32) throw new FormatException(); }
                finally { CryptographicOperations.ZeroMemory(old); }
            }
            string proof = ComputeProof(key, profile, fingerprint);
            await context.Secrets.WriteAsync(SecretId, Convert.ToBase64String(key), token);
            await context.Settings.SetAsync(BindingKey, new JsonObject
            {
                ["schema"] = 1, ["profileDirectory"] = profile, ["proof"] = proof
            }, token);
        }
        catch (PluginSecretStoreException) { throw new InvalidOperationException("主机恢复密钥无法安全保存；自动任务未启动，请检查当前 Windows 用户的安全存储。" ); }
        catch (FormatException) { throw new InvalidOperationException("既有恢复密钥格式损坏；请通过保存配置撤销旧绑定，再核对后明确启动。" ); }
        finally { CryptographicOperations.ZeroMemory(key); }
    }

    private async Task VerifyBindingAsync(string profile, string fingerprint, CancellationToken token)
    {
        if (!context.Secrets.IsSupported) throw new InvalidOperationException("主机安全凭据存储不可用；启动恢复已拒绝，不使用明文替代。" );
        byte[]? key = null;
        byte[]? actual = null;
        byte[]? expected = null;
        try
        {
            string? encoded = await context.Secrets.ReadAsync(SecretId, token);
            JsonObject? binding = await context.Settings.GetAsync<JsonObject?>(BindingKey, null, token);
            if (encoded is null || binding is null || binding["schema"]?.GetValue<int>() != 1)
                throw new InvalidOperationException("缺少既有恢复绑定；请明确启动一次自动任务，恢复过程不会创建密钥或授权。" );
            string directory = ProfileBinding.Canonicalize(PluginSession.JsonText(binding, "profileDirectory"));
            string proof = PluginSession.JsonText(binding, "proof");
            if (!string.Equals(directory, profile, StringComparison.OrdinalIgnoreCase) || !IsHex(proof))
                throw new InvalidOperationException("恢复绑定的数据目录不匹配；请核对原目录后明确启动。" );
            key = Convert.FromBase64String(encoded);
            if (key.Length != 32) throw new FormatException();
            actual = Convert.FromHexString(proof);
            expected = Convert.FromHexString(ComputeProof(key, directory, fingerprint));
            if (!CryptographicOperations.FixedTimeEquals(actual, expected))
                throw new InvalidOperationException("业务设置、凭据或计划已改变，既有恢复绑定已失效；请保存核对后明确启动。" );
        }
        catch (PluginSecretStoreException) { throw new InvalidOperationException("主机恢复密钥无法安全读取；恢复已拒绝，不会重新生成或使用明文替代。" ); }
        catch (FormatException) { throw new InvalidOperationException("主机恢复密钥或绑定格式损坏；恢复已拒绝，请核对后明确启动。" ); }
        finally
        {
            if (key is not null) CryptographicOperations.ZeroMemory(key);
            if (actual is not null) CryptographicOperations.ZeroMemory(actual);
            if (expected is not null) CryptographicOperations.ZeroMemory(expected);
        }
    }

    private static bool IsHex(string value) => value.Length == 64 && value.All(x => x is >= '0' and <= '9' or >= 'a' and <= 'f');
    private static string ComputeProof(byte[] key, string profile, string fingerprint) =>
        Convert.ToHexStringLower(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(profile + "\n" + fingerprint)));
}
