using Microsoft.UI.Xaml;
using Workbench.Core;
using Workbench.WinUI.Sdk;

namespace Workbench.Digest.Acceptance;

/// <summary>A fresh, explicitly selected user root and an exclusive lease for each real manager.</summary>
internal sealed class OwnedStore : IAsyncDisposable
{
    private FileStream? _lease;
    internal string Root { get; }
    internal string PluginsRoot => System.IO.Path.Combine(Root, "Plugins");
    internal PluginManager Manager { get; private set; } = null!;

    internal OwnedStore(string purpose, AcceptanceReport report)
    {
        Root = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "UPCAM Workbench Acceptance " + Guid.NewGuid().ToString("N"), purpose + " user root");
        RejectLinks(Root);
        if (Directory.Exists(Root) || File.Exists(Root)) throw new AcceptanceFailure("The generated acceptance user root already exists.");
        Directory.CreateDirectory(Root);
        File.WriteAllText(System.IO.Path.Combine(Root, ".acceptance-owned"), "upcam-workbench-native-acceptance-v1");
        report.OwnRoot(Root);
        Open();
    }

    private void Open()
    {
        RejectLinks(Root);
        _lease = new FileStream(System.IO.Path.Combine(Root, "host.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        try
        {
            var projectionNames = Directory.EnumerateFiles(AppContext.BaseDirectory, "*.dll")
                .Select(System.IO.Path.GetFileNameWithoutExtension).OfType<string>()
                .Where(name => name.StartsWith("Microsoft.Windows.", StringComparison.Ordinal) && name.EndsWith(".Projection", StringComparison.Ordinal)
                    || name.StartsWith("Microsoft.WindowsAppRuntime.", StringComparison.Ordinal))
                .Append("Microsoft.InteractiveExperiences.Projection").Distinct().ToArray();
            Manager = new PluginManager(new PluginManagerOptions
            {
                PluginsRoot = PluginsRoot,
                UserRoot = Root,
                HostVersion = new Version(0, 2, 0),
                // Same additional shared identities as the unmodified platform native host.
                SharedAssemblies = [typeof(WinUiView).Assembly, typeof(FrameworkElement).Assembly,
                    typeof(WinRT.ComWrappersSupport).Assembly, typeof(Windows.Foundation.IAsyncAction).Assembly],
                SharedAssemblyNames = projectionNames
            });
        }
        catch { _lease.Dispose(); _lease = null; throw; }
    }

    internal string Profile(string id) => System.IO.Path.Combine(Root, "data", id, "profile");

    internal async Task RestartManagerAsync()
    {
        await Manager.DisposeAsync();
        _lease?.Dispose();
        _lease = null;
        Open();
    }

    public async ValueTask DisposeAsync()
    {
        // Runtime 0.2 retains known pending work. Keep the store lease if shutdown reports that it is still owned.
        await Manager.DisposeAsync();
        _lease?.Dispose();
        _lease = null;
        // Preserve only this run's synthetic artifacts for diagnosis. Never enumerate normal AppData.
    }

    internal static bool IsWithin(string root, string path) =>
        string.Equals(root, path, StringComparison.OrdinalIgnoreCase) ||
        path.StartsWith(System.IO.Path.TrimEndingDirectorySeparator(root) + System.IO.Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);

    internal static void RejectLinks(string path)
    {
        for (string? part = System.IO.Path.GetFullPath(path); part is not null; part = System.IO.Path.GetDirectoryName(part))
            if ((File.Exists(part) || Directory.Exists(part)) && (File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                throw new AcceptanceFailure("Acceptance paths must have no symbolic links or reparse points.");
    }
}
