using Microsoft.UI.Xaml;

namespace Upcam.Desktop;

public partial class App : Application
{
    private Mutex? _instance;
    private MainWindow? _window;

    public App()
    {
        RequestedTheme = ApplicationTheme.Dark;
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        string[] arguments = Environment.GetCommandLineArgs().Skip(1).ToArray();
        bool smoke = arguments.Contains("--ui-smoke-test");
        if (arguments.Any(arg => arg is not ("--ui-smoke-test" or "--background")))
        {
            Environment.Exit(2);
            return;
        }
        _instance = new Mutex(false, @"Local\UPCAMTaskDigest.Singleton.v1", out bool createdNew);
        if (!createdNew)
        {
            NativeDesktop.ActivateExisting();
            _instance.Dispose();
            Exit();
            return;
        }
        _window = new MainWindow(smoke, arguments.Contains("--background"));
        _window.Closed += (_, _) => _instance.Dispose();
        _window.Activate();
    }
}
