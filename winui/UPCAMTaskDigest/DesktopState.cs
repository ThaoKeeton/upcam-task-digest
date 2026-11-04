using System.Text.Json.Nodes;

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
    public string ModelCacheStatus { get; set; } = "尚未刷新当前 API Key 可用模型。";
    public string ManualStart { get; set; } = DateTime.Now.Date.ToString("yyyy-MM-dd HH:mm");
    public string ManualEnd { get; set; } = DateTime.Now.ToString("yyyy-MM-dd HH:mm");
    public string Output { get; set; } = "选择时间范围后，先运行只读诊断。";
    public bool ExportAvailable { get; set; }

    public string Text(string key, string fallback = "") => Settings[key]?.GetValue<string>() ?? fallback;
    public bool Flag(string key) => Settings[key]?.GetValue<bool>() ?? false;
    public int Number(string key, int fallback) => Settings[key]?.GetValue<int>() ?? fallback;
    public void Set(string key, JsonNode? value)
    {
        Settings[key] = value;
        Dirty = true;
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
        if (result["settings"] is JsonObject settings) Settings = (JsonObject)settings.DeepClone();
        if (result["secret_presence"] is JsonObject presence) SecretPresence = (JsonObject)presence.DeepClone();
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

    public void ApplyModels(JsonObject result)
    {
        Models = result["models"]?.DeepClone().AsArray() ?? new JsonArray();
        Efforts = result["reasoning_efforts"]?.DeepClone().AsObject() ?? new JsonObject();
        FastModels = result["fast_models"]?.DeepClone().AsArray() ?? new JsonArray();
        string? fetched = result["fetched_at_utc"]?.GetValue<string>() ?? result["model_cache_fetched_at_utc"]?.GetValue<string>();
        bool matches = result["model_cache_matches_key"]?.GetValue<bool>() == true;
        ModelCacheStatus = result["model_cache_error"]?.GetValue<string>() ??
            (fetched is null ? "尚无本机模型缓存；点击刷新后获取当前 API Key 可用模型。" :
             $"缓存 {Models.Count} 个 API 模型 · {fetched}" + (matches ? "" : " · 当前 Key 尚未匹配，请手动刷新。"));
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
}
