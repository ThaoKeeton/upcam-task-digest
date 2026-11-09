using System.Text.Json.Nodes;
using System.Globalization;

namespace Upcam.Desktop;

internal sealed class DesktopState
{
    public JsonObject Settings { get; private set; } = new();
    public JsonObject SecretPresence { get; private set; } = new();
    public JsonObject Efforts { get; set; } = new();
    public JsonArray Models { get; set; } = new();
    public JsonArray FastModels { get; set; } = new();
    public JsonArray History { get; set; } = new();
    public Dictionary<string, string> SecretDrafts { get; } = new();
    internal static readonly string[] GeneralKeys = { "ui_language", "ui_theme", "tray_icon_enabled",
        "close_to_tray", "start_in_tray", "auto_check_updates", "start_with_windows" };
    public JsonObject GeneralPreferences { get; private set; } = new();
    public JsonObject UpdateState { get; set; } = new();
    public bool GeneralDirty { get; private set; }
    public string UpdateTokenDraft { get; set; } = "";
    public bool ClearUpdateToken { get; set; }
    public HashSet<string> CandidateOwners { get; } = new();
    public HashSet<string> CandidateStatuses { get; } = new();
    public Dictionary<string, string> StatusSuggestions { get; } = new();
    public bool ScopeVerified => Flag("api_contract_verified") &&
        (Settings["view"] as JsonObject)?["verified"]?.GetValue<bool>() == true &&
        !string.IsNullOrEmpty((Settings["view"] as JsonObject)?["evidence"]?.GetValue<string>());
    public bool Dirty { get; set; }
    public bool LedgerReady { get; set; }
    public string LedgerMessage { get; set; } = "";
    public string DataDirectory { get; set; } = "";
    public string ApplicationVersion { get; set; } = "";
    public string RulesetRevision { get; set; } = "";
    private string? _modelCacheFetched, _modelCacheError;
    private bool _cacheMatches;
    public string ModelCacheStatus => _modelCacheError is not null ? UiStrings.T(_modelCacheError) :
        _modelCacheFetched is null ? UiStrings.T("尚无本机模型缓存；点击刷新后获取当前 API Key 可用模型。") :
        UiStrings.F("缓存 {0} 个 API 模型 · {1}", Models.Count, _modelCacheFetched) +
        (_cacheMatches ? "" : UiStrings.T(" · 当前 Key 尚未匹配，请手动刷新。"));
    private string _manualStart = DateTime.Now.Date.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
    private string _manualEnd = DateTime.Now.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
    public string ManualStart
    {
        get => _manualStart;
        set { if (_manualStart != value) InvalidateDiagnostic(); _manualStart = value; }
    }
    public string ManualEnd
    {
        get => _manualEnd;
        set { if (_manualEnd != value) InvalidateDiagnostic(); _manualEnd = value; }
    }
    public string Output { get; set; } = "选择时间范围后，先运行只读诊断。";
    public bool ExportAvailable { get; set; }
    public string DiagnosticFileStem { get; set; } = "";
    public string DiagnosticText { get; set; } = "";

    private void InvalidateDiagnostic()
    {
        ExportAvailable = false;
        DiagnosticFileStem = "";
        DiagnosticText = "";
    }

    public string Text(string key, string fallback = "") => Settings[key]?.GetValue<string>() ?? fallback;
    public bool Flag(string key) => Settings[key]?.GetValue<bool>() ?? false;
    public int Number(string key, int fallback) => Settings[key]?.GetValue<int>() ?? fallback;
    public void Set(string key, JsonNode? value)
    {
        bool identityChanged = (key is "tenant_id" or "project_id") &&
            Settings[key]?.ToJsonString() != value?.ToJsonString();
        if (identityChanged)
        {
            Settings["task_number_prefix"] = "";
            Settings["task_link_view_id"] = "";
            Settings["task_display_scope"] = null;
        }
        Settings[key] = value;
        if (key is "task_number_prefix" or "task_link_view_id")
        {
            Settings["task_display_scope"] = (Text("task_number_prefix") != "" || Text("task_link_view_id") != "")
                ? new JsonObject { ["tenant_id"] = Text("tenant_id"), ["project_id"] = Text("project_id") }
                : null;
            InvalidateDiagnostic();
        }
        Dirty = true;
        if (key is "owner_names" or "status_names" or "resolved_status_ids" or "max_tasks_per_run" or "max_concurrent_tasks"
            or "dingtalk_mention_mode" or "dingtalk_mentions")
            InvalidateDiagnostic();
        if (key is "tenant_id" or "project_id" or "include_archived") InvalidateScope();
    }
    public JsonObject Map(string key) => Settings[key] as JsonObject ?? new JsonObject();
    public JsonObject Filters => (Settings["view"] as JsonObject)?["task_equals"] as JsonObject ?? new JsonObject();
    public void SetFilters(JsonObject filters)
    {
        if (Settings["view"] is not JsonObject) Settings["view"] = new JsonObject();
        Settings["view"]!["task_equals"] = filters.DeepClone();
        InvalidateScope();
        Dirty = true;
    }

    public void SetSecretDraft(string key, string value)
    {
        SecretDrafts[key] = value;
        if (!string.IsNullOrEmpty(value) && (key is "teambition_app_id" or "teambition_app_secret")) InvalidateScope();
    }

    private void InvalidateScope()
    {
        InvalidateDiagnostic();
        Settings["api_contract_verified"] = false;
        if (Settings["view"] is JsonObject view)
        {
            view["verified"] = false;
            view["evidence"] = "";
        }
        CandidateOwners.Clear();
        CandidateStatuses.Clear();
        StatusSuggestions.Clear();
    }

    public void ApplyProbe(JsonObject probe)
    {
        foreach (JsonNode? id in probe["owner_ids"]!.AsArray()) CandidateOwners.Add(id!.GetValue<string>());
        foreach (JsonNode? id in probe["status_ids"]!.AsArray()) CandidateStatuses.Add(id!.GetValue<string>());
        foreach (var item in probe["status_name_suggestions"]!.AsObject())
            StatusSuggestions[item.Key] = item.Value!.GetValue<string>();
    }

    public void Apply(JsonObject result)
    {
        if (result["settings"] is JsonObject settings)
        {
            Settings = (JsonObject)settings.DeepClone();
            InvalidateDiagnostic();
        }
        if (result["secret_presence"] is JsonObject presence) SecretPresence = (JsonObject)presence.DeepClone();
        if (!GeneralDirty && result["settings"] is JsonObject saved)
            GeneralPreferences = new JsonObject(GeneralKeys.Select(key =>
                KeyValuePair.Create<string, JsonNode?>(key, saved[key]?.DeepClone())));
        if (result["scope_probe"] is JsonObject probe) ApplyProbe(probe);
        LedgerReady = result["ledger_ready"]?.GetValue<bool>() ?? LedgerReady;
        LedgerMessage = result["ledger_message"]?.GetValue<string>() ?? LedgerMessage;
        DataDirectory = result["data_directory"]?.GetValue<string>() ?? DataDirectory;
        ApplicationVersion = result["application_version"]?.GetValue<string>() ?? ApplicationVersion;
        RulesetRevision = result["ruleset_revision"]?.GetValue<string>() ?? RulesetRevision;
        if (result["models"] is JsonArray) ApplyModels(result);
        if (result["history"] is JsonArray history) History = (JsonArray)history.DeepClone();
        SecretDrafts.Clear();
        Dirty = false;
    }

    public string GeneralText(string key, string fallback = "system") => GeneralPreferences[key]?.GetValue<string>() ?? fallback;
    public bool GeneralFlag(string key) => GeneralPreferences[key]?.GetValue<bool>() == true;
    public void SetGeneral(string key, JsonNode value)
    {
        if (!GeneralKeys.Contains(key)) throw new ArgumentException("Unknown general preference");
        GeneralPreferences[key] = value;
        GeneralDirty = true;
    }
    public JsonObject GeneralSavePayload() => new()
    {
        ["preferences"] = GeneralPreferences.DeepClone(),
        ["github_token"] = UpdateTokenDraft,
        ["clear_github_token"] = ClearUpdateToken
    };
    public void ApplyGeneral(JsonObject result)
    {
        if (result["preferences"] is JsonObject preferences)
        {
            GeneralPreferences = (JsonObject)preferences.DeepClone();
            foreach (string key in GeneralKeys) Settings[key] = preferences[key]?.DeepClone();
        }
        if (result["secret_presence"] is JsonObject presence) SecretPresence = (JsonObject)presence.DeepClone();
        GeneralDirty = false;
        UpdateTokenDraft = "";
        ClearUpdateToken = false;
    }

    public void ApplyModels(JsonObject result)
    {
        Models = result["models"]?.DeepClone().AsArray() ?? new JsonArray();
        Efforts = result["reasoning_efforts"]?.DeepClone().AsObject() ?? new JsonObject();
        FastModels = result["fast_models"]?.DeepClone().AsArray() ?? new JsonArray();
        string? fetched = result["fetched_at_utc"]?.GetValue<string>() ?? result["model_cache_fetched_at_utc"]?.GetValue<string>();
        bool matches = result["model_cache_matches_key"]?.GetValue<bool>() == true;
        _modelCacheFetched = fetched;
        _modelCacheError = result["model_cache_error"]?.GetValue<string>();
        _cacheMatches = matches;
    }

    public JsonObject SavePayload() => new()
    {
        ["settings"] = Settings.DeepClone(),
        ["secret_values"] = new JsonObject(SecretDrafts.Select(p => KeyValuePair.Create<string, JsonNode?>(p.Key, JsonValue.Create(p.Value)))),
        ["clear_secrets"] = new JsonArray(),
        ["verification_window"] = WindowPayload()
    };
    public JsonObject WindowPayload(bool scope = false)
    {
        var payload = new JsonObject { ["start"] = ManualStart, ["end"] = ManualEnd };
        if (scope)
        {
            foreach (string key in new[] { "tenant_id", "project_id", "include_archived", "max_tasks_per_run" })
                payload[key] = Settings[key]?.DeepClone();
            payload["task_equals"] = Filters.DeepClone();
        }
        return payload;
    }

    public JsonObject DiagnosticExportPayload(string path)
    {
        var payload = WindowPayload();
        payload["path"] = path;
        return payload;
    }
}
