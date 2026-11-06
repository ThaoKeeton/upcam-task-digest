using Microsoft.UI.Xaml;
using Workbench.Sdk;
using Workbench.WinUI.Sdk;

namespace Upcam.Workbench;

/// <summary>API 1 entry point. Initialization is local registration, never backend activation.</summary>
public sealed class DigestPlugin : IWorkbenchPlugin
{
#if UPCAM_ACCEPTANCE_BUILD
    internal const bool Synthetic = true;
    private const string ExpectedId = "com.upcam.task-digest.acceptance";
#else
    internal const bool Synthetic = false;
    private const string ExpectedId = "com.upcam.task-digest";
#endif
    private PluginState? _state;
    private PluginSession? _session;

    public async Task InitializeAsync(IPluginContext context, CancellationToken cancellationToken)
    {
        if (_session is not null) throw new InvalidOperationException("The plugin is already initialized.");
        if (context is not IPluginRuntimeContext runtime)
            throw new InvalidOperationException("UPCAM requires Workbench 0.2 runtime services.");
        if (!string.Equals(context.PluginId, ExpectedId, StringComparison.Ordinal))
            throw new InvalidOperationException("The package identity does not match this plugin build.");
        string defaultProfile = ProfileBinding.Canonicalize(Path.Combine(context.DataDirectory, "profile"));
        string binding = await context.Settings.GetAsync(ProfileBinding.SettingsKey, defaultProfile, cancellationToken);
        PluginState state;
        try { state = new PluginState(ProfileBinding.Canonicalize(binding)); }
        catch (InvalidOperationException)
        {
            // Keep the recorded binding intact until the user explicitly repairs it in the native Profile tab.
            state = new PluginState(defaultProfile)
            {
                BindingError = "已保存的数据目录无法安全打开；请在“数据目录”页明确选择原目录或 Workbench 默认目录。",
                Status = "数据目录绑定需要修复；尚未启动工作进程。"
            };
        }
        // Assembly.Location is the installed package; AppContext.BaseDirectory belongs to the host.
        string packageDirectory = Path.GetDirectoryName(typeof(DigestPlugin).Assembly.Location)
            ?? throw new InvalidOperationException("The installed plugin location is unavailable.");
        var session = new PluginSession(runtime, packageDirectory, defaultProfile, state);
        session.Scheduler.Register();
        _state = state;
        _session = session;
        context.RegisterView(new WinUiView("digest", "UPCAM Task Digest", () => CreateView(context, false)));
        context.RegisterView(new WinUiView("preferences", "UPCAM Task Digest", () => CreateView(context, true), WinUiViewKind.Settings));
        context.RegisterCommand(new PluginCommand("status", "UPCAM local status", token =>
        {
            token.ThrowIfCancellationRequested();
            context.Lifetime.ThrowIfCancellationRequested();
            lock (state.Sync)
                return Task.FromResult(new PluginCommandResult(true,
                    state.Ready ? "UPCAM backend ready." : "UPCAM backend not started."));
        }, "Local lifecycle status. This command does not start the worker or contact a service."));
        context.RegisterCommand(new PluginCommand("preview-sample", "Preview UPCAM sample", async token =>
        {
            bool succeeded = await session.PreviewSampleAsync(token);
            return new PluginCommandResult(succeeded, succeeded
                ? "Synthetic sample is available in Run & diagnostics. No tasks collected, model called or message sent."
                : "Sample preview did not complete. Open the native page for local status.");
        }, "Open the local backend and render the bundled fictional sample. This does not start the managed schedule."));
        context.RegisterCommand(new PluginCommand("scheduler-start", "启动 UPCAM 自动日报", async token =>
            new(await session.StartSchedulerAsync(token), "请在原生自动运行页查看本机授权和主机任务状态。")));
        context.RegisterCommand(new PluginCommand("scheduler-stop", "停止 UPCAM 自动日报", async token =>
            new(await session.StopSchedulerAsync(token), "请在原生自动运行页查看停止结果。")));
        context.RegisterCommand(new PluginCommand("scheduler-status", "UPCAM 自动任务本机状态", token =>
        {
            token.ThrowIfCancellationRequested();
            context.Lifetime.ThrowIfCancellationRequested();
            return Task.FromResult(new PluginCommandResult(true, session.Scheduler.LocalStatus()));
        }, "仅读取主机任务快照和本机检查计数；不连接工作进程，不读取密钥或业务内容。"));
    }

    private FrameworkElement CreateView(IPluginContext context, bool settings)
    {
        context.Lifetime.ThrowIfCancellationRequested();
        // A factory always creates a fresh tree. The plugin instance owns documents and session.
        return new DigestPage(_state ?? throw new InvalidOperationException("Plugin is unavailable."),
            _session ?? throw new InvalidOperationException("Plugin is unavailable."), settings);
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        if (_session is not null) await _session.DisposeAsync();
        _state?.ClearSecretDrafts();
    }
}
