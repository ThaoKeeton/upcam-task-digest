using Microsoft.UI.Xaml;

namespace Upcam.Desktop;

public partial class App : Application
{
    private Mutex? _instance;
    private MainWindow? _window;

    public App()
    {
        string language = IsSyntheticRun ? "zh-CN" : InterfaceLanguageBootstrap.Read(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UPCAM", "TaskDigest", "language.json"));
        UnhandledException += (_, args) =>
        {
            if (IsSyntheticRun) RecordSyntheticFailure(args.Exception);
        };
        try
        {
            UiStrings.Configure(language);
            // MRT Core requires one well-formed BCP-47 tag, including when the
            // preference follows Windows. An empty override fails before XAML.
            Microsoft.Windows.Globalization.ApplicationLanguages.PrimaryLanguageOverride = UiStrings.ResolvedLanguage;
            InitializeComponent();
            if (IsSyntheticRun)
            {
                DebugSettings.XamlResourceReferenceFailed += (_, args) => SyntheticTrace(args.Message);
                SyntheticTrace("Application XAML initialized.");
            }
        }
        catch (Exception ex)
        {
            if (IsSyntheticRun) RecordSyntheticFailure(ex);
            throw;
        }
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        string[] arguments = Environment.GetCommandLineArgs().Skip(1).ToArray();
        AppLaunchOptions options;
        try { options = AppLaunchOptions.Parse(arguments, Environment.ProcessId); }
        catch (ArgumentException)
        {
            Environment.Exit(2);
            return;
        }
        bool smoke = options.Smoke;
        if (options.ParentPid is int parentPid)
        {
            if (!await ProcessHandoff.WaitForParentAsync(parentPid, options.HandoffEvent))
            {
                Environment.Exit(3);
                return;
            }
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
            if (smoke) SyntheticTrace("Creating main window.");
            _window = new MainWindow(smoke, options.Background, options.AfterUpdate);
            if (smoke) SyntheticTrace("Main window constructed.");
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
        var details = new System.Text.StringBuilder(exception.ToString());
        details.AppendLine($"\nHRESULT: 0x{exception.HResult:X8}");
        foreach (System.Collections.DictionaryEntry entry in exception.Data)
            if (entry.Value is string value) details.AppendLine($"{entry.Key}: {value}");
        File.WriteAllText(Path.Combine(directory, "result.txt"), details.ToString());
        Environment.Exit(1);
    }

    private static void SyntheticTrace(string message)
    {
        string directory = Path.Combine(AppContext.BaseDirectory, "smoke-output");
        Directory.CreateDirectory(directory);
        File.AppendAllText(Path.Combine(directory, "xaml-trace.txt"), message + Environment.NewLine);
    }
}
