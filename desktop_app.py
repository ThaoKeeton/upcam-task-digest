"""Launch the UPCAM Windows desktop app for setup and unattended digests."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from render_digest import render_digest
from runtime_mappings import parse_config_json
from teambition_readonly import Client, CollectionError
from upcam_app import APP_NAME, APP_VERSION, OPENAI_MODEL, OPENAI_REASONING_EFFORT, RULESET_REVISION
from upcam_app.config import (
    DEFAULT_SETTINGS, SettingsError, SettingsStore, parse_manual_window,
    scheduled_occurrences, scheduled_window, user_data_dir, validate_settings,
)
from upcam_app.history import HistoryError, HistoryStore
from upcam_app.openai_models import (
    ModelCatalogError, OpenAIModelCatalog, cache_matches_key, reasoning_efforts,
)
from openai_summarizer import KNOWN_FAST_MODELS
from upcam_app.secrets import DpapiSecretStore, SECRET_FIELDS, SecretStoreError
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked
from delivery_guard import DeliveryBlocked, initialize_ledger
from report_contract import SHANGHAI


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SAMPLE = ROOT / "examples" / "digest.json"
MAPPING_KEYS = {
    "api_contract_verified", "include_archived", "view", "owner_names",
    "status_names", "resolved_status_ids",
}


class DigestDesktopApp:
    SECRET_LABELS = {
        "teambition_app_id": "Teambition App ID",
        "teambition_app_secret": "Teambition App Secret",
        "dingtalk_webhook": "钉钉机器人 Webhook（含 access_token）",
        "dingtalk_signing_secret": "钉钉机器人签名密钥",
        "openai_api_key": "OpenAI API Key",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} · {APP_VERSION}")
        self.root.geometry("1080x850")
        self.root.minsize(920, 700)
        self.data_dir = user_data_dir()
        self.settings_store = SettingsStore(self.data_dir / "settings.json")
        self.secret_store = DpapiSecretStore(self.data_dir / "credentials.dpapi")
        self.model_catalog = OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.history = HistoryStore(self.data_dir / "history.sqlite3")
        self.history.recover_interrupted_sends()
        self.settings = self.settings_store.load()
        self.saved_secrets = self.secret_store.load()
        self.model_cache = None
        self.ledger_ready, self.ledger_message = self._prepare_ledger()
        self.values: dict[str, tk.Variable] = {}
        self.secret_entries: dict[str, ttk.Entry] = {}
        self._job_lock = threading.Lock()
        self._scheduled_active: set[str] = set()
        self._handled_schedule_slots: set[str] = set()
        self._build()
        self._load_form()
        self._refresh_history()
        self._set_status(self.ledger_message if not self.ledger_ready else
                         "请先完成 Teambition 项目映射，再保存配置并启用自动运行。")
        self.root.after(1000, self._schedule_tick)

    def _prepare_ledger(self):
        path = self.data_dir / "delivery_ledger.sqlite3"
        identity = self.saved_secrets.get("delivery_ledger_id")
        try:
            if not path.exists() and not identity:
                identity = initialize_ledger(path)
                updated = dict(self.saved_secrets, delivery_ledger_id=identity)
                try:
                    self.secret_store.save(updated)
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                self.saved_secrets = updated
            elif not path.exists() or not identity:
                return False, "发送防重账本或本机身份缺失；为避免重复发送，自动发送已停用。"
            guard = DeliveryGuard(path, identity, persistent_storage_verified=True)
            guard.close()
        except (OSError, DeliveryBlocked, SecretStoreError):
            return False, "本机发送防重账本无法验证；自动发送已停用。"
        return True, "本机发送防重账本已验证。"

    def _build(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        top = ttk.Frame(self.root, padding=(20, 16, 20, 12))
        top.pack(fill="x")
        ttk.Label(top, text="UPCAM 站会日报", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(top, text="本机采集与规则校验 · 自动摘要 · 自动发送 · 无逐日报人工审核",
                  foreground="#536174").pack(anchor="w", pady=(4, 0))

        self.status_var = tk.StringVar(value="正在读取本机配置…")
        ttk.Label(self.root, textvariable=self.status_var, padding=(20, 8),
                  background="#fff4d6", foreground="#694d00", wraplength=1000).pack(
                      fill="x", padx=20, pady=(0, 10))

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.settings_tab = ttk.Frame(self.tabs, padding=12)
        self.mapping_tab = ttk.Frame(self.tabs, padding=18)
        self.preview_tab = ttk.Frame(self.tabs, padding=18)
        self.history_tab = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.settings_tab, text="首次配置 / 自动运行")
        self.tabs.add(self.mapping_tab, text="项目映射与范围")
        self.tabs.add(self.preview_tab, text="自选时间 / 运行")
        self.tabs.add(self.history_tab, text="运行记录")
        self._build_settings()
        self._build_mapping()
        self._build_preview()
        self._build_history()

    def _scrollable_frame(self, parent):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, padding=8)
        frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return frame

    def _build_settings(self):
        form = self._scrollable_frame(self.settings_tab)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="项目与应用凭据", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self._text_field(form, 1, 0, "Teambition Tenant / Organization ID", "tenant_id")
        self._text_field(form, 1, 2, "Teambition Project ID", "project_id")
        ttk.Label(form, text="密钥使用 Windows 用户级 DPAPI 加密保存；不会写入日报、运行记录或模板。",
                  foreground="#536174").grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))
        row = 3
        for key in SECRET_FIELDS:
            self._secret_field(form, row, key)
            row += 1

        ttk.Separator(form).grid(row=row, column=0, columnspan=4, sticky="ew", pady=10)
        row += 1
        ttk.Label(form, text="OpenAI 模型", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", pady=(0, 4))
        row += 1
        self.model_status_var = tk.StringVar(value="尚未刷新；默认模型需由当前 API Key 确认可用。")
        self.values["openai_model"] = tk.StringVar(value=OPENAI_MODEL)
        self.openai_model_box = ttk.Combobox(form, textvariable=self.values["openai_model"],
                                             values=(OPENAI_MODEL,), state="readonly", width=28)
        self.openai_model_box.grid(row=row, column=1, sticky="ew", padx=(0, 18), pady=4)
        ttk.Label(form, text="模型").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self.openai_model_box.bind("<<ComboboxSelected>>", self._on_model_changed)
        self.values["openai_reasoning_effort"] = tk.StringVar(value=OPENAI_REASONING_EFFORT)
        ttk.Label(form, text="Reasoning effort").grid(row=row, column=2, sticky="w", padx=(0, 8))
        self.openai_effort_box = ttk.Combobox(
            form, textvariable=self.values["openai_reasoning_effort"],
            values=("default", "none", "low", "medium", "high", "xhigh", "max"),
            state="readonly", width=18)
        self.openai_effort_box.grid(row=row, column=3, sticky="ew", pady=4)
        row += 1
        ttk.Button(form, text="刷新当前 API Key 可用模型", command=self._refresh_models).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Label(form, textvariable=self.model_status_var, foreground="#536174", wraplength=700).grid(
            row=row, column=2, columnspan=2, sticky="w", pady=(2, 0))
        row += 1
        ttk.Label(form, text="模型列表只返回基础元数据；reasoning 档位来自本程序核验的模型文档。模型在列表中不代表 Responses 端点权限已验证。",
                  foreground="#8a4b00", wraplength=900).grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 2))
        row += 1
        ttk.Label(form, text="API 服务等级").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.values["openai_service_tier"] = tk.StringVar(value="default")
        self.openai_service_tier_box = ttk.Combobox(
            form, textvariable=self.values["openai_service_tier"],
            values=("default", "fast"), state="readonly", width=20)
        self.openai_service_tier_box.grid(row=row, column=1, sticky="ew", padx=(0, 18), pady=4)
        ttk.Label(form, text="default = 标准费用；Fast 需模型支持并会产生更高费用。",
                  foreground="#536174", wraplength=520).grid(row=row, column=2, columnspan=2,
                                                              sticky="w", pady=4)
        row += 1
        ttk.Label(form, text="Fast 由 Responses API 的 service_tier=fast 请求；速度提升不保证，账户/地区/项目权限可能拒绝。默认不启用。",
                  foreground="#8a4b00", wraplength=900).grid(row=row, column=0, columnspan=4, sticky="w", pady=(2, 4))
        row += 1

        ttk.Separator(form).grid(row=row, column=0, columnspan=4, sticky="ew", pady=10)
        row += 1
        ttk.Label(form, text="自动运行时间与采集窗口（电脑本机时间）",
                  font=("Microsoft YaHei UI", 12, "bold")).grid(
                      row=row, column=0, columnspan=4, sticky="w", pady=(0, 6))
        row += 1
        self._text_field(form, row, 0, "上午运行时间", "morning_run_at", width=12)
        self._text_field(form, row, 2, "上午窗口起点（前一日）", "morning_start_at", width=12)
        row += 1
        self._text_field(form, row, 0, "上午窗口终点", "morning_end_at", width=12)
        self._text_field(form, row, 2, "下午运行时间", "afternoon_run_at", width=12)
        row += 1
        self._text_field(form, row, 0, "下午窗口起点", "afternoon_start_at", width=12)
        self._text_field(form, row, 2, "下午窗口终点", "afternoon_end_at", width=12)
        row += 1
        ttk.Label(form, text="默认上午：前一日 16:50 到当日 08:50；默认下午：当日 00:00 到 16:50。",
                  foreground="#536174").grid(row=row, column=0, columnspan=4, sticky="w", pady=(5, 0))
        row += 1
        self.values["auto_send_enabled"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="应用运行期间按以上时间自动采集、校验、摘要并发送到钉钉",
                        variable=self.values["auto_send_enabled"]).grid(
                            row=row, column=0, columnspan=4, sticky="w", pady=(8, 2))
        row += 1
        ttk.Label(form, text="自动执行要求此应用保持打开且 Windows 未休眠；关闭应用期间错过的时间点不会补发。",
                  foreground="#8a4b00").grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 4))
        row += 1
        ttk.Separator(form).grid(row=row, column=0, columnspan=4, sticky="ew", pady=10)
        row += 1
        ttk.Label(form, text="仓库规则版本：upcam-task-digest @ " + RULESET_REVISION[:12] +
                  "；日报模板随本程序版本发布并自动读取。",
                  foreground="#536174", wraplength=900).grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1
        actions = ttk.Frame(form)
        actions.grid(row=row, column=0, columnspan=4, sticky="w", pady=(12, 4))
        ttk.Button(actions, text="保存本机配置", command=self._save).pack(side="left")
        ttk.Button(actions, text="只读测试 Teambition 连接", command=self._test_teambition).pack(side="left", padx=8)
        ttk.Button(actions, text="清除已保存服务密钥…", command=self._clear_secrets).pack(side="left")

    def _build_mapping(self):
        tab = self.mapping_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(tab, text="Teambition 项目映射与采集范围",
                  font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(tab, text="日报格式和筛选规则由仓库模板维护。这里仅保存你自己的项目状态、成员 ID 映射和项目视图口径。完成核验后再勾选 api_contract_verified 与 view.verified。",
                  foreground="#536174", wraplength=900).grid(row=1, column=0, sticky="w", pady=(6, 8))
        frame = ttk.Frame(tab)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.mapping_editor = tk.Text(frame, wrap="none", font=("Consolas", 10), height=26)
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.mapping_editor.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.mapping_editor.xview)
        self.mapping_editor.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.mapping_editor.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        ttk.Label(tab, text="字段示例：owner_names/status_names 是“Teambition ID: 显示名”；resolved_status_ids 列出哪些状态代表已解决。未匹配或未知事件会自动阻断发送。",
                  foreground="#8a4b00", wraplength=900).grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _build_preview(self):
        tab = self.preview_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)
        ttk.Label(tab, text="自选采集起止时间", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(tab, text="使用本机时间 YYYY-MM-DD HH:MM。自动日报按仓库模板生成；发送前由程序校验来源、窗口、映射、未知事件和模型输出。",
                  foreground="#536174", wraplength=920).grid(row=1, column=0, sticky="w", pady=(4, 10))
        controls = ttk.Frame(tab)
        controls.grid(row=2, column=0, sticky="ew")
        now = datetime.now().replace(second=0, microsecond=0)
        self.manual_start = tk.StringVar(value=now.replace(hour=0, minute=0).strftime("%Y-%m-%d %H:%M"))
        self.manual_end = tk.StringVar(value=now.strftime("%Y-%m-%d %H:%M"))
        ttk.Label(controls, text="开始").pack(side="left")
        ttk.Entry(controls, textvariable=self.manual_start, width=18).pack(side="left", padx=(6, 16))
        ttk.Label(controls, text="结束").pack(side="left")
        ttk.Entry(controls, textvariable=self.manual_end, width=18).pack(side="left", padx=(6, 16))
        ttk.Button(controls, text="运行并自动发送", command=self._run_manual).pack(side="left")
        ttk.Button(controls, text="预览虚构样例", command=self._preview_sample).pack(side="left", padx=8)
        ttk.Label(tab, text="立即运行同样无需人工审核；如果自动校验失败、AI 拒绝或发送结果不明，程序会停止且不会自动重试。",
                  foreground="#8a4b00", wraplength=920).grid(row=3, column=0, sticky="w", pady=(10, 10))
        frame = ttk.Frame(tab)
        frame.grid(row=4, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.preview = tk.Text(frame, wrap="word", font=("Consolas", 10), state="disabled", height=24)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        self.preview.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_history(self):
        tab = self.history_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="运行与发送状态", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        columns = ("created", "period", "source", "state")
        self.history_view = ttk.Treeview(tab, columns=columns, show="headings", height=20)
        labels = {"created": "创建时间（UTC）", "period": "采集窗口（本机时间）",
                  "source": "来源", "state": "状态"}
        widths = {"created": 170, "period": 300, "source": 100, "state": 150}
        for column in columns:
            self.history_view.heading(column, text=labels[column])
            self.history_view.column(column, width=widths[column], anchor="w")
        self.history_view.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.history_view.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.history_view.configure(yscrollcommand=scrollbar.set)
        ttk.Label(tab, text="发送结果不明或机器人拒绝时，该运行点会锁定，不自动重发；先处理账本状态再恢复。",
                  foreground="#536174", wraplength=900).grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _text_field(self, parent, row, label_column, label, key, width=28):
        value = tk.StringVar()
        self.values[key] = value
        ttk.Label(parent, text=label).grid(row=row, column=label_column, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=value, width=width)
        entry.grid(row=row, column=label_column + 1, sticky="ew", padx=(0, 18), pady=4)

    def _secret_field(self, parent, row, key):
        ttk.Label(parent, text=self.SECRET_LABELS[key]).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        value = tk.StringVar()
        self.values[key] = value
        entry = ttk.Entry(parent, textvariable=value, show="●")
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=4)
        state = ttk.Label(parent, text="本机未保存")
        state.grid(row=row, column=3, sticky="w", pady=4)
        self.secret_entries[key] = entry
        setattr(self, f"_secret_state_{key}", state)

    def _mapping_from_settings(self, settings):
        return {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in sorted(MAPPING_KEYS)}

    def _load_form(self):
        for key, variable in self.values.items():
            if key in self.settings:
                variable.set(self.settings[key])
        self.mapping_editor.delete("1.0", "end")
        self.mapping_editor.insert("1.0", json.dumps(self._mapping_from_settings(self.settings),
                                                      ensure_ascii=False, indent=2))
        self._load_model_cache()
        self._configure_effort_options()
        for key in SECRET_FIELDS:
            getattr(self, f"_secret_state_{key}").configure(
                text="本机已加密保存" if self.saved_secrets.get(key) else "本机未保存")

    def _mapping_document(self):
        source = self.mapping_editor.get("1.0", "end-1c")
        mapping = parse_config_json(source)
        if set(mapping) != MAPPING_KEYS:
            raise SettingsError("项目映射 JSON 字段不完整或包含未知字段")
        return mapping

    def _validate_auto_ready(self, settings, secrets):
        if not self.ledger_ready or not secrets.get("delivery_ledger_id"):
            raise SettingsError("本机防重账本未通过验证")
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise SettingsError("请填写 Teambition Tenant ID 和 Project ID")
        config = validate_settings(settings)
        if (config.get("api_contract_verified") is not True or not config["view"].get("verified")
                or not config["view"].get("evidence")):
            raise SettingsError("请先在项目映射页核验 API 字段和项目视图范围")
        required = ("teambition_app_id", "teambition_app_secret", "dingtalk_webhook",
                    "dingtalk_signing_secret", "openai_api_key")
        if any(not secrets.get(key) for key in required):
            raise SettingsError("请先填写并保存 Teambition、钉钉和 OpenAI 密钥")
        try:
            cache = self.model_catalog.load()
        except ModelCatalogError:
            raise SettingsError("请用当前保存的 OpenAI API Key 刷新模型列表") from None
        if not cache_matches_key(cache, secrets["openai_api_key"]):
            raise SettingsError("模型缓存不是由当前 API Key 刷新，请重新刷新模型")
        model = settings["openai_model"]
        if model not in {item["id"] for item in cache["models"]}:
            raise SettingsError("所选模型不在当前 API Key 的缓存列表中")
        if settings["openai_reasoning_effort"] not in ("default",) + reasoning_efforts(model):
            raise SettingsError("此模型没有已核验的所选 reasoning effort")
        if settings["openai_service_tier"] == "fast" and model not in KNOWN_FAST_MODELS:
            raise SettingsError("OpenAI 官方资料未核验此模型支持 Fast；请选标准费用或已确认支持的模型")
        from upcam_app.dingtalk import signed_webhook
        try:
            signed_webhook(secrets["dingtalk_webhook"], secrets["dingtalk_signing_secret"], 1)
        except (ValueError, TypeError):
            raise SettingsError("钉钉机器人 Webhook 或签名密钥无效") from None

    def _save(self):
        proposed = dict(self.settings)
        for key in DEFAULT_SETTINGS:
            if key in self.values:
                proposed[key] = self.values[key].get()
        try:
            proposed.update(self._mapping_document())
            proposed["schema_version"] = 1
            clean = validate_settings(proposed)
        except (SettingsError, ValueError, TypeError) as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return
        entered = {key: self.values[key].get() for key in SECRET_FIELDS if self.values[key].get()}
        try:
            merged = self.secret_store.load()
            merged.update(entered)
            if clean["auto_send_enabled"]:
                self._validate_auto_ready(clean, merged)
            self.secret_store.save(merged)
            self.settings_store.save(clean)
        except (SettingsError, SecretStoreError, OSError) as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return
        self.settings, self.saved_secrets = clean, merged
        for key in SECRET_FIELDS:
            self.values[key].set("")
            getattr(self, f"_secret_state_{key}").configure(
                text="本机已加密保存" if self.saved_secrets.get(key) else "本机未保存")
        self._load_model_cache()
        self._set_status("本机设置已保存。" + ("自动执行已启用。" if clean["auto_send_enabled"] else "自动执行当前关闭。"))
        messagebox.showinfo("保存完成", "设置已保存在本机。服务密钥使用 Windows DPAPI 加密。", parent=self.root)

    def _clear_secrets(self):
        if not messagebox.askyesno("清除密钥", "清除本机保存的 Teambition、钉钉与 OpenAI 密钥？防重复账本身份会保留。",
                                   parent=self.root):
            return
        self.settings["auto_send_enabled"] = False
        self.values["auto_send_enabled"].set(False)
        self.settings_store.save(self.settings)
        internal = {key: value for key, value in self.saved_secrets.items() if key == "delivery_ledger_id"}
        try:
            self.secret_store.save(internal)
        except SecretStoreError as exc:
            messagebox.showerror("无法清除密钥", str(exc), parent=self.root)
            return
        self.saved_secrets = internal
        for key in SECRET_FIELDS:
            self.values[key].set("")
            getattr(self, f"_secret_state_{key}").configure(text="本机未保存")
        self._set_status("已清除服务密钥；本机防重账本身份保留。")

    def _load_model_cache(self):
        try:
            cache = self.model_catalog.load()
        except ModelCatalogError:
            self.model_cache = None
            self.openai_model_box.configure(values=(OPENAI_MODEL,))
            self.model_status_var.set("本机模型缓存无法读取；刷新成功后才会替换缓存。")
            return
        api_key = self.saved_secrets.get("openai_api_key", "")
        if not cache:
            self.model_cache = None
            self.openai_model_box.configure(values=(OPENAI_MODEL,))
            self.model_status_var.set("尚无模型缓存；点击刷新当前 API Key 可用模型。")
            return
        if not cache_matches_key(cache, api_key):
            self.model_cache = None
            self.openai_model_box.configure(values=(OPENAI_MODEL,))
            self.model_status_var.set("本机缓存属于另一个 API Key；保存当前密钥后再刷新。")
            return
        self.model_cache = cache
        ids = [item["id"] for item in cache["models"]]
        self.openai_model_box.configure(values=tuple(ids))
        self.model_status_var.set(f"已载入当前 Key 的本机缓存（{cache['fetched_at_utc']}）；只有点击刷新才重新请求。")
        self._show_model_availability(ids)

    def _refresh_models(self):
        if getattr(self, "_model_refreshing", False):
            return
        api_key = self.values["openai_api_key"].get() or self.saved_secrets.get("openai_api_key", "")
        if not api_key:
            messagebox.showinfo("需要 API Key", "先输入或保存 OpenAI API Key，再刷新模型。", parent=self.root)
            return
        self._model_refreshing = True
        self.model_status_var.set("正在请求 OpenAI API；成功后才替换本机模型缓存…")

        def worker():
            try:
                cache = self.model_catalog.refresh(api_key)
                error = None
            except ModelCatalogError as exc:
                cache, error = None, str(exc)
            self.root.after(0, lambda: self._models_refreshed(cache, error))

        threading.Thread(target=worker, daemon=True, name="openai-model-refresh").start()

    def _models_refreshed(self, cache, error):
        self._model_refreshing = False
        if error:
            self.model_status_var.set("刷新失败；原缓存保留，没有自动切换模型。")
            messagebox.showerror("模型刷新失败", error + "\n\n原模型缓存保持不变。", parent=self.root)
            self._load_model_cache()
            return
        current_key = self.values["openai_api_key"].get() or self.saved_secrets.get("openai_api_key", "")
        if not cache_matches_key(cache, current_key):
            self.model_status_var.set("返回的模型缓存与当前 API Key 不匹配；请重试。")
            return
        self.model_cache = cache
        ids = [item["id"] for item in cache["models"]]
        self.openai_model_box.configure(values=tuple(ids))
        self._configure_effort_options()
        self.model_status_var.set(f"刷新完成：当前 Key 返回 {len(ids)} 个 API 模型；缓存于 {cache['fetched_at_utc']}。")
        if self.values["openai_model"].get() not in ids:
            self.model_status_var.set("刷新完成，但当前模型不在列表中；选择未自动改变。")

    def _show_model_availability(self, ids):
        model = self.values["openai_model"].get()
        if ids and model not in ids:
            self.model_status_var.set(f"当前选择 {model} 未出现在当前 Key 的 API 列表中；不会自动切换。")

    def _on_model_changed(self, _event=None):
        self._configure_effort_options()

    def _configure_effort_options(self):
        model = self.values["openai_model"].get()
        options = ("default",) + reasoning_efforts(model)
        self.openai_effort_box.configure(values=options)
        current = self.values["openai_reasoning_effort"].get()
        if current not in options:
            self.values["openai_reasoning_effort"].set("default")

    def _test_teambition(self):
        settings = dict(self.settings)
        for key in ("tenant_id", "project_id"):
            settings[key] = self.values[key].get().strip()
        secrets = dict(self.saved_secrets)
        secrets.update({key: self.values[key].get() for key in
                        ("teambition_app_id", "teambition_app_secret") if self.values[key].get()})
        if not settings["tenant_id"] or not settings["project_id"] or any(
                not secrets.get(key) for key in ("teambition_app_id", "teambition_app_secret")):
            messagebox.showinfo("需要配置", "填写 Tenant ID、Project ID、App ID 和 App Secret 后再测试。",
                                parent=self.root)
            return
        self._set_status("正在执行 Teambition 只读项目查询；不会请求 OpenAI 或发送钉钉。")

        def worker():
            try:
                now = datetime.now().astimezone(SHANGHAI)
                start, end = now - timedelta(minutes=5), now
                client = Client(settings["tenant_id"], credentials=lambda: (
                    secrets["teambition_app_id"], secrets["teambition_app_secret"]))
                collection = client.collect(settings["project_id"], day=end.date().isoformat(),
                                            window=(start, end), include_archived=settings["include_archived"])
                result, error = f"Teambition 只读查询成功；收到 {len(collection['records'])} 条任务记录。", None
            except (CollectionError, OSError):
                result, error = None, "Teambition 连接测试失败；请检查本机网络、凭据和项目权限。"
            except Exception:
                result, error = None, "Teambition 连接测试未完成；详细响应未写入日志。"
            self.root.after(0, lambda: self._connection_test_finished(result, error))

        threading.Thread(target=worker, daemon=True, name="teambition-connection-test").start()

    def _connection_test_finished(self, result, error):
        self._set_status(error or result)
        if error:
            messagebox.showerror("Teambition 测试失败", error, parent=self.root)
        else:
            messagebox.showinfo("Teambition 测试成功", result, parent=self.root)

    def _run_manual(self):
        try:
            start, end = parse_manual_window(self.manual_start.get(), self.manual_end.get())
        except SettingsError as exc:
            messagebox.showerror("时间窗口无效", str(exc), parent=self.root)
            return
        self._launch_run(start, end, schedule_key=None, scheduled=False)

    def _launch_run(self, start, end, *, schedule_key, scheduled):
        if not self.ledger_ready:
            self._set_status(self.ledger_message)
            return
        if not self._job_lock.acquire(blocking=False):
            self._set_status("已有日报任务正在运行；当前触发不会并行执行。")
            return
        self._set_status("正在本地采集并校验；通过后只发送必要评论摘要到 OpenAI，再自动发送钉钉。")

        def worker():
            result = None
            error = None
            try:
                workflow = DigestWorkflow(self.settings, self.saved_secrets, self.data_dir,
                                          history=self.history, model_catalog=self.model_catalog)
                result = workflow.run(start, end, schedule_key=schedule_key)
            except WorkflowBlocked as exc:
                error = str(exc)
            except Exception:
                error = "自动日报运行失败；详细响应和密钥未写入日志。"
            finally:
                self._job_lock.release()
            try:
                self.root.after(0, lambda: self._run_finished(result, error, scheduled))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="upcam-digest-run").start()

    def _run_finished(self, result, error, scheduled):
        if result:
            self._show_preview(result["report"]["markdown"])
            self._set_status(f"已自动发送并收到钉钉确认。运行记录：{result['run_id']}")
        else:
            self._set_status(error or "自动日报未完成；没有重试。")
            if not scheduled:
                messagebox.showerror("日报未发送", error or "自动日报未完成。", parent=self.root)
        self._refresh_history()

    def _schedule_tick(self):
        try:
            if self.settings.get("auto_send_enabled"):
                now = datetime.now().replace(microsecond=0)
                slots = scheduled_occurrences(self.settings, now - timedelta(seconds=90), now)
                for slot, kind, key in slots:
                    if key in self._handled_schedule_slots or key in self._scheduled_active:
                        continue
                    start, end = scheduled_window(self.settings, kind, slot.date())
                    if self._launch_scheduled(start, end, key):
                        self._handled_schedule_slots.add(key)
        except (SettingsError, OSError):
            pass
        self.root.after(15000, self._schedule_tick)

    def _launch_scheduled(self, start, end, schedule_key):
        if not self.ledger_ready or not self._job_lock.acquire(blocking=False):
            return False
        self._scheduled_active.add(schedule_key)
        self._set_status(f"自动运行中：{schedule_key}。")

        def worker():
            result = None
            error = None
            try:
                workflow = DigestWorkflow(self.settings, self.saved_secrets, self.data_dir,
                                          history=self.history, model_catalog=self.model_catalog)
                result = workflow.run(start, end, schedule_key=schedule_key)
            except WorkflowBlocked as exc:
                error = str(exc)
            except Exception:
                error = "自动运行已停止；详细响应和密钥未写入日志。"
            finally:
                self._job_lock.release()
            try:
                self.root.after(0, lambda: self._scheduled_finished(schedule_key, result, error))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="upcam-scheduled-run").start()
        return True

    def _scheduled_finished(self, schedule_key, result, error):
        self._scheduled_active.discard(schedule_key)
        if result:
            self._set_status(f"自动日报已发送并确认：{schedule_key}。")
        else:
            self._set_status(f"自动日报已阻断：{schedule_key}。{error or ''}")
        self._refresh_history()

    def _preview_sample(self):
        try:
            _, end = parse_manual_window(self.manual_start.get(), self.manual_end.get())
            data = json.loads(SAMPLE.read_text(encoding="utf-8"))
            data["date"] = end.date().isoformat()
            output = render_digest(data)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror("无法生成样例", str(exc), parent=self.root)
            return
        self._show_preview(output)
        self._set_status("已显示仓库格式的虚构样例；样例没有采集、调用模型或发送。")

    def _show_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _refresh_history(self):
        for item in self.history_view.get_children():
            self.history_view.delete(item)
        for run in self.history.list_runs():
            period = f"{run['window_start_local']} → {run['window_end_local']}"
            state = {"prepared": "等待发送", "sending": "发送中", "sent": "已确认",
                     "blocked": "校验阻断", "delivery_unknown": "结果不明", "preview": "样例"}.get(
                         run["state"], run["state"])
            self.history_view.insert("", "end", values=(run["created_at_utc"], period,
                                                            run["source"], state))

    def _set_status(self, message):
        self.status_var.set(message)


def main():
    root = tk.Tk()
    DigestDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
