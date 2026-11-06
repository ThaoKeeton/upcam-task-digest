using Microsoft.UI.Xaml;

namespace Workbench.Digest.Acceptance;

public sealed partial class AcceptanceApplication : Application
{
    private readonly CancellationTokenSource _lifetime = new(TimeSpan.FromMinutes(12));
    private readonly CancellationTokenSource _watchdog = new();
    private readonly AcceptanceReport _report;
    private readonly RunOptions? _options;
    private Window? _window;
    private bool _finished;

    public AcceptanceApplication()
    {
        // Do not inspect values inherited from a developer/runner. Remove known external-service credential names
        // from this process before any child can inherit them; system/user environment settings are untouched.
        foreach (var name in new[] { "TEAMBITION_APP_ID", "TEAMBITION_APP_SECRET", "TEAMBITION_USER_MAP_JSON",
            "TEAMBITION_TENANT_ID", "TEAMBITION_PROJECT_ID", "DINGTALK_WEBHOOK", "DINGTALK_SIGNING_SECRET", "OPENAI_API_KEY" })
            Environment.SetEnvironmentVariable(name, null, EnvironmentVariableTarget.Process);

        try
        {
            _options = RunOptions.Parse(Environment.GetCommandLineArgs().Skip(1).ToArray());
            _report = new AcceptanceReport(_options.ReportPath);
        }
        catch (Exception error)
        {
            _report = new AcceptanceReport(System.IO.Path.Combine(System.IO.Path.GetTempPath(), "UPCAM acceptance invalid options " + Guid.NewGuid().ToString("N") + ".json"));
            _report.Fail("command-line-options", error is AcceptanceFailure ? error.Message : "Invalid options; raw arguments suppressed.");
        }
        UnhandledException += (_, args) =>
        {
            _report.Fail("unhandled-native-ui", $"Unhandled {args.Exception.GetType().Name}; raw exception suppressed.");
            args.Handled = true;
            _lifetime.Cancel();
        };
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(TimeSpan.FromMinutes(14), _watchdog.Token);
                _report.Fail("process-deadline", "The native process deadline expired; owned job-object cleanup must be checked by transport acceptance.");
                _report.Finish();
                Environment.Exit(124);
            }
            catch (OperationCanceledException) { }
        });
        try { InitializeComponent(); }
        catch (Exception error)
        {
            _report.Fail("compiled-application-resources", $"Unexpected {error.GetType().Name}; raw exception suppressed.");
            _report.Finish();
            Environment.Exit(1);
        }
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _ = RunAsync();
    }

    private async Task RunAsync()
    {
        try
        {
            if (_options is null) return;
            _window = new Window();
            _window.Closed += (_, _) => { if (!_finished) _lifetime.Cancel(); };
            var host = new NativeHost(_window, _lifetime.Token);
            _window.Activate();
            // Yield to the SDK message loop before inspecting the visible native tree.
            await Task.Yield();
            await new AcceptanceRunner(host, _options, _report, _lifetime.Token).RunAsync();
        }
        catch (Exception error)
        {
            _report.Fail("native-runner", error is AcceptanceFailure ? error.Message : $"Unexpected {error.GetType().Name}; raw exception suppressed.");
        }
        finally
        {
            _finished = true;
            _report.Finish();
            _watchdog.Cancel();
            Environment.ExitCode = _report.Succeeded ? 0 : 1;
            _window?.Close();
            Exit();
            // A test executable must finish even if a failed plugin retains an unrelated managed thread.
            Environment.Exit(Environment.ExitCode);
        }
    }
}
