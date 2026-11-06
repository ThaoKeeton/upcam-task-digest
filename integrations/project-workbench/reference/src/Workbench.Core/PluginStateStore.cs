using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Workbench.Core;

internal sealed record SavedPluginState
{
    public required bool Enabled { get; init; }
    public required string? TrustedFingerprint { get; init; }
}

internal sealed record PluginStateDocument
{
    public required int Version { get; init; }
    public required Dictionary<string, SavedPluginState> Plugins { get; init; }
}

internal sealed class PluginStateStore(string userRoot)
{
    private readonly string _path = Path.Combine(userRoot, "plugin-state.json");
    private readonly ConcurrentQueue<PluginDiagnostic> _diagnostics = new();
    private Dictionary<string, SavedPluginState> _states = new(StringComparer.Ordinal);
    private bool _loaded;
    private static readonly JsonSerializerOptions ReadOptions = new(AtomicJsonFile.Options)
    {
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    internal IReadOnlyList<PluginDiagnostic> Diagnostics => Array.AsReadOnly(_diagnostics.ToArray());
    internal SavedPluginState? Get(string id) => _states.GetValueOrDefault(id);

    internal async Task LoadAsync(CancellationToken cancellationToken)
    {
        if (_loaded)
            return;
        try
        {
            FileSystemGuard.RejectLinks(_path);
            if (File.Exists(_path))
            {
                FileSystemGuard.EnsureRegularFile(_path);
                await using var stream = new FileStream(_path, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, useAsync: true);
                var document = await JsonSerializer.DeserializeAsync<PluginStateDocument>(stream, ReadOptions, cancellationToken)
                    ?? throw new JsonException("The state document cannot be null.");
                if (document.Version != 1 || document.Plugins is null || document.Plugins.Any(p =>
                    !PackageValidator.IsSafePluginId(p.Key) || p.Value is null ||
                    p.Value.TrustedFingerprint is { } fingerprint && (fingerprint.Length != 64 || !fingerprint.All(Uri.IsHexDigit))))
                    throw new JsonException("The saved plugin state has an unsupported version or invalid approval fingerprint.");
                _states = document.Plugins;
            }
            _loaded = true;
        }
        catch (Exception error) when (error is JsonException or IOException or UnauthorizedAccessException)
        {
            _diagnostics.Enqueue(new(PluginDiagnosticCode.StateCorrupt,
                $"Saved plugin state at '{_path}' could not be read; all packages default to disabled and untrusted. {error.Message}"));
            _states = new(StringComparer.Ordinal);
            _loaded = true;
        }
    }

    internal async Task SaveAsync(string id, bool enabled, string? fingerprint, CancellationToken cancellationToken)
    {
        await LoadAsync(cancellationToken);
        var updated = new Dictionary<string, SavedPluginState>(_states, StringComparer.Ordinal)
        {
            [id] = new() { Enabled = enabled, TrustedFingerprint = fingerprint }
        };
        await AtomicJsonFile.WriteAsync(_path, new PluginStateDocument { Version = 1, Plugins = updated }, cancellationToken);
        _states = updated;
    }
}
