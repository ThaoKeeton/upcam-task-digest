"""Small, non-business application preferences shared by native and worker UI."""

PREFERENCE_DEFAULTS = {
    "ui_language": "system",
    "ui_theme": "system",
    "tray_icon_enabled": True,
    "close_to_tray": True,
    "start_in_tray": False,
    "auto_check_updates": False,
    "start_with_windows": False,
}


class PreferenceError(ValueError):
    pass


def preference_values(settings: dict) -> dict:
    values = {key: settings.get(key, default) for key, default in PREFERENCE_DEFAULTS.items()}
    if values["ui_language"] not in ("system", "zh-CN", "en-US"):
        raise PreferenceError("界面语言只能选择 Windows 默认、简体中文或 English。")
    if values["ui_theme"] not in ("system", "light", "dark"):
        raise PreferenceError("应用主题只能选择 Windows 默认、浅色或深色。")
    if any(type(values[key]) is not bool for key, default in PREFERENCE_DEFAULTS.items()
           if type(default) is bool):
        raise PreferenceError("启动、托盘和更新检查选项必须为开启或关闭。")
    if not values["tray_icon_enabled"] and (values["close_to_tray"] or values["start_in_tray"]):
        raise PreferenceError("关闭托盘图标前，请关闭隐藏到托盘和启动时隐藏选项。")
    return values


def validate_update_token(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 512
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise PreferenceError("GitHub 只读令牌格式无效；不要输入空格、换行或请求头。")
    return value
