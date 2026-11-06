using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using Upcam.Desktop;

namespace Upcam.Workbench;

internal sealed partial class DigestPage
{
    private FrameworkElement BuildProfile(ProfileDraft profile)
    {
        var adoption = Identify(new TextBox { Header = "现有独立版数据目录", Text = _state.AdoptionPath,
            PlaceholderText = @"C:\Users\user\...\UPCAMTaskDigest" }, "profile-folder", "Existing standalone profile folder");
        adoption.TextChanging += (_, _) =>
        {
            lock (_state.Sync) if (!_state.Busy || _state.BackgroundActivity) _state.AdoptionPath = adoption.Text;
        };
        return LocalScroll(Column(
            Paragraph(_state.BindingError is null ? "Active profile" : "候选默认目录（请明确选择后连接）", 18), Paragraph(profile.Directory),
            Paragraph(_state.BindingError ?? ""),
            Paragraph("The default profile belongs to this Workbench user root. Credentials use current-Windows-user DPAPI. Host JSON stores only the folder binding. Adopting a standalone profile opens its same settings, history and delivery ledger; it does not copy or migrate them."),
            Paragraph("Exit the standalone app before opening its folder here. The shared singleton permits only one backend. Disabling this plugin stops its worker and releases the profile for standalone 0.4.3 rollback."),
            adoption,
            Toolbar(
                ActionButton("Bind existing standalone folder", "adopt-profile", async () =>
                {
                    string path;
                    lock (_state.Sync) path = _state.AdoptionPath;
                    await _session.SwitchProfileAsync(path, adopt: true);
                }),
                ActionButton("Use Workbench default profile", "default-profile", async () =>
                { await _session.SwitchProfileAsync(_session.DefaultProfile, adopt: false); })),
            Paragraph("Unsaved edits are retained per profile during this plugin session. Save before disabling or exiting; API 1 has no shared save/discard dialog. Profile switch stops the old worker before opening the selected profile."),
            Paragraph("Workbench integration limits", 18), Paragraph(PluginSession.ActivationNotice),
            Paragraph("The plugin inherits the host theme and creates no separate window. API 1 has no host language/preference bridge or native picker window handle. Existing standalone language, theme, tray, autostart and update settings remain in the profile for rollback.")));
    }

    private FrameworkElement BuildAccounts(ProfileDraft profile)
    {
        var tabs = Identify(new TabView { IsAddTabButtonVisible = false, CanDragTabs = false, CanReorderTabs = false,
            TabWidthMode = TabViewWidthMode.SizeToContent }, "account-tabs", "Service accounts");
        tabs.TabItems.Add(new TabViewItem { Header = "Teambition", IsClosable = false,
            Content = LocalScroll(Column(
                SecretInput(profile, "Teambition App ID", "teambition_app_id"),
                SecretInput(profile, "Teambition App Secret", "teambition_app_secret"),
                Paragraph("Saved values are never displayed. Blank keeps a saved secret; new values are saved only by Save business settings. Credentials remain in current-user DPAPI, never Workbench settings or logs."),
                ActionButton("Read-only connection test", "test-teambition", async () =>
                { await _session.CallAsync("test-teambition", "test_teambition", _ => null, requireSaved: true); }))) });
        var mode = Identify(new ComboBox { Header = "钉钉艾特标识类型", HorizontalAlignment = HorizontalAlignment.Stretch }, "dingtalk-mention-mode", "DingTalk mention identifier type");
        mode.Items.Add("mobile"); mode.Items.Add("user_id");
        mode.SelectedItem = profile.Data.Text("dingtalk_mention_mode", "mobile");
        mode.SelectionChanged += (_, _) =>
        {
            if (mode.SelectedItem is string value) _state.Edit(profile, data => data.Set("dingtalk_mention_mode", value));
        };
        tabs.TabItems.Add(new TabViewItem { Header = "DingTalk", IsClosable = false,
            Content = LocalScroll(Column(
                SecretInput(profile, "DingTalk robot Webhook", "dingtalk_webhook"),
                SecretInput(profile, "DingTalk signing secret", "dingtalk_signing_secret"), mode,
                SettingText(profile, "Mention specific members (optional, at most 50)", "dingtalk_mentions", "dingtalk-mentions", multiline: true),
                Paragraph("Enter group members' phone numbers or DingTalk user IDs, separated by lines, commas, semicolons or spaces. Names, employee numbers and Teambition member IDs are not valid. Blank mentions nobody; this never adds @all."),
                ActionButton("Check local robot configuration", "check-dingtalk", async () =>
                { await _session.CallAsync("check-dingtalk", "validate_dingtalk_config", _ => null, requireSaved: true); }),
                Paragraph("The check validates local syntax only; it neither sends a test message nor verifies group membership."))) });
        tabs.TabItems.Add(new TabViewItem { Header = "OpenAI", IsClosable = false, Content = BuildModels(profile) });
        tabs.SelectionChanged += (_, _) => _state.EditDraft(profile, p => p.AccountTab = tabs.SelectedIndex);
        tabs.SelectedIndex = Math.Clamp(profile.AccountTab, 0, 2);
        return tabs;
    }

    private FrameworkElement SecretInput(ProfileDraft profile, string label, string key)
    {
        var settingsAtCreation = profile.Data.Settings;
        var input = Identify(new PasswordBox { Header = Label(label),
            Password = profile.Data.SecretDrafts.GetValueOrDefault(key, ""),
            PlaceholderText = profile.Data.SecretPresence[key]?.GetValue<bool>() == true ? "Saved locally · leave blank to keep" : "Not saved locally",
            PasswordRevealMode = PasswordRevealMode.Hidden }, "secret-" + key, label);
        input.PasswordChanged += (_, _) => _state.Edit(profile, data =>
        {
            if (ReferenceEquals(data.Settings, settingsAtCreation)) data.SetSecretDraft(key, input.Password);
        });
        return input;
    }

    private FrameworkElement BuildModels(ProfileDraft profile)
    {
        var data = profile.Data;
        string current = data.Text("openai_model", "gpt-6-luna");
        var model = Identify(new ComboBox { Header = "API 模型", HorizontalAlignment = HorizontalAlignment.Stretch }, "openai-model", "OpenAI API model");
        foreach (string id in data.Models.Select(x => Value(x, "id")).Append(current).Where(x => x.Length > 0).Distinct().Order()) model.Items.Add(id);
        model.SelectedItem = current;
        var effort = Identify(new ComboBox { Header = "推理程度", HorizontalAlignment = HorizontalAlignment.Stretch }, "reasoning-effort", "OpenAI reasoning effort");
        var tier = Identify(new ComboBox { Header = "服务等级", HorizontalAlignment = HorizontalAlignment.Stretch }, "service-tier", "OpenAI service tier");
        bool settingOptions = false;
        void Options()
        {
            settingOptions = true;
            try
            {
                string selected = model.SelectedItem as string ?? current;
                effort.Items.Clear(); effort.Items.Add("default");
                if (data.Efforts[selected] is JsonArray efforts)
                    foreach (JsonNode? value in efforts)
                    {
                        string item = value!.GetValue<string>();
                        if (!effort.Items.Contains(item)) effort.Items.Add(item);
                    }
                string savedEffort = data.Text("openai_reasoning_effort", "default");
                effort.SelectedItem = effort.Items.Contains(savedEffort) ? savedEffort : null;
                tier.Items.Clear(); tier.Items.Add("default");
                if (data.FastModels.Any(x => x?.GetValue<string>() == selected)) tier.Items.Add("fast");
                string savedTier = data.Text("openai_service_tier", "default");
                tier.SelectedItem = tier.Items.Contains(savedTier) ? savedTier : null;
            }
            finally { settingOptions = false; }
        }
        Options();
        model.SelectionChanged += (_, _) =>
        {
            if (model.SelectedItem is not string selected) return;
            _state.Edit(profile, state => state.Set("openai_model", selected));
            Options();
            _state.Edit(profile, state =>
            {
                state.Set("openai_reasoning_effort", effort.SelectedItem as string ?? "default");
                state.Set("openai_service_tier", tier.SelectedItem as string ?? "default");
            });
        };
        effort.SelectionChanged += (_, _) =>
        {
            if (!settingOptions && effort.SelectedItem is string selected) _state.Edit(profile, state => state.Set("openai_reasoning_effort", selected));
        };
        tier.SelectionChanged += (_, _) =>
        {
            if (!settingOptions && tier.SelectedItem is string selected) _state.Edit(profile, state => state.Set("openai_service_tier", selected));
        };
        return LocalScroll(Column(
            SecretInput(profile, "OpenAI API key", "openai_api_key"),
            Identify(Paragraph(data.ModelCacheStatus), "model-cache-status", "Local OpenAI model cache status"),
            ActionButton("Refresh models for saved key", "refresh-models", async () =>
            { await _session.CallAsync("refresh-models", "refresh_models", _ => null, (p, result) => p.Data.ApplyModels(result), requireSaved: true); }),
            model, effort, tier,
            Paragraph("Opening the plugin reads only the local model cache. Refresh makes the requested API call. API key permissions differ from ChatGPT subscriptions. Unknown models offer default effort; only verified models offer Fast. Fast can cost more and does not guarantee faster responses."),
            Check("Clear all saved service credentials and disable automatic sending.", "confirm-clear-credentials", profile.ClearCredentialsConfirmed,
                value => _state.EditDraft(profile, p => p.ClearCredentialsConfirmed = value)),
            ActionButton("Clear saved service credentials", "clear-credentials", async () =>
            {
                await _session.CallAsync("clear-credentials", "clear_credentials", p =>
                {
                    if (!p.ClearCredentialsConfirmed) throw new InvalidOperationException("Confirm clearing saved service credentials first.");
                    return null;
                }, (p, result) =>
                {
                    p.Data.Apply(result); p.ClearCredentialsConfirmed = false; _state.SavedAutoEnabled = false;
                });
            })));
    }

    private FrameworkElement BuildProject(ProfileDraft profile)
    {
        var data = profile.Data;
        var filters = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 140 }, "filter-list", "Extra project filters");
        foreach (var pair in data.Filters) filters.Items.Add(new ListViewItem { Tag = pair.Key, Content = Paragraph($"{pair.Key} = {pair.Value?.GetValue<string>()}") });
        var field = Identify(new ComboBox { Header = "稳定任务字段", HorizontalAlignment = HorizontalAlignment.Stretch }, "filter-field", "Stable task filter field");
        foreach (string name in new[] { "executorId", "tasklistId", "stageId", "tfsId", "scenariofieldconfigId", "visible", "parentTaskId" }) field.Items.Add(name);
        field.SelectedItem = profile.FilterField;
        field.SelectionChanged += (_, _) => { if (field.SelectedItem is string value) _state.EditDraft(profile, p => p.FilterField = value); };
        return LocalScroll(Column(
            SettingText(profile, "Tenant / organization ID", "tenant_id", "tenant-id"),
            SettingText(profile, "Project ID", "project_id", "project-id"),
            Check("Include archived tasks", "include-archived", data.Flag("include_archived"), value => _state.Edit(profile, state => state.Set("include_archived", value))),
            Paragraph("The collector scans all currently readable tasks in this project and their activity pages, then selects events by occurrence time. Deleted or inaccessible tasks cannot be reconstructed. Scope or credential changes invalidate the previous read-only proof."),
            new Expander { Header = $"额外项目筛选（{data.Filters.Count}）", HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = Column(field, DraftText(profile, "Field value / API ID", "filter-value", p => p.FilterValue, (p, value) => p.FilterValue = value), filters,
                    Toolbar(
                        ActionButton("Add / update filter", "filter-put", async () =>
                        {
                            await _session.CallAsync("filter-put", "update_view_filter", p => new JsonObject
                            { ["action"] = "put", ["field"] = p.FilterField, ["value"] = p.FilterValue, ["filters"] = p.Data.Filters.DeepClone() },
                                (p, result) => p.Data.SetFilters(result["filters"]!.AsObject()));
                        }),
                        ActionButton("Remove selected filter", "filter-remove", async () =>
                        {
                            string selected = (filters.SelectedItem as ListViewItem)?.Tag as string ?? "";
                            await _session.CallAsync("filter-remove", "update_view_filter", p => new JsonObject
                            { ["action"] = "remove", ["field"] = selected, ["filters"] = p.Data.Filters.DeepClone() },
                                (p, result) => p.Data.SetFilters(result["filters"]!.AsObject()));
                        }))) },
            Paragraph("Read-only verification and mapping discovery", 18), WindowInputs(profile),
            ActionButton("Verify project and discover mapping IDs", "probe-project", async () =>
            {
                await _session.CallAsync("probe-project", "probe_project", p =>
                {
                    if (p.Data.SecretDrafts.Any(x => x.Value.Length > 0)) throw new InvalidOperationException("Save newly entered Teambition credentials before project verification.");
                    return p.Data.WindowPayload(scope: true);
                }, (p, result) =>
                {
                    p.Data.ApplyProbe(result);
                    p.Data.Output = $"Read {result["task_count"]} tasks and {result["activity_count"]} activities. Verify discovered member/status names, then save business settings.";
                });
            }),
            Paragraph("Saving adopts the current scope and performs a read-only proof when needed. If verification fails, your edits remain saved and automatic sending is disabled. Missing member/status mappings still block sends."),
            SettingText(profile, "Verified task number prefix (optional)", "task_number_prefix", "task-number-prefix"),
            SettingText(profile, "All-tasks view link / ID (optional)", "task_link_view_id", "task-link-view"),
            Paragraph("The numeric task number comes from Teambition uniqueId. A verified prefix supplies a missing project prefix. The task-opening view changes links only, never the collection scope.")));
    }

    private FrameworkElement BuildMembers(ProfileDraft profile)
    {
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 230 }, "member-list", "Teambition member mappings");
        foreach (string id in profile.Data.Map("owner_names").Select(x => x.Key).Concat(profile.Data.CandidateOwners).Distinct().Order())
            list.Items.Add(new ListViewItem { Tag = id, Content = Paragraph(id + " · " + (profile.Data.Map("owner_names")[id]?.GetValue<string>() ?? "Name needs verification")) });
        list.SelectionChanged += (_, _) =>
        {
            if (list.SelectedItem is not ListViewItem item || item.Tag is not string id) return;
            _state.EditDraft(profile, p => { p.MemberId = id; p.MemberName = p.Data.Map("owner_names")[id]?.GetValue<string>() ?? ""; });
            Render();
        };
        return LocalScroll(Column(
            Paragraph("Discovery yields sampled API IDs, not a complete member directory. Verify each name. Employee numbers cannot replace Teambition API IDs."), list,
            DraftText(profile, "API member ID", "member-id-input", p => p.MemberId, (p, value) => p.MemberId = value),
            DraftText(profile, "Verified member name", "member-name-input", p => p.MemberName, (p, value) => p.MemberName = value),
            Toolbar(
                ActionButton("Add / update member", "member-put", async () =>
                {
                    await _session.CallAsync("member-put", "update_owner_mapping", p =>
                    {
                        var map = (JsonObject)p.Data.Map("owner_names").DeepClone(); map[p.MemberId.Trim()] = p.MemberName;
                        return new JsonObject { ["mappings"] = map };
                    }, (p, result) => p.Data.Set("owner_names", result["mappings"]!.DeepClone()));
                }),
                ActionButton("Remove member mapping", "member-delete", async () =>
                {
                    await _session.CallAsync("member-delete", "update_owner_mapping", p =>
                    {
                        var map = (JsonObject)p.Data.Map("owner_names").DeepClone(); map.Remove(p.MemberId.Trim());
                        return new JsonObject { ["mappings"] = map };
                    }, (p, result) => p.Data.Set("owner_names", result["mappings"]!.DeepClone()));
                })),
            DraftText(profile, "Bulk member import: JSON object or ID <TAB> name per line", "member-import-text", p => p.MemberImport, (p, value) => p.MemberImport = value, multiline: true),
            ActionButton("Import member mappings", "member-import", async () =>
            {
                await _session.CallAsync("member-import", "import_owner_mappings", p => new JsonObject
                { ["source"] = p.MemberImport, ["existing"] = p.Data.Map("owner_names").DeepClone() },
                    (p, result) => p.Data.Set("owner_names", result["mappings"]!.DeepClone()));
            }), Paragraph("Edits are validated as a whole. Conflicts and malformed imports are rejected. Click Save business settings to commit mappings.")));
    }

    private FrameworkElement BuildStatuses(ProfileDraft profile)
    {
        var resolved = Resolved(profile);
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 230 }, "status-list", "Teambition status mappings");
        foreach (string id in profile.Data.Map("status_names").Select(x => x.Key).Concat(profile.Data.CandidateStatuses).Distinct().Order())
            list.Items.Add(new ListViewItem { Tag = id, Content = Paragraph($"{id} · {profile.Data.Map("status_names")[id]?.GetValue<string>() ?? "Name needs verification"} · {(resolved.Contains(id) ? "Resolved" : "Unresolved")}\nSample suggestion: {profile.Data.StatusSuggestions.GetValueOrDefault(id, "None")}") });
        list.SelectionChanged += (_, _) =>
        {
            if (list.SelectedItem is not ListViewItem item || item.Tag is not string id) return;
            _state.EditDraft(profile, p => { p.StatusId = id; p.StatusName = p.Data.Map("status_names")[id]?.GetValue<string>() ?? ""; p.StatusResolved = Resolved(p).Contains(id); });
            Render();
        };
        return LocalScroll(Column(
            Paragraph("Use real project status names. Sample suggestions require review. Explicitly mark which statuses mean resolved."), list,
            DraftText(profile, "API status ID", "status-id-input", p => p.StatusId, (p, value) => p.StatusId = value),
            DraftText(profile, "Verified status name", "status-name-input", p => p.StatusName, (p, value) => p.StatusName = value),
            Check("This status means resolved", "status-resolved", profile.StatusResolved, value => _state.EditDraft(profile, p => p.StatusResolved = value)),
            Toolbar(
                ActionButton("Add / update status", "status-put", async () =>
                {
                    await _session.CallAsync("status-put", "update_status_mapping", p =>
                    {
                        string id = p.StatusId.Trim(); var map = (JsonObject)p.Data.Map("status_names").DeepClone(); map[id] = p.StatusName;
                        var done = Resolved(p); if (p.StatusResolved) done.Add(id); else done.Remove(id);
                        return StatusPayload(map, done);
                    }, ApplyStatuses);
                }),
                ActionButton("Remove status mapping", "status-delete", async () =>
                {
                    await _session.CallAsync("status-delete", "update_status_mapping", p =>
                    {
                        string id = p.StatusId.Trim(); var map = (JsonObject)p.Data.Map("status_names").DeepClone(); map.Remove(id);
                        var done = Resolved(p); done.Remove(id); return StatusPayload(map, done);
                    }, ApplyStatuses);
                })),
            DraftText(profile, "Bulk status JSON object (IDs to verified names)", "status-import-text", p => p.StatusImport, (p, value) => p.StatusImport = value, multiline: true),
            ActionButton("Import status mappings", "status-import", async () =>
            {
                await _session.CallAsync("status-import", "update_status_mapping", p => StatusPayload(ProfileDraft.ParseMap(p.StatusImport),
                    Resolved(p)), ApplyStatuses);
            }), Paragraph("Bulk status import replaces the current names. Resolved IDs must still exist; verify them individually. Save business settings to commit.")));
    }

    private static HashSet<string> Resolved(ProfileDraft profile) => (profile.Data.Settings["resolved_status_ids"] as JsonArray)?
        .Select(x => x!.GetValue<string>()).ToHashSet(StringComparer.Ordinal) ?? new HashSet<string>(StringComparer.Ordinal);
    private static JsonObject StatusPayload(JsonObject map, HashSet<string> resolved) => new()
    { ["mappings"] = map, ["resolved_status_ids"] = new JsonArray(resolved.Order().Select(x => (JsonNode?)JsonValue.Create(x)).ToArray()) };
    private static void ApplyStatuses(ProfileDraft profile, JsonObject result)
    {
        profile.Data.Set("status_names", result["mappings"]!.DeepClone());
        profile.Data.Set("resolved_status_ids", result["resolved_status_ids"]!.DeepClone());
    }

    private FrameworkElement BuildSchedule(ProfileDraft profile)
    {
        var toggle = Identify(new ToggleSwitch { Header = "自动采集、校验、摘要并发送", IsOn = profile.Data.Flag("auto_send_enabled"),
            OnContent = "保存后开启", OffContent = "已关闭" }, "auto-run", "Automatic digest sending");
        toggle.Toggled += (_, _) => _state.Edit(profile, data => data.Set("auto_send_enabled", toggle.IsOn));
        var limit = Identify(new NumberBox { Header = "每批采集任务数（1–700）", Minimum = 1, Maximum = 700,
            Value = profile.Data.Number("max_tasks_per_run", 300), SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact }, "task-limit", "Tasks per collection batch");
        limit.ValueChanged += (_, _) =>
        {
            if (!double.IsNaN(limit.Value)) _state.Edit(profile, data => data.Set("max_tasks_per_run", (int)limit.Value));
        };
        FrameworkElement Slot(string name, string prefix) => Column(Paragraph(name, 18),
            SettingText(profile, "Run at (HH:MM local)", prefix + "_run_at", prefix + "-run-at"),
            SettingText(profile, prefix == "morning" ? "Range starts on previous day (HH:MM)" : "Range starts (HH:MM)", prefix + "_start_at", prefix + "-start-at"),
            SettingText(profile, prefix == "morning" ? "Range ends on current day (HH:MM)" : "Range ends (HH:MM)", prefix + "_end_at", prefix + "-end-at"));
        return LocalScroll(Column(toggle,
            Paragraph("Saving explicitly authorizes the selected automatic schedule only after credentials, scope and mappings validate. Merely opening or binding a profile never turns authorization on. Existing saved authorization may resume after first activation. The computer and Workbench must remain running, signed in, connected and awake."),
            Paragraph("Only existing core recovery rules apply: at most the last 24 hours, during the saved authorization period, with no existing local run record. An uncertain or confirmed delivery is not automatically retried."),
            Slot("Morning digest", "morning"), Slot("Afternoon digest", "afternoon"), limit,
            Paragraph("A batch defaults to 300 tasks; this is not a project limit. A complete run supports up to 10,000 currently readable tasks, 50,000 requests, 1,000,000 returned records, 512 MiB responses, 128 MiB retained data and two hours including checkpoint age. Up to three source verification rounds run within these budgets. Exceeding a budget blocks summary and sending rather than truncating."),
            Paragraph("Completed task/activity pages can be saved as a short-lived DPAPI progress checkpoint. Cancellation retains incomplete progress for up to two hours; resumption re-lists tasks and refreshes changes. This is not a historic snapshot."),
            ActionButton("Clear incomplete collection progress", "clear-collection-progress", async () =>
            { await _session.CallAsync("clear-collection-progress", "clear_collection_progress", _ => null); }),
            Paragraph("Clearing progress retains settings, credentials, history, delivery ledger and automatic-send authorization. Global startup, tray and updates are managed by Workbench.")));
    }
}
