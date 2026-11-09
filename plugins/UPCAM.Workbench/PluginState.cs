using System.Text.Json;
using System.Text.Json.Nodes;
using Upcam.Desktop;

namespace Upcam.Workbench;

/// <summary>Documents belong to the plugin instance, never to a navigation control.</summary>
internal sealed class PluginState
{
    internal readonly object Sync = new();
    private readonly Dictionary<string, ProfileDraft> _profiles = new(StringComparer.OrdinalIgnoreCase);
    internal ProfileDraft Current { get; private set; }
    internal bool Ready { get; set; }
    internal bool Busy { get; set; }
    internal bool BackgroundActivity { get; set; }
    internal string ActiveOperation { get; set; } = "";
    internal bool SavedAutoEnabled { get; set; }
    internal string? BindingError { get; set; }
    internal string Status { get; set; } = "Open a page or run a command to start the backend.";
    internal int DigestTab { get; set; }
    internal int SettingsTab { get; set; }
    internal string AdoptionPath { get; set; } = "";
    internal long OperationRevision { get; set; }
    internal string OperationResult { get; set; } = "0 · No operation yet.";
    internal long ScheduleCheckRevision { get; set; }
    internal bool LastScheduleCheckSucceeded { get; set; }
    internal string LastScheduleOperation { get; set; } = "";
    internal string ScheduleCheckResult { get; set; } = "0 · 尚未检查已保存的计划。";
    internal string SchedulerStatus { get; set; } = "主机自动任务尚未启动。";
    internal bool ResumeOnStartup { get; set; }
    internal string LastSchedulerError { get; set; } = "";
    internal event EventHandler<bool>? Changed;

    internal PluginState(string path) => Current = GetProfile(path);

    internal ProfileDraft GetProfile(string path)
    {
        if (!_profiles.TryGetValue(path, out ProfileDraft? draft))
            _profiles[path] = draft = new ProfileDraft(path);
        return draft;
    }

    internal void SelectProfile(string path) => Current = GetProfile(path);
    internal void Notify(bool rebuild = true) => Changed?.Invoke(this, rebuild);
    internal void ClearSecretDrafts()
    {
        lock (Sync)
            foreach (ProfileDraft profile in _profiles.Values) profile.Data.SecretDrafts.Clear();
    }

    internal void Edit(ProfileDraft profile, Action<DesktopState> action)
    {
        lock (Sync)
            if (ReferenceEquals(profile, Current) && (!Busy || BackgroundActivity)) action(profile.Data);
    }

    internal void EditDraft(ProfileDraft profile, Action<ProfileDraft> action)
    {
        lock (Sync)
            if (ReferenceEquals(profile, Current) && (!Busy || BackgroundActivity)) action(profile);
    }
}

internal sealed class ProfileDraft(string directory)
{
    internal string Directory { get; } = directory;
    internal DesktopState Data { get; } = new();
    internal bool Loaded { get; private set; }
    internal string MemberId { get; set; } = "";
    internal int AccountTab { get; set; }
    internal string MemberName { get; set; } = "";
    internal string MemberImport { get; set; } = "";
    internal string StatusId { get; set; } = "";
    internal string StatusName { get; set; } = "";
    internal bool StatusResolved { get; set; }
    internal string StatusImport { get; set; } = "";
    internal string FilterField { get; set; } = "executorId";
    internal string FilterValue { get; set; } = "";
    internal string ExportPath { get; set; } = "";
    internal bool DetailedExportConsent { get; set; }
    internal bool ClearCredentialsConfirmed { get; set; }
    internal string HistoryDetail { get; set; } = "";
    internal string SelectedHistoryId { get; set; } = "";

    internal void ApplyInitialization(JsonObject result)
    {
        if (result["application_version"]?.GetValue<string>() != "0.4.4")
            throw new InvalidOperationException("The backend version does not match this plugin. Reinstall the complete package.");
        // Reopening a profile restores fresh disk metadata without overwriting its unsaved document.
        bool preserve = Loaded && (Data.Dirty || Data.SecretDrafts.Any(x => x.Value.Length > 0));
        var secrets = Data.SecretDrafts.ToArray();
        JsonObject input = (JsonObject)result.DeepClone();
        if (preserve) input.Remove("settings");
        Data.Apply(input);
        if (preserve)
        {
            foreach (var item in secrets) Data.SecretDrafts[item.Key] = item.Value;
            Data.Dirty = true;
        }
        Loaded = true;
    }

    internal static JsonObject ParseMap(string text)
    {
        try
        {
            if (JsonNode.Parse(text) is not JsonObject result || result.Any(x =>
                x.Value is not JsonValue value || !value.TryGetValue<string>(out _)))
                throw new JsonException();
            return result;
        }
        catch (JsonException)
        {
            throw new InvalidOperationException("Enter a JSON object whose keys are API IDs and values are verified names.");
        }
    }
}

internal static class ProfileBinding
{
    internal const string SettingsKey = "profile-directory";

    internal static string Canonicalize(string path, bool requireExistingProfile = false)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path) ||
                path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal))
                throw new IOException();
            // Use the exact same Windows identity as the worker handshake, including existing 8.3 aliases.
            string canonical = PluginWorkerClient.CanonicalizeDirectory(path, mustExist: requireExistingProfile);
            string? root = Path.GetPathRoot(canonical);
            if (root is null || string.Equals(canonical, Path.TrimEndingDirectorySeparator(root), StringComparison.OrdinalIgnoreCase))
                throw new IOException();
            // Check every existing component, including files used to identify a legacy profile.
            if (canonical.IndexOf(':', 2) >= 0) throw new IOException();
            for (DirectoryInfo? part = new(canonical); part is not null; part = part.Parent)
                if (part.LinkTarget is not null || (part.Exists && (part.Attributes & FileAttributes.ReparsePoint) != 0)) throw new IOException();
            if (File.Exists(canonical)) throw new IOException();
            foreach (string name in new[] { "settings.json", "credentials.dpapi", "history.sqlite3", "openai_models.json", "delivery_ledger.sqlite3" })
            {
                string item = Path.Combine(canonical, name);
                if (new FileInfo(item).LinkTarget is not null) throw new IOException();
                if ((File.Exists(item) || Directory.Exists(item)) &&
                    (File.GetAttributes(item) & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0)
                    throw new IOException();
            }
            if (requireExistingProfile)
            {
                string settings = Path.Combine(canonical, "settings.json");
                if (!Directory.Exists(canonical) || !File.Exists(settings) || new FileInfo(settings).Length > 2 * 1024 * 1024)
                    throw new IOException();
                using var document = JsonDocument.Parse(File.ReadAllText(settings));
                var value = document.RootElement;
                if (value.ValueKind != JsonValueKind.Object ||
                    !value.TryGetProperty("schema_version", out var version) || version.GetInt32() != 1 ||
                    !value.TryGetProperty("owner_names", out var owners) || owners.ValueKind != JsonValueKind.Object ||
                    !value.TryGetProperty("status_names", out var statuses) || statuses.ValueKind != JsonValueKind.Object ||
                    !value.TryGetProperty("project_id", out var project) || project.ValueKind != JsonValueKind.String)
                    throw new IOException();
            }
            return canonical;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or JsonException or InvalidOperationException or FormatException or OverflowException)
        {
            throw new InvalidOperationException("Choose an absolute local profile folder with regular files and no links. For adoption, select an existing UPCAM settings.json profile.");
        }
    }
}
