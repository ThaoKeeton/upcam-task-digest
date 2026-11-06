using Microsoft.UI.Xaml;

namespace Workbench.App;

public sealed partial class WorkbenchApplication : Application
{
    private readonly RunOptions _options;
    private MainWindow? _window;

    public WorkbenchApplication()
    {
        // The SDK owns Main, COM wrappers and the UI dispatcher setup. Read the
        // same process arguments here before loading its generated App.xaml resources.
        _options = RunOptions.Parse(Environment.GetCommandLineArgs().Skip(1).ToArray());
        _options.TraceStartup("sdk-generated-main-application-constructor");
        UnhandledException += (_, args) =>
        {
            Environment.ExitCode = 1;
            _options.WriteFailure("unhandled-ui-exception", args.Exception, args.Message);
            // Do not turn an unhandled native/UI failure into a successful smoke result.
        };
        _options.TraceStartup("initializing-compiled-application-xaml");
        InitializeComponent();
        _options.TraceStartup("application-resources-ready");
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            _options.TraceStartup("launch-callback-entered");
            _options.TraceStartup("constructing-main-window");
            _window = new MainWindow(_options);
            _options.TraceStartup("activating-main-window");
            _window.Activate();
            _options.TraceStartup("main-window-activated");
            _ = _window.InitializeAsync();
        }
        catch (Exception error)
        {
            Environment.ExitCode = 1;
            _options.WriteFailure("launch-callback-failed", error);
            throw;
        }
    }
}
