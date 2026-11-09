using System.Security.Cryptography;
using System.Text.Json;
using Workbench.Core;

// A test utility using the exact shipping Core binary. It never loads plugin code.
// Run only in a fresh GitHub-hosted Windows account, before opening the normal host.
if (!OperatingSystem.IsWindows() || !Environment.UserInteractive ||
    Environment.GetEnvironmentVariable("CI") != "true" ||
    Environment.GetEnvironmentVariable("GITHUB_ACTIONS") != "true" ||
    Environment.GetEnvironmentVariable("RUNNER_ENVIRONMENT") != "github-hosted" || args.Length != 2)
    throw new InvalidOperationException("Fresh interactive hosted Windows CI is required.");

string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ProjectWorkbench");
string package = Path.GetFullPath(args[0]);
string report = Path.GetFullPath(args[1]);
foreach (string path in new[] { root, package, report }) RejectLinks(path);
if (Path.Exists(root) || Path.Exists(report) || !Directory.Exists(package) ||
    report.Equals(root, StringComparison.OrdinalIgnoreCase) || report.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
    throw new InvalidOperationException("Only a fresh profile and separate new report are allowed.");
string coreHash = Hash(typeof(PluginPackageStore).Assembly.Location);
if (coreHash != "a7f7f3fca18c3a8276b5fc4bd19d66600f3b4c65acc63a449887475ac3ee9db6")
    throw new InvalidOperationException("The loaded Core is not the pinned shipping Core.");
using (var metadata = JsonDocument.Parse(File.ReadAllText(Path.Combine(package, "plugin-package.json"))))
{
    var value = metadata.RootElement;
    if (value.GetProperty("pluginId").GetString() != "com.upcam.task-digest" ||
        value.GetProperty("pluginVersion").GetString() != "0.1.1" ||
        value.GetProperty("backendVersion").GetString() != "0.4.5" ||
        value.GetProperty("syntheticAcceptance").ValueKind != JsonValueKind.False)
        throw new InvalidOperationException("Only the new production package is allowed.");
}
Directory.CreateDirectory(root);
await using (var lease = new FileStream(Path.Combine(root, "host.lock"), FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None))
{
    var options = new PluginManagerOptions { UserRoot = root, PluginsRoot = Path.Combine(root, "Plugins"), HostVersion = new Version(0, 4, 0) };
    var store = new PluginPackageStore(options);
    var result = await store.InstallAsync(package);
    if (!result.Succeeded) throw new InvalidOperationException("Shipping PackageStore rejected the package.");
    await using var manager = new PluginManager(options);
    await manager.DiscoverAsync();
    var installed = manager.Plugins.Single();
    if (installed.Manifest?.Id != "com.upcam.task-digest" || installed.Manifest.Version != "0.1.1" ||
        installed.State != PluginState.Disabled || installed.IsLoaded || installed.IsTrusted || installed.DesiredEnabled)
        throw new InvalidOperationException("Installed package was not initially disabled, unloaded and untrusted.");
    var sourceFiles = Directory.GetFiles(package, "*", SearchOption.AllDirectories).Order().ToArray();
    var targetFiles = Directory.GetFiles(installed.PackagePath, "*", SearchOption.AllDirectories).Order().ToArray();
    if (sourceFiles.Length != targetFiles.Length || sourceFiles.Any(path =>
        Hash(path) != Hash(Path.Combine(installed.PackagePath, Path.GetRelativePath(package, path)))))
        throw new InvalidOperationException("Installed bytes do not match the audited production package.");
    Directory.CreateDirectory(Path.GetDirectoryName(report)!);
    await using var stream = new FileStream(report, FileMode.CreateNew, FileAccess.Write, FileShare.None);
    await JsonSerializer.SerializeAsync(stream, new { succeeded = true, method = "shipping-Core-PluginPackageStore.InstallAsync-folder", coreSha256 = coreHash,
        pluginId = "com.upcam.task-digest", pluginVersion = "0.1.1", backendVersion = "0.4.5", syntheticAcceptance = false,
        installedFiles = sourceFiles.Length, installedBytesMatch = true, disabled = true, trusted = false, loaded = false, desiredEnabled = false });
}
Console.WriteLine("Shipping PackageStore installed the production package, disabled and untrusted.");

static string Hash(string path) { using var stream = File.OpenRead(path); return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant(); }
static void RejectLinks(string path)
{
    for (string? current = Path.GetFullPath(path); current is not null; current = Path.GetDirectoryName(current))
        if (Path.Exists(current) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("Linked paths are forbidden.");
}
