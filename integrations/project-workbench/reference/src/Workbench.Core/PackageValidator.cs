using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Workbench.Sdk;

namespace Workbench.Core;

internal sealed record PackageFile(string RelativePath, string FullPath);

internal sealed record ValidatedPackage(
    string Path,
    PluginManifest? Manifest,
    string? Fingerprint,
    IReadOnlyList<PackageFile> Files,
    IReadOnlyList<PluginDiagnostic> Diagnostics)
{
    public bool IsValid => Manifest is not null && Diagnostics.Count == 0;
}

internal static partial class PackageValidator
{
    private static readonly JsonSerializerOptions ManifestJson = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    [GeneratedRegex("^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*)+\\z")]
    private static partial Regex PluginIdPattern();

    [GeneratedRegex("^[0-9]+\\.[0-9]+\\.[0-9]+\\z")]
    private static partial Regex VersionPattern();

    internal static bool IsSafePluginId(string? id) => id is { Length: > 0 and <= 128 } && PluginIdPattern().IsMatch(id);

    public static async Task<ValidatedPackage> ValidateAsync(string packagePath, Version hostVersion, CancellationToken cancellationToken)
    {
        packagePath = System.IO.Path.GetFullPath(packagePath);
        PluginManifest? manifest = null;
        List<PluginDiagnostic> diagnostics = [];
        IReadOnlyList<PackageFile> files = [];
        string? fingerprint = null;
        try
        {
            files = Inventory(packagePath);
            var manifestFile = files.SingleOrDefault(f => f.RelativePath == "plugin.json");
            if (manifestFile is null)
                throw new JsonException("The package must contain plugin.json at its root.");
            var text = await File.ReadAllTextAsync(manifestFile.FullPath, cancellationToken);
            using var document = JsonDocument.Parse(text);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw new JsonException("The manifest must be a JSON object.");
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in document.RootElement.EnumerateObject())
                if (!names.Add(property.Name))
                    throw new JsonException($"Duplicate manifest property '{property.Name}'.");
            manifest = JsonSerializer.Deserialize<PluginManifest>(text, ManifestJson)
                ?? throw new JsonException("The manifest cannot be null.");
            ValidateManifest(manifest);
            manifest = manifest with { Capabilities = Array.AsReadOnly(manifest.Capabilities.ToArray()) };
            if (manifest.ApiVersion != WorkbenchApi.Version)
                diagnostics.Add(new(PluginDiagnosticCode.IncompatibleApi,
                    $"Package API {manifest.ApiVersion} is incompatible with host API {WorkbenchApi.Version}."));
            if (System.Version.Parse(manifest.MinimumHostVersion) > hostVersion)
                diagnostics.Add(new(PluginDiagnosticCode.IncompatibleHost,
                    $"Package requires host {manifest.MinimumHostVersion}; this host is {hostVersion}."));
            if (!files.Any(f => f.RelativePath.Equals(manifest.EntryAssembly, StringComparison.Ordinal)))
                diagnostics.Add(new(PluginDiagnosticCode.MissingAssembly, $"Entry assembly '{manifest.EntryAssembly}' is missing."));
            fingerprint = await FingerprintAsync(files, cancellationToken);
        }
        catch (JsonException error)
        {
            diagnostics.Add(new(PluginDiagnosticCode.InvalidManifest, error.Message));
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException)
        {
            diagnostics.Add(new(PluginDiagnosticCode.InvalidPackage, error.Message));
        }
        return new(packagePath, manifest, fingerprint, files, Array.AsReadOnly(diagnostics.ToArray()));
    }

    private static void ValidateManifest(PluginManifest manifest)
    {
        if (!IsSafePluginId(manifest.Id))
            throw new JsonException("Plugin ID must be a lowercase namespaced identifier, for example com.example.notes.");
        if (string.IsNullOrWhiteSpace(manifest.Name) || manifest.Name.Length > 200)
            throw new JsonException("Plugin name is required and must be at most 200 characters.");
        if (!IsVersion(manifest.Version) || !IsVersion(manifest.MinimumHostVersion))
            throw new JsonException("version and minimumHostVersion must be three-part numeric versions.");
        if (!IsSafeRelativePath(manifest.EntryAssembly) || !manifest.EntryAssembly.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
            throw new JsonException("entryAssembly must be a contained relative .dll path using forward slashes.");
        if (string.IsNullOrWhiteSpace(manifest.EntryType) || manifest.EntryType.Length > 500)
            throw new JsonException("entryType is required and must be at most 500 characters.");
        if (manifest.Description is null || manifest.Description.Length > 4000)
            throw new JsonException("description is required and must be at most 4000 characters.");
        if (manifest.Capabilities is null || manifest.Capabilities.Count > 100 ||
            manifest.Capabilities.Any(c => string.IsNullOrWhiteSpace(c) || c.Length > 200))
            throw new JsonException("capabilities must be an array of nonempty descriptions.");
    }

    private static bool IsVersion(string? value) => value is not null && VersionPattern().IsMatch(value) && System.Version.TryParse(value, out _);

    internal static bool IsSafeRelativePath(string? value) =>
        !string.IsNullOrWhiteSpace(value) && !System.IO.Path.IsPathRooted(value) &&
        value.Split('/').All(IsSafeFileName);

    private static bool IsSafeFileName(string part) => part.Length > 0 && part is not "." and not ".." &&
        !part.Any(c => c < 32 || c is '\\' or ':' or '<' or '>' or '"' or '|' or '?' or '*') &&
        !part.EndsWith(' ') && !part.EndsWith('.') && !IsWindowsDeviceName(part);

    private static bool IsWindowsDeviceName(string part)
    {
        var stem = part.Split('.')[0].TrimEnd(' ').ToUpperInvariant();
        return stem is "CON" or "PRN" or "AUX" or "NUL" or "CONIN$" or "CONOUT$" ||
            stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) || stem.StartsWith("LPT", StringComparison.Ordinal)) &&
            "123456789¹²³".Contains(stem[3]);
    }

    internal static IReadOnlyList<PackageFile> Inventory(string root)
    {
        FileSystemGuard.RejectLinks(root);
        if (!Directory.Exists(root))
            throw new IOException($"Package directory '{root}' does not exist.");
        List<PackageFile> files = [];
        var portableNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        Walk(root);
        return Array.AsReadOnly(files.OrderBy(f => f.RelativePath, StringComparer.Ordinal).ToArray());

        void Walk(string directory)
        {
            foreach (var child in Directory.EnumerateFileSystemEntries(directory).Order(StringComparer.Ordinal))
            {
                var attributes = File.GetAttributes(child);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException($"Symbolic links and reparse points are not allowed: '{child}'.");
                var relative = System.IO.Path.GetRelativePath(root, child).Replace(System.IO.Path.DirectorySeparatorChar, '/');
                if (!IsSafeRelativePath(relative) || !portableNames.Add(relative))
                    throw new IOException($"Unsafe or case-colliding package path: '{relative}'.");
                if ((attributes & FileAttributes.Directory) != 0)
                    Walk(child);
                else
                {
                    FileSystemGuard.EnsureRegularFile(child);
                    files.Add(new(relative, child));
                }
            }
        }
    }

    private static async Task<string> FingerprintAsync(IReadOnlyList<PackageFile> files, CancellationToken cancellationToken)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var file in files)
        {
            cancellationToken.ThrowIfCancellationRequested();
            hash.AppendData(Encoding.UTF8.GetBytes(file.RelativePath));
            hash.AppendData([0]);
            await using var stream = new FileStream(file.FullPath, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, useAsync: true);
            hash.AppendData(await SHA256.HashDataAsync(stream, cancellationToken));
        }
        return Convert.ToHexString(hash.GetHashAndReset());
    }
}
