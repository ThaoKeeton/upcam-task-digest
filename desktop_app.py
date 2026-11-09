"""Launch the UPCAM Windows desktop app for setup and unattended digests."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from automated_digest import collection_validation_message, diagnose_automatic_source
from upcam_app.diagnostic_export import safe_structure_export
from render_digest import render_digest
from runtime_mappings import validate_mappings, merge_discovered
from teambition_readonly import CollectionError
from upcam_app import APP_NAME, APP_VERSION, OPENAI_MODEL, OPENAI_REASONING_EFFORT, RULESET_REVISION
from upcam_app.config import (
    DEFAULT_SETTINGS, SCHEDULE_RECOVERY_WINDOW, SettingsError, SettingsStore, parse_manual_window,
    recoverable_schedule_occurrences, runtime_config, scheduled_window, user_data_dir, validate_settings,
)
from upcam_app.collection_runtime import make_collection_client, list_mcp_projects
from upcam_app.history import HistoryError, HistoryStore
from upcam_app.project_setup import (
    ProjectSetupError, analyze_project_probe, merge_owner_mappings, parse_owner_mapping_tsv,
)
from upcam_app.windows_integration import (
    StartupIntegrationError, WindowsSingleInstance, configure_user_startup,
    parse_launch_options,
)
from upcam_app.openai_models import (
    ModelCatalogError, OpenAIModelCatalog, cache_matches_key, reasoning_efforts,
)
from openai_summarizer import KNOWN_FAST_MODELS
from upcam_app.secrets import DpapiSecretStore, SECRET_FIELDS, SecretStoreError, revoke_missing_mcp_authorization, teambition_mcp_identity, mcp_connection_authorized, MCP_AUTHORIZATION_FIELD
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked
from upcam_app.dingtalk_mentions import MentionConfigError, reject_credential_mentions
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from report_contract import SHANGHAI, updated_selection


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SAMPLE = ROOT / "examples" / "digest.json"
VIEW_FILTERS = {
    "执行人 ID": "executorId",
    "任务列表 ID": "tasklistId",
    "阶段 ID": "stageId",
    "状态 ID": "tfsId",
    "自定义字段配置 ID": "scenariofieldconfigId",
    "可见性": "visible",
    "父任务 ID": "parentTaskId",
}
SMOKE_TEST_TABS = (
    "账户与模型",
    "项目范围",
    "高级：成员覆盖",
    "高级：状态覆盖",
    "自动运行",
    "运行与诊断",
    "运行记录",
)


def verify_desktop_smoke(app, sample_path=SAMPLE, *, assert_hidden=False,
                         exercise_controls=False, screenshot_path=None) -> bool:
    """Check real app pages, controls, and the packaged report template offline."""
    try:
        labels = tuple(app.tabs.tab(index, "text") for index in range(app.tabs.index("end")))
    except (AttributeError, tk.TclError, TypeError) as exc:
        raise RuntimeError("The desktop window did not initialize its tabs") from exc
    if labels != SMOKE_TEST_TABS:
        raise RuntimeError("The desktop window is missing a required application tab")
    if not all(hasattr(app, name) for name in (
            "navigation", "project_probe_button", "owner_tree", "status_tree", "startup_check",
            "schedule_tab", "owner_tab", "status_tab", "owner_bulk_text", "owner_import_button",
            "source_diagnostic_button", "diagnostic_export_button")):
        raise RuntimeError("The desktop window is missing project setup or Windows startup controls")
    if hasattr(app, "mapping_canvas"):
        raise RuntimeError("Full-page scrolling must not be part of the application navigation")
    if hasattr(app, "mapping_editor"):
        raise RuntimeError("The project setup UI must not require editing mapping JSON")
    if not Path(sample_path).is_file():
        raise RuntimeError("The packaged report template is missing")
    if os.name == "nt" and (getattr(app, "_tray_icon", None) is None
                             or not bool(getattr(app._tray_icon, "visible", False))
                             or getattr(app._tray_icon, "menu", None) is None):
        raise RuntimeError("The Windows system tray icon or its menu did not initialize")
    if assert_hidden and (getattr(app, "_tray_icon", None) is None
                          or not bool(getattr(app._tray_icon, "visible", False))
                          or app.root.state() != "withdrawn"):
        raise RuntimeError("The application did not start hidden with a working tray icon")
    if exercise_controls:
        _exercise_mapping_controls(app, assert_hidden=assert_hidden)
    if screenshot_path:
        app.tabs.select(app.settings_tab)
        app.root.deiconify()
        app.root.lift()
        app.root.update_idletasks()
        app.root.update()
        try:
            from PIL import ImageGrab
            x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
            width, height = app.root.winfo_width(), app.root.winfo_height()
            if width < 850 or height < 580:
                raise RuntimeError("The desktop window is smaller than the UI screenshot acceptance size")
            capture_path = Path(screenshot_path)
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(capture_path)
        except Exception as exc:
            raise RuntimeError("The Windows UI screenshot could not be captured") from exc
    return True


def _new_teambition_client(settings, secrets):
    return make_collection_client(settings, secrets, user_data_dir())


def _exercise_mapping_controls(app, *, assert_hidden=False):
    """Exercise the real Windows mapping widgets using synthetic values only."""
    root, button = app.root, app.owner_import_button

    def inside_window(widget):
        root.update_idletasks()
        left, top = root.winfo_rootx(), root.winfo_rooty()
        right, bottom = left + root.winfo_width(), top + root.winfo_height()
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        return (widget.winfo_ismapped() and x >= left and y >= top
                and x + widget.winfo_width() <= right and y + widget.winfo_height() <= bottom)

    original_owner_map = dict(app.owner_map)
    original_candidates = set(app.owner_candidate_ids)
    original_filters = dict(app.view_filters)
    try:
        app.tabs.select(app.owner_tab)
        root.deiconify()
        root.update_idletasks()
        for label, nav_button in zip(SMOKE_TEST_TABS, app.navigation.buttons):
            if not inside_window(nav_button):
                raise RuntimeError(f"The {label} navigation button is outside the visible window")
        if not inside_window(button):
            raise RuntimeError("The member import button is not visible on its dedicated page")

        synthetic = '{"smoke-member-01":"虚构成员甲","smoke-member-02":"虚构成员乙"}'
        app.owner_bulk_text.delete("1.0", "end")
        app.owner_bulk_text.insert("1.0", synthetic)
        button.invoke()
        root.update_idletasks()
        if any(app.owner_map.get(key) != value for key, value in (
                ("smoke-member-01", "虚构成员甲"), ("smoke-member-02", "虚构成员乙"))):
            raise RuntimeError("Pasting JSON did not import the synthetic member mappings")
        for member_id, name in (("smoke-member-01", "虚构成员甲"),
                                ("smoke-member-02", "虚构成员乙")):
            if member_id not in app.owner_tree.get_children():
                raise RuntimeError("An imported member mapping is missing from the visible table")
            if app.owner_tree.item(member_id, "values")[1] != name:
                raise RuntimeError("The member table does not show the pasted synthetic name")

        app.view_filter_label.set("执行人 ID")
        app.view_filter_value.set("smoke-executor-01")
        app._add_view_filter()
        if app.view_filters.get("executorId") != "smoke-executor-01":
            raise RuntimeError("Adding an optional project filter did not update the project scope")

        app.tabs.select(app.mapping_tab)
        root.update_idletasks()
        if not inside_window(app.project_probe_button):
            raise RuntimeError("The project verification button is not visible on the project page")

        app.tabs.select(app.schedule_tab)
        root.update_idletasks()
        if not inside_window(app.startup_check):
            raise RuntimeError("The Windows startup control is not visible on the schedule page")

        app.tabs.select(app.preview_tab)
        root.update_idletasks()
        if (not inside_window(app.source_diagnostic_button)
                or not inside_window(app.diagnostic_export_button)):
            raise RuntimeError("The read-only diagnostic button is not visible on the run tab")
        app.tabs.select(app.history_tab)
        root.update_idletasks()
        if "note" not in app.history_view.cget("columns"):
            raise RuntimeError("The run history does not expose local validation explanations")
    finally:
        app.owner_map = original_owner_map
        app.owner_candidate_ids = original_candidates
        app.view_filters = original_filters
        app._render_setup_tables()
        if assert_hidden:
            root.withdraw()
            root.update_idletasks()


class SideNavigation:
    """Compact sidebar navigation made from the app's real Tk/ttk controls."""

    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(self.frame, width=218, bg="#15324b", highlightthickness=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = ttk.Frame(self.frame, padding=(22, 18, 22, 18))
        self.content.pack(side="left", fill="both", expand=True)
        tk.Label(self.sidebar, text="UPCAM", bg="#15324b", fg="#ffffff",
                 font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", padx=20, pady=(24, 0))
        tk.Label(self.sidebar, text="Teambition 工作助手", bg="#15324b", fg="#b8c8d8",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=21, pady=(0, 18))
        self.nav_items = tk.Frame(self.sidebar, bg="#15324b")
        self.nav_items.pack(fill="x", padx=10)
        tk.Label(self.sidebar, text=APP_VERSION, bg="#15324b", fg="#91a6ba",
                 font=("Microsoft YaHei UI", 8)).pack(side="bottom", anchor="w", padx=20, pady=16)
        self.pages = []
        self.labels = []
        self.buttons = []
        self.current = None

    def add(self, label):
        page = ttk.Frame(self.content)
        self.labels.append(label)
        self.pages.append(page)
        button = tk.Button(
            self.nav_items, text=label, anchor="w", relief="flat", borderwidth=0,
            padx=12, pady=9, cursor="hand2", font=("Microsoft YaHei UI", 10),
            command=lambda selected=page: self.select(selected))
        button.pack(fill="x", pady=2)
        self.buttons.append(button)
        if self.current is None:
            self.select(page)
        return page

    def index(self, _index):
        return len(self.pages)

    def tab(self, index, option):
        if option != "text":
            raise ValueError("Only the page label is available")
        return self.labels[index]

    def select(self, page_or_index):
        if isinstance(page_or_index, int):
            page = self.pages[page_or_index]
        else:
            page = page_or_index
        if page not in self.pages:
            return
        if self.current is not None:
            self.current.pack_forget()
        page.pack(fill="both", expand=True)
        self.current = page
        for button, candidate in zip(self.buttons, self.pages):
            selected = candidate is page
            button.configure(
                bg="#285273" if selected else "#15324b",
                fg="#ffffff" if selected else "#d7e2ec",
                activebackground="#32678e", activeforeground="#ffffff")


class DigestDesktopApp:
    SECRET_LABELS = {
        "teambition_mcp_token": "Teambition MCP 个人 Token",
        "dingtalk_webhook": "钉钉机器人 Webhook（含 access_token）",
        "dingtalk_signing_secret": "钉钉机器人签名密钥",
        "openai_api_key": "OpenAI API Key",
    }

    def __init__(self, root: tk.Tk, *, start_hidden=False, tray_enabled=True):
        self.root = root
        self.root.title(f"UPCAM · Teambition 工作助手 · {APP_VERSION}")
        self.root.geometry("1080x850")
        self.root.minsize(920, 700)
        self.data_dir = user_data_dir()
        self.settings_store = SettingsStore(self.data_dir / "settings.json")
        self.secret_store = DpapiSecretStore(self.data_dir / "credentials.dpapi")
        self.model_catalog = OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.history = HistoryStore(self.data_dir / "history.sqlite3")
        self.last_diagnostic_shape = None
        self.history.recover_interrupted_sends()
        self.settings = self.settings_store.load()
        self.saved_secrets = self.secret_store.load()
        migrated = revoke_missing_mcp_authorization(self.settings, self.saved_secrets)
        if migrated != self.settings:
            self.settings_store.save(migrated)
        self.settings = migrated
        configured_now = datetime.now().replace(microsecond=0)
        enabled_since = self.settings.get("auto_send_enabled_since_local", "")
        if self.settings.get("auto_send_enabled"):
            self._auto_run_enabled_since = (datetime.fromisoformat(enabled_since) if enabled_since
                                            else configured_now - SCHEDULE_RECOVERY_WINDOW)
            if not enabled_since:
                # Existing settings predate recovery tracking; grant only the bounded recovery window.
                self.settings["auto_send_enabled_since_local"] = self._auto_run_enabled_since.isoformat(
                    timespec="seconds")
        else:
            self._auto_run_enabled_since = configured_now
        self.model_cache = None
        self.ledger_ready, self.ledger_message = self._prepare_ledger()
        self.values: dict[str, tk.Variable] = {}
        self.secret_entries: dict[str, ttk.Entry] = {}
        self._job_lock = threading.Lock()
        self._scheduled_active: set[str] = set()
        self._handled_schedule_slots: set[str] = set()
        self._tray_icon = None
        self._build()
        self._load_form()
        self._refresh_history()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self._set_status(self.ledger_message if not self.ledger_ready else
                         "请保存 MCP 个人 Token，选择项目和时间；成员姓名与任务状态自动读取。")
        if tray_enabled and os.name == "nt":
            self._start_tray()
        if start_hidden and self._tray_icon is not None:
            self.root.after_idle(self.root.withdraw)
            self._check_background_tray_ready(0)
        elif start_hidden:
            self._set_status("托盘不可用，已显示主窗口以避免隐藏后无法退出。")
        self.root.after(1000, self._schedule_tick)

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new("RGB", (64, 64), "#163b5c")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill="#2f80b7")
            draw.line((19, 34, 28, 43, 46, 21), fill="white", width=6)
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", lambda _icon, _item: self.root.after(0, self._show_window),
                                 default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出应用", lambda _icon, _item: self.root.after(0, self._quit_application)),
            )
            self._tray_icon = pystray.Icon(APP_NAME, image, "UPCAM · Teambition 工作助手", menu)
            self._tray_icon.run_detached()
        except Exception:
            self._tray_icon = None
            self._set_status("系统托盘初始化失败；关闭窗口会退出应用，请重新启动程序检查安装。")

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _tray_ready(self):
        return self._tray_icon is not None and bool(getattr(self._tray_icon, "visible", False))

    def _check_background_tray_ready(self, attempts):
        if self._tray_ready():
            return
        if attempts >= 10:
            self._show_window()
            self._set_status("系统托盘启动超时；已显示主窗口，避免应用隐藏后无法操作。")
            return
        try:
            self.root.after(500, lambda: self._check_background_tray_ready(attempts + 1))
        except tk.TclError:
            pass

    def _hide_to_tray(self):
        if not self._tray_ready():
            self._quit_application()
            return
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def _quit_application(self):
        if getattr(self, "_job_lock", None) is not None and self._job_lock.locked():
            self._show_window()
            if not messagebox.askyesno(
                    "退出应用",
                    "当前有日报任务正在运行。退出后，发送结果可能记为“结果不明”，程序不会自动重发。仍要退出？",
                    parent=self.root):
                return
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

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
        style.configure("TFrame", background="#f2f5f8")
        style.configure("TLabel", background="#f2f5f8", foreground="#203448")
        style.configure("TSeparator", background="#d5dee7")
        self.root.configure(background="#f2f5f8")
        screen_width = max(900, self.root.winfo_screenwidth())
        screen_height = max(680, self.root.winfo_screenheight())
        width, height = min(1240, screen_width - 60), min(880, screen_height - 90)
        minimum_width, minimum_height = min(900, width), min(620, height)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(minimum_width, minimum_height)
        self.wraplength = min(900, max(480, width - 300))
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        workspace = ttk.Frame(self.root)
        workspace.grid(row=0, column=0, sticky="nsew")
        self.navigation = SideNavigation(workspace)
        self.tabs = self.navigation
        self.settings_tab = self.tabs.add("账户与模型")
        self.mapping_tab = self.tabs.add("项目范围")
        self.owner_tab = self.tabs.add("高级：成员覆盖")
        self.status_tab = self.tabs.add("高级：状态覆盖")
        self.schedule_tab = self.tabs.add("自动运行")
        self.preview_tab = self.tabs.add("运行与诊断")
        self.history_tab = self.tabs.add("运行记录")
        self.status_var = tk.StringVar(value="正在读取本机配置…")
        status = tk.Frame(self.root, bg="#e8eef4", padx=14, pady=8)
        status.grid(row=1, column=0, sticky="ew")
        tk.Label(status, text="状态", bg="#e8eef4", fg="#4b6278",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 10))
        tk.Label(status, textvariable=self.status_var, bg="#e8eef4", fg="#324b62",
                 anchor="w", justify="left", wraplength=self.wraplength,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", fill="x", expand=True)
        self._build_settings()
        self._build_mapping()
        self._build_owner_mapping()
        self._build_status_mapping()
        self._build_schedule()
        self._build_preview()
        self._build_history()

    def _build_settings(self):
        form = self.settings_tab
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="项目与 MCP 授权", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self._text_field(form, 1, 0, "Teambition Tenant / Organization ID", "tenant_id")
        self._text_field(form, 1, 2, "Teambition Project ID", "project_id")
        ttk.Label(form, text="密钥使用 Windows 用户级 DPAPI 加密保存；不会写入日报、运行记录或模板。",
                  foreground="#536174").grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))
        row = 3
        self.project_choices = []
        self._project_choices_identity = None
        project_row = ttk.Frame(form)
        project_row.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(4, 8))
        project_row.columnconfigure(0, weight=1)
        self.project_combo = ttk.Combobox(project_row, state="readonly", values=())
        self.project_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.project_combo.bind("<<ComboboxSelected>>", self._select_discovered_project)
        self.load_projects_button = ttk.Button(project_row, text="加载我的项目", command=self._load_projects)
        self.load_projects_button.grid(row=0, column=1)
        ttk.Label(project_row, text="先保存个人 Token 并确认授权，再加载项目；也可手动输入上方 ID。选择后仍需保存。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=1, column=0, columnspan=2, sticky="w")
        row += 1
        for key in SECRET_FIELDS:
            self._secret_field(form, row, key)
            row += 1

        ttk.Label(form, text="艾特指定成员（可选，留空保持原消息）",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 4))
        row += 1
        self.values["dingtalk_mention_mode"] = tk.StringVar(value="mobile")
        ttk.Label(form, text="标识类型").grid(row=row, column=0, sticky="w")
        ttk.Combobox(form, textvariable=self.values["dingtalk_mention_mode"],
                     values=("mobile", "user_id"), state="readonly", width=18).grid(row=row, column=1, sticky="ew", padx=(0, 18))
        self._text_field(form, row, 2, "指定成员", "dingtalk_mentions", width=30)
        row += 1
        ttk.Label(form, text="mobile 为手机号，user_id 为钉钉 userId；多个标识用逗号、分号或空格分隔，不加 @，最多 50 人。不要填姓名、员工编号或 Teambition ID。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=row, column=0, columnspan=4, sticky="w", pady=(3, 5))
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
        ttk.Label(form, textvariable=self.model_status_var, foreground="#536174", wraplength=self.wraplength).grid(
            row=row, column=2, columnspan=2, sticky="w", pady=(2, 0))
        row += 1
        ttk.Label(form, text="模型列表只返回基础元数据；reasoning 档位来自本程序核验的模型文档。模型在列表中不代表 Responses 端点权限已验证。",
                  foreground="#8a4b00", wraplength=self.wraplength).grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 2))
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
                  foreground="#8a4b00", wraplength=self.wraplength).grid(row=row, column=0, columnspan=4, sticky="w", pady=(2, 4))
        row += 1
        ttk.Label(form, text="仓库规则版本：upcam-task-digest @ " + RULESET_REVISION[:12] +
                  "；日报模板随本程序版本发布并自动读取。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=row, column=0, columnspan=4,
                                                               sticky="w", pady=(8, 0))
        row += 1
        actions = ttk.Frame(form)
        actions.grid(row=row, column=0, columnspan=4, sticky="w", pady=(14, 4))
        ttk.Button(actions, text="保存本机配置", command=self._save).pack(side="left")
        ttk.Button(actions, text="只读测试 Teambition 连接", command=self._test_teambition).pack(side="left", padx=8)
        ttk.Button(actions, text="清除已保存服务密钥…", command=self._clear_secrets).pack(side="left")

    def _build_schedule(self):
        form = self.schedule_tab
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="自动运行时间与采集窗口（电脑本机时间）",
                  font=("Microsoft YaHei UI", 12, "bold")).grid(
                      row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        row = 1
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
        self._text_field(form, row, 0, "每批采集任务数（1–700）", "max_tasks_per_run", width=9)
        ttk.Label(form,
                  text="默认每批 300，非项目总数限制；单次最多 10,000 个候选。整次最多 50,000 次请求、1,000,000 条返回记录、512 MiB 响应、128 MiB 保留数据及两小时，最多 3 轮来源重核；单批预算仍有效。超限阻断，不截断。普通模式按更新时间向 Teambition 筛选；详细诊断仍会全项目复核。",
                  foreground="#536174", wraplength=550).grid(row=row, column=2, columnspan=2,
                                                              sticky="w", pady=4)
        row += 1
        self.values["auto_send_enabled"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="应用运行期间按以上时间自动采集、校验、摘要并发送到钉钉",
                        variable=self.values["auto_send_enabled"]).grid(
                            row=row, column=0, columnspan=4, sticky="w", pady=(8, 2))
        row += 1
        self.values["start_with_windows"] = tk.BooleanVar(value=False)
        self.startup_check = ttk.Checkbutton(
            form, text="当前 Windows 用户登录时自动启动应用并最小化到系统托盘",
            variable=self.values["start_with_windows"])
        self.startup_check.grid(row=row, column=0, columnspan=4, sticky="w", pady=(2, 2))
        if os.name != "nt":
            self.startup_check.configure(state="disabled")
        row += 1
        ttk.Label(form, text="自动补跑仅限应用重新打开后 24 小时内、且自动执行已启用期间错过的时段。开机启动需要 Windows 已开机并且该用户登录；关机、休眠或未联网时不会执行。已有记录（含结果不明）不会重发。",
                  foreground="#8a4b00", wraplength=self.wraplength).grid(
                      row=row, column=0, columnspan=4, sticky="w", pady=(0, 4))
        row += 1
        actions = ttk.Frame(form)
        actions.grid(row=row, column=0, columnspan=4, sticky="w", pady=(18, 4))
        ttk.Button(actions, text="保存本机配置", command=self._save).pack(side="left")

    def _build_mapping(self):
        form = self.mapping_tab
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="项目范围与只读验证",
                  font=("Microsoft YaHei UI", 12, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(form,
                  text="选择项目与时间即可使用固定日报。官方 MCP 自动读取任务、动态、成员姓名和任务状态；此验证不会调用 OpenAI 或发送钉钉。",
                  foreground="#536174", wraplength=self.wraplength).grid(
                      row=1, column=0, columnspan=2, sticky="w", pady=(5, 8))

        self.values["include_archived"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="将已归档任务纳入采集范围", variable=self.values["include_archived"],
                        command=self._scope_controls_changed).grid(
                            row=2, column=0, sticky="w", pady=(0, 5))
        probe_row = ttk.Frame(form)
        probe_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.project_probe_button = ttk.Button(
            probe_row, text="只读验证项目", command=self._probe_project)
        self.project_probe_button.pack(side="left")
        self.project_probe_status = tk.StringVar(value="尚未完成只读项目验证。")
        ttk.Label(probe_row, textvariable=self.project_probe_status, foreground="#536174",
                  wraplength=max(430, self.wraplength - 80)).pack(side="left", padx=(10, 0))
        ttk.Label(form,
                  text="验证使用“运行与诊断”页的开始和结束时间。为核对动态字段，请选择至少包含一条任务动态的区间。查询完整但无样本时不会解除门禁。",
                  foreground="#8a4b00", wraplength=self.wraplength).grid(
                      row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Separator(form).grid(row=5, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(form, text="项目视图范围",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(
                      row=6, column=0, columnspan=2, sticky="w", pady=(2, 3))
        ttk.Label(form,
                  text="默认范围是此 Project ID 下、日报时间窗内所有符合仓库规则的任务。需要缩小范围时，可添加稳定字段等值条件。当前 API 不会替你判断网页里的自定义视图是否等价。",
                  foreground="#536174", wraplength=self.wraplength).grid(
                      row=7, column=0, columnspan=2, sticky="w", pady=(0, 5))
        filter_controls = ttk.Frame(form)
        filter_controls.grid(row=8, column=0, columnspan=2, sticky="ew")
        self.view_filter_label = tk.StringVar(value=next(iter(VIEW_FILTERS)))
        ttk.Combobox(filter_controls, textvariable=self.view_filter_label,
                     values=tuple(VIEW_FILTERS), state="readonly", width=23).pack(side="left")
        self.view_filter_value = tk.StringVar()
        ttk.Label(filter_controls, text="字段值 / ID").pack(side="left", padx=(10, 4))
        ttk.Entry(filter_controls, textvariable=self.view_filter_value, width=32).pack(side="left")
        filter_actions = ttk.Frame(form)
        filter_actions.grid(row=9, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Button(filter_actions, text="添加 / 更新条件", command=self._add_view_filter).pack(side="left", padx=(0, 7))
        ttk.Button(filter_actions, text="删除选中条件", command=self._remove_view_filter).pack(side="left")
        self.view_filter_tree = ttk.Treeview(form, columns=("field", "value"), show="headings", height=3)
        self.view_filter_tree.heading("field", text="项目内筛选字段")
        self.view_filter_tree.heading("value", text="字段值")
        scope_table_width = max(470, self.wraplength - 25)
        self.view_filter_tree.column("field", width=int(scope_table_width * 0.38), anchor="w")
        self.view_filter_tree.column("value", width=int(scope_table_width * 0.62), anchor="w")
        self.view_filter_tree.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        self.scope_confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form,
                        text="我已确认：以上项目、归档选项和筛选条件就是日报应覆盖的范围。",
                        variable=self.scope_confirm_var).grid(
                            row=11, column=0, columnspan=2, sticky="w", pady=(6, 4))

    def _build_owner_mapping(self):
        form = self.owner_tab
        form.columnconfigure(0, weight=1)
        ttk.Label(form, text="成员映射",
                  font=("Microsoft YaHei UI", 12, "bold")).grid(
                      row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(form,
                  text="高级可选覆盖：成员姓名由 MCP 自动读取；仅未解析时补充已核对的姓名。已有覆盖保留，冲突时停止并提示。",
                  foreground="#536174", wraplength=self.wraplength).grid(
                      row=1, column=0, sticky="w", pady=(0, 8))
        owner_table = ttk.Frame(form)
        owner_table.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        owner_table.columnconfigure(0, weight=1)
        self.owner_tree = ttk.Treeview(owner_table, columns=("id", "name", "source"), show="headings", height=5)
        for column, title, width in (("id", "成员 ID", 190), ("name", "姓名", 130), ("source", "来源", 140)):
            self.owner_tree.heading(column, text=title)
            self.owner_tree.column(column, width=width, anchor="w")
        self.owner_tree.grid(row=0, column=0, sticky="nsew")
        owner_scroll = ttk.Scrollbar(owner_table, orient="vertical", command=self.owner_tree.yview)
        owner_scroll.grid(row=0, column=1, sticky="ns")
        self.owner_tree.configure(yscrollcommand=owner_scroll.set)
        self.owner_tree.bind("<<TreeviewSelect>>", self._select_owner_mapping)
        owner_controls = ttk.Frame(form)
        owner_controls.grid(row=3, column=0, sticky="ew", pady=(4, 2))
        owner_controls.columnconfigure(1, weight=1)
        self.owner_id_input = tk.StringVar()
        self.owner_name_input = tk.StringVar()
        ttk.Label(owner_controls, text="成员 ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(owner_controls, textvariable=self.owner_id_input, width=26).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(owner_controls, text="姓名").grid(row=1, column=0, sticky="w")
        ttk.Entry(owner_controls, textvariable=self.owner_name_input, width=26).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(owner_controls, text="保存成员映射", command=self._save_owner_mapping).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Button(owner_controls, text="删除选中映射", command=self._delete_owner_mapping).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(owner_controls, text="批量粘贴格式：每行一个“成员 ID<TAB>姓名”",
                  foreground="#536174").grid(row=3, column=0, columnspan=2, sticky="w", pady=(3, 1))
        self.owner_bulk_text = tk.Text(owner_controls, height=4, width=55, wrap="none", font=("Consolas", 9))
        self.owner_bulk_text.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.owner_import_button = ttk.Button(
            owner_controls, text="导入成员映射", command=self._import_owner_mappings)
        self.owner_import_button.grid(row=5, column=0, sticky="w", pady=5)

        ttk.Label(form,
                  text="导入支持 JSON 对象，或每行一个“成员 ID<TAB>姓名”。错误时整批拒绝。完成后到“账户与模型”或“自动运行”页面保存本机配置。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=4, column=0, sticky="w", pady=(8, 0))

    def _build_status_mapping(self):
        form = self.status_tab
        form.columnconfigure(0, weight=1)
        ttk.Label(form, text="状态映射与“已解决”标记",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(
                      row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(form,
                  text="高级可选覆盖：状态名称与完成属性由 MCP 自动读取。仅未解析时填写已核对的状态，不能凭名称或 ID 猜测已解决。",
                  foreground="#536174", wraplength=self.wraplength).grid(
                      row=1, column=0, sticky="w", pady=(0, 8))
        status_table = ttk.Frame(form)
        status_table.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        status_table.columnconfigure(0, weight=1)
        self.status_tree = ttk.Treeview(status_table, columns=("id", "name", "suggestion", "resolved"), show="headings", height=5)
        for column, title, width in (("id", "状态 ID", 150), ("name", "已保存名称", 105),
                                     ("suggestion", "样本名称建议", 125), ("resolved", "已解决", 60)):
            self.status_tree.heading(column, text=title)
            self.status_tree.column(column, width=width, anchor="w")
        self.status_tree.grid(row=0, column=0, sticky="nsew")
        status_scroll = ttk.Scrollbar(status_table, orient="vertical", command=self.status_tree.yview)
        status_scroll.grid(row=0, column=1, sticky="ns")
        self.status_tree.configure(yscrollcommand=status_scroll.set)
        self.status_tree.bind("<<TreeviewSelect>>", self._select_status_mapping)
        status_controls = ttk.Frame(form)
        status_controls.grid(row=3, column=0, sticky="ew", pady=(4, 2))
        status_controls.columnconfigure(1, weight=1)
        self.status_id_input = tk.StringVar()
        self.status_name_input = tk.StringVar()
        self.status_resolved_var = tk.BooleanVar(value=False)
        ttk.Label(status_controls, text="状态 ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(status_controls, textvariable=self.status_id_input, width=24).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(status_controls, text="名称").grid(row=1, column=0, sticky="w")
        ttk.Entry(status_controls, textvariable=self.status_name_input, width=24).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Checkbutton(status_controls, text="此状态代表已解决", variable=self.status_resolved_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Button(status_controls, text="采用选中状态的样本建议", command=self._use_status_suggestion).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Button(status_controls, text="保存状态映射", command=self._save_status_mapping).grid(
            row=4, column=0, sticky="w", pady=2)
        ttk.Button(status_controls, text="删除选中映射", command=self._delete_status_mapping).grid(
            row=4, column=1, sticky="w", pady=2)

        ttk.Label(form,
                  text="保存后，系统仍会对每日报表中的未知成员、未知状态、未完整翻页和不匹配项目范围实行阻断。当前只读接口不提供完整成员/状态列表；无法从样本可靠确认的内容必须在表格中补齐。",
                  foreground="#8a4b00", wraplength=self.wraplength).grid(
                      row=4, column=0, sticky="w", pady=(10, 4))
        self.owner_map = {}
        self.status_map = {}
        self.resolved_status_ids = set()
        self.owner_candidate_ids = set()
        self.status_candidate_ids = set()
        self.status_suggestions = {}
        self.status_conflicts = set()
        self.view_filters = {}
        self._setup_probe = None
        self._setup_probe_filters = None
        self._render_setup_tables()

    def _render_setup_tables(self):
        if hasattr(self, "view_filter_tree"):
            for item in self.view_filter_tree.get_children():
                self.view_filter_tree.delete(item)
            inverse = {value: key for key, value in VIEW_FILTERS.items()}
            for key, value in sorted(self.view_filters.items()):
                self.view_filter_tree.insert("", "end", iid=key, values=(inverse.get(key, key), value))
        if hasattr(self, "owner_tree"):
            for item in self.owner_tree.get_children():
                self.owner_tree.delete(item)
            for member_id in sorted(set(self.owner_map) | self.owner_candidate_ids):
                name = self.owner_map.get(member_id, "")
                source = "已保存" if name else "样本发现 ID；待核对姓名"
                self.owner_tree.insert("", "end", iid=member_id,
                                       values=(member_id, name, source))
        if hasattr(self, "status_tree"):
            for item in self.status_tree.get_children():
                self.status_tree.delete(item)
            for status_id in sorted(set(self.status_map) | self.status_candidate_ids):
                suggestion = ("样本名称冲突；请核对" if status_id in self.status_conflicts
                              else self.status_suggestions.get(status_id, ""))
                self.status_tree.insert(
                    "", "end", iid=status_id,
                    values=(status_id, self.status_map.get(status_id, ""),
                            suggestion,
                            "是" if status_id in self.resolved_status_ids else "否"))

    def _scope_controls_changed(self):
        if hasattr(self, "scope_confirm_var"):
            self.scope_confirm_var.set(False)

    def _add_view_filter(self):
        key = VIEW_FILTERS.get(self.view_filter_label.get())
        value = self.view_filter_value.get().strip()
        if not key or not value:
            messagebox.showerror("筛选条件无效", "请选择字段并填写字段值或 ID。", parent=self.root)
            return
        self.view_filters[key] = value
        self.view_filter_value.set("")
        self.scope_confirm_var.set(False)
        self._render_setup_tables()

    def _remove_view_filter(self):
        selection = self.view_filter_tree.selection()
        if not selection:
            return
        self.view_filters.pop(selection[0], None)
        self.scope_confirm_var.set(False)
        self._render_setup_tables()

    def _select_owner_mapping(self, _event=None):
        selection = self.owner_tree.selection()
        if selection:
            member_id = selection[0]
            self.owner_id_input.set(member_id)
            self.owner_name_input.set(self.owner_map.get(member_id, ""))

    def _save_owner_mapping(self):
        member_id, name = self.owner_id_input.get().strip(), self.owner_name_input.get()
        try:
            validate_mappings({"owner_names": {member_id: name}, "status_names": {}, "resolved_status_ids": []})
        except (CollectionError, ValueError, TypeError):
            messagebox.showerror("成员映射无效", "请输入有效的成员 ID 和姓名；姓名不能留空。", parent=self.root)
            return
        self.owner_map[member_id] = name
        self.owner_candidate_ids.add(member_id)
        self._render_setup_tables()
        self._set_status("成员映射已加入本机待保存配置。")

    def _delete_owner_mapping(self):
        selection = self.owner_tree.selection()
        member_id = selection[0] if selection else self.owner_id_input.get().strip()
        if member_id:
            self.owner_map.pop(member_id, None)
            self._render_setup_tables()

    def _import_owner_mappings(self):
        source = self.owner_bulk_text.get("1.0", "end-1c")
        try:
            parsed = parse_owner_mapping_tsv(source)
            merged = merge_owner_mappings(self.owner_map, parsed)
        except ProjectSetupError as exc:
            messagebox.showerror(
                "成员映射导入失败",
                str(exc) + " 支持 JSON 对象（{\"成员 ID\":\"姓名\"}）或每行“成员 ID<TAB>姓名”。未导入任何行。",
                parent=self.root)
            return
        self.owner_map = merged
        self.owner_candidate_ids.update(parsed)
        self.owner_bulk_text.delete("1.0", "end")
        self._render_setup_tables()
        self._set_status(f"已导入 {len(parsed)} 条成员映射；保存本机配置后生效。")

    def _select_status_mapping(self, _event=None):
        selection = self.status_tree.selection()
        if selection:
            status_id = selection[0]
            self.status_id_input.set(status_id)
            self.status_name_input.set(self.status_map.get(status_id, ""))
            self.status_resolved_var.set(status_id in self.resolved_status_ids)

    def _use_status_suggestion(self):
        selection = self.status_tree.selection()
        status_id = selection[0] if selection else self.status_id_input.get().strip()
        if status_id in self.status_suggestions:
            self.status_name_input.set(self.status_suggestions[status_id])
            self._set_status("已填入 Teambition 动态样本返回的状态名称；请核对后点击“保存状态映射”。")

    def _save_status_mapping(self):
        status_id, name = self.status_id_input.get().strip(), self.status_name_input.get()
        resolved = self.status_resolved_var.get()
        try:
            validate_mappings({"owner_names": {}, "status_names": {status_id: name},
                               "resolved_status_ids": [status_id] if resolved else []})
        except (CollectionError, ValueError, TypeError):
            messagebox.showerror("状态映射无效", "请输入有效的状态 ID 和名称。", parent=self.root)
            return
        self.status_map[status_id] = name
        self.status_candidate_ids.add(status_id)
        if resolved:
            self.resolved_status_ids.add(status_id)
        else:
            self.resolved_status_ids.discard(status_id)
        self._render_setup_tables()
        self._set_status("状态映射已加入本机待保存配置。")

    def _delete_status_mapping(self):
        selection = self.status_tree.selection()
        status_id = selection[0] if selection else self.status_id_input.get().strip()
        if status_id:
            self.status_map.pop(status_id, None)
            self.resolved_status_ids.discard(status_id)
            self._render_setup_tables()

    def _probe_project(self):
        settings = dict(self.settings)
        settings["tenant_id"] = self.values["tenant_id"].get().strip()
        settings["project_id"] = self.values["project_id"].get().strip()
        settings["include_archived"] = bool(self.values["include_archived"].get())
        try:
            settings["max_tasks_per_run"] = int(self.values["max_tasks_per_run"].get())
        except (ValueError, TypeError):
            messagebox.showerror("每批任务数无效", "每批采集任务数请输入 1 到 700 之间的整数。", parent=self.root)
            return
        secrets = dict(self.saved_secrets)
        secrets.update({key: self.values[key].get() for key in
                        ("teambition_mcp_token",) if self.values[key].get()})
        if not settings["tenant_id"] or not settings["project_id"] or any(
                not secrets.get(key) for key in ("teambition_mcp_token",)):
            messagebox.showinfo("需要配置", "请先填写 Tenant ID、Project ID、MCP 个人 Token。",
                                parent=self.root)
            return
        if not mcp_connection_authorized(secrets):
            messagebox.showinfo("需要连接授权", "请先保存个人 Token 并确认授权本机连接 Teambition 官方 MCP。", parent=self.root)
            return
        try:
            start_local, end_local = parse_manual_window(self.manual_start.get(), self.manual_end.get())
            start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
            report_day = end.date().isoformat()
            selection = updated_selection(report_day, start, end)
        except (SettingsError, TypeError, ValueError) as exc:
            messagebox.showerror("时间窗口无效", str(exc), parent=self.root)
            return
        settings["api_contract_verified"] = False
        self._setup_probe = None
        probe_filters = dict(self.view_filters)
        self.project_probe_status.set("正在只读查询所选区间；不会请求 OpenAI 或发送钉钉……")
        self._set_status("正在验证当前租户、项目和所选时间窗的只读查询。")
        self.project_probe_button.configure(state="disabled")

        def worker():
            try:
                client = _new_teambition_client(settings, secrets)
                collection = client.collect(settings["project_id"], day=report_day,
                                            window=(start, end),
                                            include_archived=settings["include_archived"])
                probe = analyze_project_probe(
                    collection, tenant_id=settings["tenant_id"], project_id=settings["project_id"],
                    expected_selection=selection, include_archived=settings["include_archived"])
                merge_discovered(runtime_config(settings), collection.get("metadata", {}))
                result, error = probe, None
            except ProjectSetupError as exc:
                result, error = None, str(exc)
            except (CollectionError, OSError) as exc:
                result, error = None, collection_validation_message(exc)
            except Exception:
                result, error = None, "只读项目验证失败；详细响应未写入日志，也没有更改本机核验状态。"
            self.root.after(0, lambda: self._project_probe_finished(
                result, error, settings["tenant_id"], settings["project_id"],
                settings["include_archived"], probe_filters, selection, teambition_mcp_identity(secrets)))

        threading.Thread(target=worker, daemon=True, name="teambition-project-setup").start()

    def _project_probe_finished(self, probe, error, tenant_id, project_id, include_archived,
                                filters, selection, credential_identity=None):
        self.project_probe_button.configure(state="normal")
        if error:
            self.project_probe_status.set(error)
            self._set_status(error)
            messagebox.showerror("项目验证未完成", error, parent=self.root)
            return
        self._setup_probe = {
            "probe": probe, "tenant_id": tenant_id, "project_id": project_id,
            "include_archived": include_archived, "filters": filters,
            "selection": selection, "credential_identity": credential_identity,
        }
        self.scope_confirm_var.set(False)
        self.owner_candidate_ids.update(probe.owner_ids)
        self.status_candidate_ids.update(probe.status_ids)
        self.status_conflicts = set(probe.conflicting_status_ids)
        for status_id, name in probe.status_name_suggestions.items():
            if status_id not in probe.conflicting_status_ids:
                self.status_suggestions[status_id] = name
        self._render_setup_tables()
        self.project_probe_status.set(
            f"只读样本通过：{probe.task_count} 个任务、{probe.activity_count} 条动态；"
            f"发现 {len(probe.owner_ids)} 个成员 ID、{len(probe.status_ids)} 个状态 ID。")
        self._set_status("MCP 字段样本已核验。请确认项目范围并保存；成员姓名与状态在每次采集时自动读取。")
        messagebox.showinfo(
            "只读验证通过",
            f"已完整读取所选区间的 {probe.task_count} 个任务和 {probe.activity_count} 条动态。\n"
            f"发现 {len(probe.owner_ids)} 个成员 ID 与 {len(probe.status_ids)} 个状态 ID。\n\n"
            "成员姓名与任务状态在每次采集时由 MCP 自动读取。仅未解析时需要高级覆盖。\n"
            "最后请确认上方项目视图范围并保存配置。",
            parent=self.root)

    def _build_preview(self):
        tab = self.preview_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(5, weight=1)
        ttk.Label(tab, text="自选采集起止时间", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(tab, text="使用本机时间 YYYY-MM-DD HH:MM。自动日报按仓库模板生成；发送前由程序校验来源、窗口、映射、未知事件和模型输出。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=1, column=0, sticky="w", pady=(4, 10))
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
        diagnostic_controls = ttk.Frame(tab)
        diagnostic_controls.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.source_diagnostic_button = ttk.Button(
            diagnostic_controls, text="只读诊断当前数据（不调用模型 / 不发送）",
            command=self._diagnose_manual)
        self.source_diagnostic_button.pack(side="left")
        self.diagnostic_export_button = ttk.Button(
            diagnostic_controls, text="导出上次诊断的脱敏结构…", state="disabled",
            command=self._export_safe_diagnostic)
        self.diagnostic_export_button.pack(side="left", padx=(8, 0))
        ttk.Label(diagnostic_controls, text="使用已保存的项目和映射配置。",
                  foreground="#536174").pack(side="left", padx=(10, 0))
        ttk.Label(tab, text="立即运行同样无需人工审核；如果自动校验失败、AI 拒绝或发送结果不明，程序会停止且不会自动重试。",
                  foreground="#8a4b00", wraplength=self.wraplength).grid(row=4, column=0, sticky="w", pady=(8, 10))
        frame = ttk.Frame(tab)
        frame.grid(row=5, column=0, sticky="nsew")
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
        columns = ("created", "period", "source", "state", "note")
        self.history_view = ttk.Treeview(tab, columns=columns, show="headings", height=20)
        labels = {"created": "创建时间（UTC）", "period": "采集窗口（本机时间）",
                  "source": "来源", "state": "状态", "note": "本机诊断 / 结果说明"}
        widths = {"created": 155, "period": 280, "source": 90, "state": 110, "note": 390}
        for column in columns:
            self.history_view.heading(column, text=labels[column])
            self.history_view.column(column, width=widths[column], anchor="w")
        self.history_view.grid(row=1, column=0, sticky="nsew")
        self.history_view.bind("<Double-1>", self._show_history_detail)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.history_view.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.history_view.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(tab, orient="horizontal", command=self.history_view.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.history_view.configure(xscrollcommand=horizontal.set)
        ttk.Label(tab, text="发送结果不明或机器人拒绝时，该运行点会锁定，不自动重发；先处理账本状态再恢复。",
                  foreground="#536174", wraplength=self.wraplength).grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _show_history_detail(self, _event=None):
        selection = self.history_view.selection()
        if not selection:
            return
        row = self.history.get(selection[0])
        if not row:
            return
        note = row.get("delivery_note") or "没有附加错误说明。"
        messagebox.showinfo("本机运行记录", f"状态：{row['state']}\n\n{note}", parent=self.root)

    def _text_field(self, parent, row, label_column, label, key, width=28):
        value = tk.StringVar()
        self.values[key] = value
        if key in ("tenant_id", "project_id"):
            value.trace_add("write", lambda *_args: self._scope_controls_changed())
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

    def _load_form(self):
        for key, variable in self.values.items():
            if key in self.settings:
                value = self.settings[key]
                variable.set(value.replace("\n", ", ") if key == "dingtalk_mentions" else value)
        self.owner_map = dict(self.settings.get("owner_names", {}))
        self.status_map = dict(self.settings.get("status_names", {}))
        self.resolved_status_ids = set(self.settings.get("resolved_status_ids", []))
        self.owner_candidate_ids = set(self.owner_map)
        self.status_candidate_ids = set(self.status_map)
        view = self.settings.get("view", DEFAULT_SETTINGS["view"])
        self.view_filters = dict(view.get("task_equals", {}))
        self.scope_confirm_var.set(view.get("verified") is True and bool(view.get("evidence")))
        if view.get("verified") is True and view.get("evidence"):
            self.project_probe_status.set("已保存的项目范围验证有效；若更改租户、项目、归档选项或筛选条件，需要重新验证。")
        self._render_setup_tables()
        self._load_model_cache()
        self._configure_effort_options()
        for key in SECRET_FIELDS:
            getattr(self, f"_secret_state_{key}").configure(
                text="本机已加密保存" if self.saved_secrets.get(key) else "本机未保存")

    def _validate_auto_ready(self, settings, secrets):
        if not mcp_connection_authorized(secrets):
            raise SettingsError("请先保存个人 Token 并确认授权本机连接 Teambition 官方 MCP。")
        if not self.ledger_ready or not secrets.get("delivery_ledger_id"):
            raise SettingsError("本机防重账本未通过验证")
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise SettingsError("请填写 Teambition Tenant ID 和 Project ID")
        config = validate_settings(settings)
        if (config.get("api_contract_verified") is not True or not config["view"].get("verified")
                or not config["view"].get("evidence")):
            raise SettingsError("请先在项目映射页核验 API 字段和项目视图范围")
        required = ("teambition_mcp_token", "dingtalk_webhook",
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
        previous_auto_send = bool(self.settings.get("auto_send_enabled"))
        for key in DEFAULT_SETTINGS:
            if key in self.values:
                value = self.values[key].get()
                if key == "max_tasks_per_run":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        raise SettingsError("每批采集任务数请输入 1 到 700 之间的整数") from None
                proposed[key] = value
        authorized_mcp_identity = None
        try:
            DigestDesktopApp._apply_project_display_draft(self, proposed)
            if bool(proposed["auto_send_enabled"]) != previous_auto_send:
                proposed["auto_send_enabled_since_local"] = (
                    datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
                    if proposed["auto_send_enabled"] else "")
            proposed["owner_names"] = dict(self.owner_map)
            proposed["status_names"] = dict(self.status_map)
            proposed["resolved_status_ids"] = sorted(self.resolved_status_ids)
            old_view = self.settings.get("view", DEFAULT_SETTINGS["view"])
            filters = dict(self.view_filters)
            entered_token = self.values["teambition_mcp_token"].get()
            effective_secrets = dict(self.saved_secrets)
            if entered_token:
                effective_secrets["teambition_mcp_token"] = entered_token
            mcp_token_changed = effective_secrets.get("teambition_mcp_token") != self.saved_secrets.get("teambition_mcp_token")
            if mcp_token_changed:
                effective_secrets.pop(MCP_AUTHORIZATION_FIELD, None)
            if teambition_mcp_identity(effective_secrets) and not mcp_connection_authorized(effective_secrets):
                if messagebox.askyesno("授权本机连接 Teambition 官方 MCP",
                        "个人 Token 将作为认证信息发送给 Teambition 官方 MCP 服务，用于只读获取项目、任务、动态、成员姓名和任务状态。\n\n"
                        "允许后会按你的主动操作、保存时核验或已启用的定时运行连接。Token 泄露可能使他人取得账号权限，请勿分享；清除服务密钥可撤销本机授权。\n\n"
                        "是否允许连接？选择否只保存在本机，不连接。", parent=self.root):
                    authorized_mcp_identity = teambition_mcp_identity(effective_secrets)
                    effective_secrets[MCP_AUTHORIZATION_FIELD] = authorized_mcp_identity
            credential_identity = (teambition_mcp_identity(effective_secrets)
                                   if mcp_connection_authorized(effective_secrets) else None)
            credentials_unchanged = bool(credential_identity and credential_identity ==
                                         teambition_mcp_identity(self.saved_secrets)
                                         and mcp_connection_authorized(self.saved_secrets))
            identity_unchanged = credentials_unchanged and all(
                proposed.get(key) == self.settings.get(key, DEFAULT_SETTINGS[key])
                for key in ("tenant_id", "project_id"))
            scope_unchanged = (identity_unchanged
                               and proposed["include_archived"] == self.settings.get("include_archived", False)
                               and filters == old_view.get("task_equals", {}))
            probe_matches = bool(self._setup_probe and credential_identity
                and self._setup_probe.get("credential_identity") == credential_identity and
                proposed["tenant_id"] == self._setup_probe["tenant_id"]
                and proposed["project_id"] == self._setup_probe["project_id"]
                and proposed["include_archived"] == self._setup_probe["include_archived"]
                and filters == self._setup_probe_filters_for_current_scope())
            prior_api_verified = self.settings.get("api_contract_verified") is True and identity_unchanged
            proposed["api_contract_verified"] = prior_api_verified or probe_matches
            prior_view_verified = (old_view.get("verified") is True and bool(old_view.get("evidence"))
                                   and scope_unchanged)
            view_verified = bool(self.scope_confirm_var.get() and (probe_matches or prior_view_verified))
            evidence = old_view.get("evidence", "") if prior_view_verified else ""
            if view_verified and probe_matches:
                probe = self._setup_probe["probe"]
                scope_hash = hashlib.sha256(json.dumps(
                    {"tenant_id": proposed["tenant_id"], "project_id": proposed["project_id"],
                     "include_archived": proposed["include_archived"], "task_equals": filters},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
                evidence = ("desktop-readonly-probe/v1; "
                            f"tasks={probe.task_count}; activities={probe.activity_count}; "
                            f"selection={probe.selection['start']}/{probe.selection['end_exclusive']}; "
                            f"scope_sha256={scope_hash}; operator_confirmed=true")
            proposed["view"] = {"verified": view_verified, "evidence": evidence if view_verified else "",
                                 "task_equals": filters}
            proposed["schema_version"] = 1
            if not view_verified:
                proposed["auto_send_enabled"] = False
                proposed["auto_send_enabled_since_local"] = ""
            clean = validate_settings(proposed)
        except (SettingsError, ValueError, TypeError) as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return
        entered = {key: self.values[key].get() for key in SECRET_FIELDS if self.values[key].get()}
        previous_startup = bool(self.settings.get("start_with_windows", False))
        startup_changed = clean["start_with_windows"] != previous_startup
        startup_applied = False
        try:
            prior_credentials = self.secret_store.load()
            merged = dict(prior_credentials)
            merged.update(entered)
            if mcp_token_changed or merged.get("teambition_mcp_token") != prior_credentials.get("teambition_mcp_token"):
                merged.pop(MCP_AUTHORIZATION_FIELD, None)
            if authorized_mcp_identity is not None:
                merged[MCP_AUTHORIZATION_FIELD] = authorized_mcp_identity
            reject_credential_mentions(clean, prior_credentials, merged)
            if clean["auto_send_enabled"]:
                self._validate_auto_ready(clean, merged)
            if startup_changed:
                configure_user_startup(clean["start_with_windows"])
                startup_applied = True
            self.secret_store.save(merged)
            self.settings_store.save(clean)
        except (SettingsError, SecretStoreError, StartupIntegrationError, MentionConfigError, OSError) as exc:
            if startup_applied:
                try:
                    configure_user_startup(previous_startup)
                except StartupIntegrationError:
                    pass
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return
        self.settings, self.saved_secrets = clean, merged
        self._project_display_draft = None
        if clean["auto_send_enabled"]:
            self._auto_run_enabled_since = datetime.fromisoformat(clean["auto_send_enabled_since_local"])
        else:
            self._auto_run_enabled_since = datetime.now().replace(microsecond=0)
        for key in SECRET_FIELDS:
            self.values[key].set("")
            getattr(self, f"_secret_state_{key}").configure(
                text="本机已加密保存" if self.saved_secrets.get(key) else "本机未保存")
        self._load_model_cache()
        self._set_status("本机设置已保存。" + ("自动执行已启用。" if clean["auto_send_enabled"] else "自动执行当前关闭。")
                         + ("" if mcp_connection_authorized(merged) else " MCP 连接尚未授权；仅保存 Token 不会连接。"))
        startup_note = ("开机启动已启用。" if clean["start_with_windows"] else "开机启动当前关闭。")
        messagebox.showinfo("保存完成", "设置已保存在本机。" + startup_note +
                            "服务密钥使用 Windows DPAPI 加密。", parent=self.root)

    def _setup_probe_filters_for_current_scope(self):
        return self._setup_probe["filters"] if self._setup_probe else None

    def _clear_secrets(self):
        if not messagebox.askyesno("清除密钥", "清除本机保存的 Teambition、钉钉与 OpenAI 密钥？防重复账本身份会保留。",
                                   parent=self.root):
            return
        try:
            current_credentials = self.secret_store.load()
            entered = {key: self.values[key].get() for key in SECRET_FIELDS if self.values[key].get()}
            pending = dict(self.settings)
            for key in ("dingtalk_mention_mode", "dingtalk_mentions"):
                if key in self.values:
                    pending[key] = self.values[key].get()
            reject_credential_mentions(self.settings, self.saved_secrets, current_credentials, entered)
            reject_credential_mentions(pending, self.saved_secrets, current_credentials, entered)
        except (MentionConfigError, SecretStoreError, OSError) as exc:
            messagebox.showerror("无法清除密钥", str(exc), parent=self.root)
            return
        self.settings["auto_send_enabled"] = False
        self.settings["auto_send_enabled_since_local"] = ""
        self._auto_run_enabled_since = datetime.now().replace(microsecond=0)
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

    def _load_projects(self):
        # Loading is always a deliberate action, never an initialization side effect.
        if self.values["teambition_mcp_token"].get():
            messagebox.showinfo("需要先保存", "请先保存新 Token 并确认连接授权，再加载项目列表。", parent=self.root)
            return
        secrets = self.secret_store.load()
        if not mcp_connection_authorized(secrets):
            messagebox.showinfo("需要连接授权", "请先保存个人 Token 并确认授权本机连接 Teambition 官方 MCP。", parent=self.root)
            return
        if not self._job_lock.acquire(blocking=False):
            self._set_status("已有采集或发送正在运行；请完成后再加载项目列表。")
            return
        identity = teambition_mcp_identity(secrets)
        self.project_choices = []
        self._project_choices_identity = None
        self.project_combo.configure(values=())
        self.project_combo.set("")
        self.load_projects_button.configure(state="disabled")
        self._set_status("正在只读加载我的项目；原项目配置保持不变。")

        def worker():
            try:
                projects, error = list_mcp_projects(secrets), None
            except Exception as exc:
                projects = []
                error = ("Teambition MCP 个人 Token 已失效或无读取权限；请重新生成 Token 并保存，确认当前账户有项目读取权限。"
                         if isinstance(exc, CollectionError) and str(exc) == "Teambition MCP authorization expired or denied" else
                         "MCP 项目列表加载失败，可能是读取合同未核验、分页复核不一致或无权限；当前项目未更改。可手动输入 Tenant / Organization ID 和 Project ID 后再核验。")
            finally:
                self._job_lock.release()
            self.root.after(0, lambda: self._projects_loaded(projects, error, identity))
        threading.Thread(target=worker, daemon=True, name="teambition-project-list").start()

    def _projects_loaded(self, projects, error, identity):
        self.load_projects_button.configure(state="normal")
        current = self.secret_store.load()
        if not mcp_connection_authorized(current) or teambition_mcp_identity(current) != identity:
            error = "MCP 授权在加载期间已改变；未返回项目列表，请重新授权后加载。"
        if error:
            self.project_choices = []
            self._project_choices_identity = None
            self.project_combo.configure(values=())
            self.project_combo.set("")
            self._set_status(error)
            messagebox.showerror("项目列表未完成", error, parent=self.root)
            return
        self.project_choices = list(projects)
        self._project_choices_identity = identity
        self.project_combo.configure(values=tuple(
            f"{project['name']} · {project['organizationId']} · {project['id']}" for project in projects))
        self._set_status("当前账号参与的可见项目，列表可能不完整；找不到可手动填 ID。列表不含归档项目，选择后保存生效。" if projects else
                         "未找到当前账号参与的可见项目，列表可能不完整且不含归档项目；原项目配置保持不变，可手动填 ID。")

    def _select_discovered_project(self, _event=None):
        current = self.secret_store.load()
        if (not mcp_connection_authorized(current)
                or teambition_mcp_identity(current) != self._project_choices_identity):
            self._set_status("项目列表对应的 MCP 授权已改变；请重新加载项目列表。")
            return
        index = self.project_combo.current()
        if not 0 <= index < len(self.project_choices):
            return
        selected = self.project_choices[index]
        scope = {"tenant_id": selected["organizationId"], "project_id": selected["id"]}
        previous_draft = getattr(self, "_project_display_draft", None)
        saved = getattr(self, "settings", {})
        if previous_draft and previous_draft["scope"] == scope:
            display = dict(previous_draft["display"])
        elif all(saved.get(key) == value for key, value in scope.items()):
            display = {key: saved.get(key, DEFAULT_SETTINGS[key]) for key in
                       ("task_number_prefix", "task_link_view_id", "task_display_scope")}
        else:
            # A different project's prefix/view cannot follow the new draft.
            # The saved original remains intact until the user explicitly saves.
            display = {"task_number_prefix": "", "task_link_view_id": "", "task_display_scope": None}
        if "uniqueIdPrefix" in selected:
            display["task_number_prefix"] = selected["uniqueIdPrefix"]
            display["task_display_scope"] = dict(scope)
        self._project_display_draft = {"scope": scope, "display": display}
        self.values["tenant_id"].set(selected["organizationId"])
        self.values["project_id"].set(selected["id"])
        self._set_status("项目已填入表单；原配置保持不变，保存后生效。")

    def _apply_project_display_draft(self, proposed):
        """Apply only the explicitly selected project's display draft at save."""
        draft = getattr(self, "_project_display_draft", None)
        if draft and all(proposed.get(key) == value for key, value in draft["scope"].items()):
            proposed.update(draft["display"])

    def _test_teambition(self):
        settings = dict(self.settings)
        for key in ("tenant_id", "project_id"):
            settings[key] = self.values[key].get().strip()
        secrets = dict(self.saved_secrets)
        secrets.update({key: self.values[key].get() for key in
                        ("teambition_mcp_token",) if self.values[key].get()})
        if not settings["tenant_id"] or not settings["project_id"] or any(
                not secrets.get(key) for key in ("teambition_mcp_token",)):
            messagebox.showinfo("需要配置", "填写 Tenant ID、Project ID、MCP 个人 Token 后再测试。",
                                parent=self.root)
            return
        if not mcp_connection_authorized(secrets):
            messagebox.showinfo("需要连接授权", "请先保存个人 Token 并确认授权本机连接 Teambition 官方 MCP。", parent=self.root)
            return
        self._set_status("正在执行 Teambition 只读项目查询；不会请求 OpenAI 或发送钉钉。")

        def worker():
            try:
                now = datetime.now().astimezone(SHANGHAI)
                start, end = now - timedelta(minutes=5), now
                client = _new_teambition_client(settings, secrets)
                collection = client.collect(settings["project_id"], day=end.date().isoformat(),
                                            window=(start, end), include_archived=settings["include_archived"])
                result, error = f"Teambition 只读查询成功；收到 {len(collection['records'])} 条任务记录。", None
            except (CollectionError, OSError) as exc:
                result, error = None, collection_validation_message(exc)
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

    def _diagnose_manual(self):
        """Read Teambition and run local source/event checks only; never summarize or send."""
        try:
            start_local, end_local = parse_manual_window(self.manual_start.get(), self.manual_end.get())
        except SettingsError as exc:
            messagebox.showerror("时间窗口无效", str(exc), parent=self.root)
            return
        if not self._job_lock.acquire(blocking=False):
            self._set_status("已有日报或诊断任务正在运行；请等待完成后再检查。")
            return
        required = ("teambition_mcp_token",)
        if (not self.settings.get("tenant_id") or not self.settings.get("project_id")
                or any(not self.saved_secrets.get(key) for key in required)):
            self._job_lock.release()
            messagebox.showinfo(
                "需要已保存的 Teambition 配置",
                "请先在“账户与模型”填写并保存 Tenant ID、Project ID、Teambition MCP 个人 Token。此项诊断不需要 OpenAI 或钉钉密钥。",
                parent=self.root)
            return
        settings, secrets = dict(self.settings), dict(self.saved_secrets)
        self.source_diagnostic_button.configure(state="disabled")
        self.diagnostic_export_button.configure(state="disabled")
        self.last_diagnostic_shape = None
        self._set_status("正在只读读取 Teambition 并本地核验；不会调用 OpenAI 或发送钉钉……")

        def worker():
            collection = None
            safe_shape = None
            try:
                start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
                day = end.date().isoformat()
                config = runtime_config(settings)
                client = _new_teambition_client(settings, secrets)
                collection = client.collect(
                    settings["project_id"], day=day, window=(start, end),
                    include_archived=settings["include_archived"])
                config = merge_discovered(config, collection.get("metadata", {}))
                diagnostic = diagnose_automatic_source(collection, config, day, (start, end))
                try:
                    safe_shape = safe_structure_export(collection, config, day, (start, end))
                except Exception:
                    diagnostic["message"] += (
                        "\n当前诊断可查看，但安全结构文件生成失败；不会导出原始响应。"
                    )
            except (CollectionError, OSError) as exc:
                diagnostic = {"ok": False, "message": collection_validation_message(exc), "task_count": None}
            except SettingsError as exc:
                diagnostic = {"ok": False,
                              "message": "本机设置未通过校验：" + str(exc) + "。先保存正确配置后重新诊断。",
                              "task_count": None}
            except Exception:
                diagnostic = {"ok": False,
                              "message": "只读诊断未完成；没有调用 OpenAI、没有发送钉钉，详细 API 响应不会写入记录。请检查本机网络、Teambition 凭据与项目读取权限。",
                              "task_count": None}
            finally:
                self._job_lock.release()
            diagnostic["safe_shape"] = safe_shape
            try:
                self.root.after(0, lambda: self._diagnostic_finished(diagnostic))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="upcam-readonly-diagnostic").start()

    def _diagnostic_finished(self, diagnostic):
        self.source_diagnostic_button.configure(state="normal")
        self.last_diagnostic_shape = diagnostic.get("safe_shape")
        self.diagnostic_export_button.configure(
            state="normal" if self.last_diagnostic_shape else "disabled")
        message = diagnostic["message"]
        self._show_preview(message)
        self._set_status(message.splitlines()[0])
        if diagnostic["ok"]:
            messagebox.showinfo("只读诊断通过", message, parent=self.root)
        else:
            messagebox.showwarning("只读诊断发现问题", message, parent=self.root)
        self._refresh_history()

    def _export_safe_diagnostic(self):
        if not isinstance(self.last_diagnostic_shape, dict):
            messagebox.showinfo("没有可导出的诊断", "请先完成一次“只读诊断当前数据”。", parent=self.root)
            return
        target = filedialog.asksaveasfilename(
            parent=self.root, title="保存脱敏动态结构诊断",
            initialfile="teambition-safe-structure.json",
            defaultextension=".json", filetypes=(("JSON 文件", "*.json"),))
        if not target:
            return
        try:
            Path(target).write_text(
                json.dumps(self.last_diagnostic_shape, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except OSError:
            messagebox.showerror("导出失败", "无法写入所选文件；请检查目标文件夹的权限和可用空间。", parent=self.root)
            return
        messagebox.showinfo(
            "脱敏结构已导出",
            "文件只包含任务/动态 ID、API 字段名、值类型和字符串长度，并用真假值表示映射是否匹配；"
            "不包含任务标题、姓名、评论正文、凭据或 API 响应的字段值。分享前请先自行检查文件。",
            parent=self.root)

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
                manual_options = {} if scheduled else {
                    "allow_confirmed_repeat": True, "manual_request_id": str(uuid.uuid4()),
                }
                result = workflow.run(start, end, schedule_key=schedule_key, **manual_options)
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
                slots = recoverable_schedule_occurrences(
                    self.settings, now, enabled_since=self._auto_run_enabled_since)
                self._handled_schedule_slots.intersection_update(item[2] for item in slots)
                for slot, kind, key in slots:
                    if key in self._handled_schedule_slots or key in self._scheduled_active:
                        continue
                    if self.history.has_schedule_key(key):
                        self._handled_schedule_slots.add(key)
                        continue
                    start, end = scheduled_window(self.settings, kind, slot.date())
                    if self._launch_scheduled(start, end, key):
                        self._handled_schedule_slots.add(key)
        except (SettingsError, OSError, sqlite3.Error):
            self._set_status("自动调度检查失败；本次未启动日报。")
        finally:
            try:
                self.root.after(15000, self._schedule_tick)
            except tk.TclError:
                pass

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
            start, end = parse_manual_window(self.manual_start.get(), self.manual_end.get())
            start, end = start.astimezone(SHANGHAI), end.astimezone(SHANGHAI)
            data = json.loads(SAMPLE.read_text(encoding="utf-8"))
            data["date"] = end.date().isoformat()
            data['time_range'] = {'start': start.isoformat(), 'end_exclusive': end.isoformat(),
                                  'timezone': 'Asia/Shanghai'}
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
            note = run.get("delivery_note") or "—"
            self.history_view.insert("", "end", iid=run["run_id"],
                                     values=(run["created_at_utc"], period,
                                             run["source"], state, note))

    def _set_status(self, message):
        self.status_var.set(message)


def main():
    try:
        options = parse_launch_options(sys.argv[1:])
    except ValueError:
        return 2
    _enable_windows_dpi_awareness()
    current = {"app": None, "pending_show": False}

    def request_show():
        app = current["app"]
        if app is None:
            current["pending_show"] = True
            return
        try:
            app.root.after(0, app._show_window)
        except tk.TclError:
            pass

    instance = WindowsSingleInstance(request_show)
    try:
        if not instance.acquire():
            return 0
        root = tk.Tk()
        app = DigestDesktopApp(root, start_hidden=options.background, tray_enabled=True)
        current["app"] = app
        if current["pending_show"]:
            root.after_idle(app._show_window)
        smoke_exit_code = {"value": 0}
        if options.smoke_test:
            def finish_smoke_test():
                try:
                    root.update_idletasks()
                    verify_desktop_smoke(app, assert_hidden=options.background,
                                         exercise_controls=True,
                                         screenshot_path=options.capture_screenshot)
                except Exception as exc:
                    print(f"UI smoke test failed: {exc}", file=sys.stderr)
                    smoke_exit_code["value"] = 4
                finally:
                    app._quit_application()

            root.after(700, finish_smoke_test)
        root.mainloop()
        return smoke_exit_code["value"]
    except StartupIntegrationError:
        return 3
    finally:
        instance.close()


def _enable_windows_dpi_awareness():
    """Request per-monitor DPI scaling for this process without changing system settings."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        try:
            set_context = user32.SetProcessDpiAwarenessContext
            set_context.argtypes = (ctypes.c_void_p,)
            set_context.restype = ctypes.c_bool
            if set_context(ctypes.c_void_p(-4)):
                return True
        except (AttributeError, OSError, ctypes.ArgumentError):
            pass
        try:
            set_awareness = ctypes.windll.shcore.SetProcessDpiAwareness
            set_awareness.argtypes = (ctypes.c_int,)
            set_awareness.restype = ctypes.c_long
            return set_awareness(2) == 0
        except (AttributeError, OSError, ctypes.ArgumentError):
            return False
    except (AttributeError, OSError, OverflowError):
        return False


if __name__ == "__main__":
    main()
