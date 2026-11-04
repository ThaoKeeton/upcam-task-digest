using Microsoft.UI.Xaml;

namespace Upcam.Desktop;

public partial class App : Application
{
    private Mutex? _instance;
    private MainWindow? _window;

    public App()
    {
        RequestedTheme = ApplicationTheme.Dark;
        UnhandledException += (_, args) =>
        {
            if (IsSyntheticRun) RecordSyntheticFailure(args.Exception);
        };
        try { InitializeComponent(); }
        catch (Exception ex)
        {
            if (IsSyntheticRun) RecordSyntheticFailure(ex);
            throw;
        }
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
        try
        {
            _window = new MainWindow(smoke, arguments.Contains("--background"));
            _window.Closed += (_, _) => _instance.Dispose();
            _window.Activate();
        }
        catch (Exception ex)
        {
            if (smoke) RecordSyntheticFailure(ex);
            throw;
        }
    }

    private static bool IsSyntheticRun => Environment.GetCommandLineArgs().Contains("--ui-smoke-test");

    private static void RecordSyntheticFailure(Exception exception)
    {
        // This flag starts an isolated worker with fixtures only, never a user profile.
        string directory = Path.Combine(AppContext.BaseDirectory, "smoke-output");
        Directory.CreateDirectory(directory);
        File.WriteAllText(Path.Combine(directory, "result.txt"), exception.ToString());
        Environment.Exit(1);
    }
}
