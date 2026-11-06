using Workbench.Sdk;

namespace Workbench.Core;

internal sealed class PluginRegistrationContext : IPluginContext, IDisposable
{
    private readonly object _sync = new();
    private readonly CancellationTokenSource _lifetime = new();
    private readonly Dictionary<string, IPluginView> _views = new(StringComparer.Ordinal);
    private readonly Dictionary<string, PluginCommand> _commands = new(StringComparer.Ordinal);
    private readonly Dictionary<string, PluginSetting> _settings = new(StringComparer.Ordinal);
    private bool _open = true;
    private bool _active;

    public PluginRegistrationContext(string pluginId, string dataDirectory, IPluginSettings settings)
    {
        PluginId = pluginId;
        DataDirectory = dataDirectory;
        Settings = settings;
        Lifetime = _lifetime.Token;
    }

    public string PluginId { get; }
    public string DataDirectory { get; }
    public CancellationToken Lifetime { get; }
    public IPluginSettings Settings { get; }

    public void RegisterView(IPluginView view)
    {
        ArgumentNullException.ThrowIfNull(view);
        ValidateMetadata(view.Id, view.Title);
        lock (_sync)
        {
            EnsureOpen();
            if (!_views.TryAdd(view.Id, view))
                throw new InvalidOperationException($"Duplicate view ID '{view.Id}'.");
        }
    }

    public void RegisterCommand(PluginCommand command)
    {
        ArgumentNullException.ThrowIfNull(command);
        ValidateMetadata(command.Id, command.Title);
        ArgumentNullException.ThrowIfNull(command.ExecuteAsync);
        lock (_sync)
        {
            EnsureOpen();
            if (!_commands.TryAdd(command.Id, command))
                throw new InvalidOperationException($"Duplicate command ID '{command.Id}'.");
        }
    }

    public void RegisterSetting(PluginSetting setting)
    {
        ArgumentNullException.ThrowIfNull(setting);
        ValidateMetadata(setting.Id, setting.Title);
        ArgumentNullException.ThrowIfNull(setting.DefaultValue);
        lock (_sync)
        {
            EnsureOpen();
            if (!_settings.TryAdd(setting.Id, setting))
                throw new InvalidOperationException($"Duplicate setting ID '{setting.Id}'.");
        }
    }

    private static void ValidateMetadata(string id, string title)
    {
        JsonPluginSettings.ValidateKey(id);
        if (string.IsNullOrWhiteSpace(title) || title.Length > 200)
            throw new ArgumentException("A contribution title of at most 200 characters is required.", nameof(title));
    }

    private void EnsureOpen()
    {
        if (!_open)
            throw new InvalidOperationException("Contributions can only be registered during InitializeAsync.");
    }

    internal void Commit()
    {
        lock (_sync)
        {
            EnsureOpen();
            _open = false;
            _active = true;
        }
    }

    internal PluginContributions Snapshot()
    {
        lock (_sync)
        {
            return new(
                Array.AsReadOnly(_views.Values.Select(v => new PluginViewRegistration(PluginId, v)).ToArray()),
                Array.AsReadOnly(_commands.Values.Select(c => new PluginCommandRegistration(PluginId, c.Id, c.Title, c.Description)).ToArray()),
                Array.AsReadOnly(_settings.Values.Select(s => new PluginSettingRegistration(PluginId, s)).ToArray()));
        }
    }

    internal PluginCommand? FindCommand(string id)
    {
        lock (_sync)
            return _active ? _commands.GetValueOrDefault(id) : null;
    }

    internal Exception? Close()
    {
        lock (_sync)
        {
            _open = false;
            _active = false;
            _views.Clear();
            _commands.Clear();
            _settings.Clear();
        }
        try { _lifetime.Cancel(); }
        catch (Exception error) { return error; }
        return null;
    }

    public void Dispose() => _lifetime.Dispose();
}
