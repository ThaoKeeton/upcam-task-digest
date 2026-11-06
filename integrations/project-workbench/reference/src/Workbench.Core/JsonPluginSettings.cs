using System.Text.Json;
using System.Text.RegularExpressions;
using Workbench.Sdk;

namespace Workbench.Core;

internal sealed partial class JsonPluginSettings(string directory, Action<PluginDiagnostic> diagnostic) : IPluginSettings
{
    private readonly string _path = Path.Combine(directory, "settings.json");
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Dictionary<string, JsonElement>? _values;

    [GeneratedRegex("^[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\\z")]
    private static partial Regex KeyPattern();

    internal static void ValidateKey(string key)
    {
        if (key is not { Length: > 0 and <= 128 } || !KeyPattern().IsMatch(key))
            throw new ArgumentException("Setting and contribution IDs must be safe names of at most 128 characters; paths are not allowed.", nameof(key));
    }

    public async Task<T> GetAsync<T>(string key, T defaultValue, CancellationToken cancellationToken = default)
    {
        ValidateKey(key);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            await LoadAsync(cancellationToken);
            if (!_values!.TryGetValue(key, out var value))
                return defaultValue;
            try { return value.Deserialize<T>(AtomicJsonFile.Options)!; }
            catch (JsonException error)
            {
                diagnostic(new(PluginDiagnosticCode.SettingsCorrupt, $"Setting '{key}' has an incompatible JSON value: {error.Message}"));
                return defaultValue;
            }
        }
        finally { _gate.Release(); }
    }

    public async Task SetAsync<T>(string key, T value, CancellationToken cancellationToken = default)
    {
        ValidateKey(key);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            await LoadAsync(cancellationToken);
            var updated = new Dictionary<string, JsonElement>(_values!, StringComparer.Ordinal)
            {
                [key] = JsonSerializer.SerializeToElement(value, AtomicJsonFile.Options)
            };
            await AtomicJsonFile.WriteAsync(_path, updated, cancellationToken);
            _values = updated;
        }
        finally { _gate.Release(); }
    }

    private async Task LoadAsync(CancellationToken cancellationToken)
    {
        FileSystemGuard.RejectLinks(_path);
        if (_values is not null)
            return;
        if (!File.Exists(_path))
        {
            _values = new(StringComparer.Ordinal);
            return;
        }
        FileSystemGuard.EnsureRegularFile(_path);
        try
        {
            await using var stream = new FileStream(_path, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, useAsync: true);
            _values = await JsonSerializer.DeserializeAsync<Dictionary<string, JsonElement>>(stream, AtomicJsonFile.Options, cancellationToken)
                ?? throw new JsonException("The settings document cannot be null.");
        }
        catch (JsonException error)
        {
            diagnostic(new(PluginDiagnosticCode.SettingsCorrupt, $"Corrupt settings at '{_path}'; defaults are being used. {error.Message}"));
            _values = new(StringComparer.Ordinal);
        }
    }
}
