using System.Runtime.InteropServices;
using System.Text;
using Microsoft.UI.Xaml;

namespace Upcam.Desktop;

/// <summary>Native shell tray integration; no registry or system-wide settings are changed here.</summary>
internal sealed class NativeDesktop : IDisposable
{
    private readonly IntPtr _hwnd;
    private readonly SubclassProc _callback;
    private readonly Action _show;
    private readonly Action _exit;
    private readonly Func<bool> _closeRequested;
    private readonly string _iconPath = Path.Combine(AppContext.BaseDirectory, "Assets", "Teambition.ico");
    private IntPtr _trayIcon;
    private IntPtr _largeWindowIcon;
    private readonly uint _taskbarCreated = RegisterWindowMessage("TaskbarCreated");
    private const uint TrayMessage = 0x8000 + 37;
    public bool TrayReady { get; private set; }
    private bool _trayEnabled = true;
    private bool _addingTray;
    private readonly Func<uint, bool>? _trayRegistration;

    public NativeDesktop(Window window, Action show, Action exit, Func<bool> closeRequested, Func<uint, bool>? trayRegistration = null)
    {
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        _show = show;
        _exit = exit;
        _closeRequested = closeRequested;
        _trayRegistration = trayRegistration;
        _callback = WindowProcedure;
        if (!SetWindowSubclass(_hwnd, _callback, 1, 0))
            throw new InvalidOperationException("无法初始化窗口消息处理。");
        try
        {
            uint dpi = GetDpiForWindow(_hwnd);
            _trayIcon = LoadAssetIcon(dpi);
            _largeWindowIcon = LoadAssetIcon(dpi, small: false);
            SetWindowIcons();
            AddIcon();
        }
        catch
        {
            SendMessage(_hwnd, 0x0080, UIntPtr.Zero, IntPtr.Zero);
            SendMessage(_hwnd, 0x0080, (UIntPtr)1, IntPtr.Zero);
            if (_trayIcon != IntPtr.Zero) DestroyIcon(_trayIcon);
            if (_largeWindowIcon != IntPtr.Zero) DestroyIcon(_largeWindowIcon);
            RemoveWindowSubclass(_hwnd, _callback, 1);
            throw;
        }
    }

    private NotifyIconData Data() => new()
    {
        Size = (uint)Marshal.SizeOf<NotifyIconData>(), Window = _hwnd, Id = 1,
        Flags = 1 | 2 | 4, Callback = TrayMessage, Icon = _trayIcon,
        Tip = UiStrings.T("UPCAM 站会日报"), Info = "", InfoTitle = ""
    };

    private void AddIcon()
    {
        if (!_trayEnabled || _addingTray) return;
        _addingTray = true;
        try
        {
            NotifyIconData data = Data();
            TrayReady = NotifyShell(0, ref data);
            if (!TrayReady && !IsWindowVisible(_hwnd)) _show();
        }
        finally { _addingTray = false; }
    }

    private bool NotifyShell(uint operation, ref NotifyIconData data) =>
        _trayRegistration?.Invoke(operation) ?? ShellNotifyIcon(operation, ref data);

    internal void ConfigureTray(bool enabled)
    {
        _trayEnabled = enabled;
        if (!enabled)
        {
            NotifyIconData previous = Data();
            NotifyShell(2, ref previous);
            TrayReady = false;
            if (!IsWindowVisible(_hwnd)) _show();
        }
        if (enabled && !TrayReady) AddIcon();
        else if (enabled)
        {
            NotifyIconData current = Data();
            TrayReady = NotifyShell(1, ref current);
            if (!TrayReady) AddIcon();
        }
    }

    private IntPtr LoadAssetIcon(uint dpi, bool small = true)
    {
        IntPtr icon = LoadImage(IntPtr.Zero, _iconPath, 1,
            GetSystemMetricsForDpi(small ? 49 : 11, dpi), GetSystemMetricsForDpi(small ? 50 : 12, dpi), 0x10);
        if (icon == IntPtr.Zero) throw new InvalidOperationException("无法读取应用图标；请完整解压 Assets 文件夹。");
        return icon;
    }

    private void UpdateIconForDpi(uint dpi)
    {
        IntPtr replacement = LoadAssetIcon(dpi);
        IntPtr large;
        try { large = LoadAssetIcon(dpi, small: false); }
        catch { DestroyIcon(replacement); throw; }
        IntPtr previous = _trayIcon;
        IntPtr previousLarge = _largeWindowIcon;
        _trayIcon = replacement;
        _largeWindowIcon = large;
        SetWindowIcons();
        if (_trayEnabled)
        {
            NotifyIconData data = Data();
            TrayReady = NotifyShell(1, ref data);
            if (!TrayReady) AddIcon();
        }
        if (previous != IntPtr.Zero) DestroyIcon(previous);
        if (previousLarge != IntPtr.Zero) DestroyIcon(previousLarge);
    }

    private void SetWindowIcons()
    {
        // Select exact small/large frames instead of letting AppWindow resize a
        // different frame. The small window and tray share the same owned HICON.
        SendMessage(_hwnd, 0x0080, UIntPtr.Zero, _trayIcon);
        SendMessage(_hwnd, 0x0080, (UIntPtr)1, _largeWindowIcon);
    }

    internal IntPtr TrayIconHandle => _trayIcon;
    internal IntPtr WindowIconHandle => SendMessage(_hwnd, 0x007F, UIntPtr.Zero, IntPtr.Zero);

    internal (int Width, int Height) IconSizeForDpi(uint dpi)
    {
        IntPtr icon = LoadAssetIcon(dpi);
        try { return IconSize(icon); }
        finally { DestroyIcon(icon); }
    }

    internal static (int Width, int Height) IconSize(IntPtr icon)
    {
        if (icon == IntPtr.Zero || !GetIconInfo(icon, out var info))
            throw new InvalidOperationException("无法读取原生图标。");
        try
        {
            IntPtr bitmap = info.Color != IntPtr.Zero ? info.Color : info.Mask;
            if (GetObject(bitmap, Marshal.SizeOf<Bitmap>(), out var data) == 0)
                throw new InvalidOperationException("无法读取原生图标尺寸。");
            return (data.Width, info.Color != IntPtr.Zero ? data.Height : data.Height / 2);
        }
        finally
        {
            if (info.Color != IntPtr.Zero) DeleteObject(info.Color);
            if (info.Mask != IntPtr.Zero) DeleteObject(info.Mask);
        }
    }

    private IntPtr WindowProcedure(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam, UIntPtr id, UIntPtr data)
    {
        if ((message == 0x0010 || message == 0x0112 && (wparam.ToUInt64() & 0xFFF0) == 0xF060) && _closeRequested())
            return IntPtr.Zero;
        if (message == _taskbarCreated) AddIcon();
        // PowerToys retries a failed initial registration on window-position activity.
        // AddIcon is guarded against re-entry and restores only a hidden window.
        if (message == 0x0046 && _trayEnabled && !TrayReady) AddIcon();
        if (message == 0x02E0)
        {
            IntPtr result = DefSubclassProc(hwnd, message, wparam, lparam);
            // A missing moved asset must not throw across a native window callback.
            try { UpdateIconForDpi((uint)(wparam.ToUInt64() & 0xFFFF)); }
            catch (InvalidOperationException) { }
            return result;
        }
        if (message == TrayMessage)
        {
            int action = lparam.ToInt32();
            if (action is 0x0202 or 0x0203) _show();
            if (action == 0x0205) ShowMenu();
            return IntPtr.Zero;
        }
        return DefSubclassProc(hwnd, message, wparam, lparam);
    }

    public void RequestCaptionClose() => PostMessage(_hwnd, 0x0112, (UIntPtr)0xF060, IntPtr.Zero);
    internal void RequestSystemCommand(uint command) => PostMessage(_hwnd, 0x0112, (UIntPtr)command, IntPtr.Zero);

    private void ShowMenu()
    {
        IntPtr menu = CreatePopupMenu();
        if (menu == IntPtr.Zero) return;
        try
        {
            AppendMenu(menu, 0, 1, UiStrings.T("显示主窗口"));
            AppendMenu(menu, 0x800, 0, null);
            AppendMenu(menu, 0, 2, UiStrings.T("退出应用"));
            GetCursorPos(out Point point);
            SetForegroundWindow(_hwnd);
            uint choice = TrackPopupMenu(menu, 0x0100 | 0x0002, point.X, point.Y, 0, _hwnd, IntPtr.Zero);
            PostMessage(_hwnd, 0, UIntPtr.Zero, IntPtr.Zero);
            if (choice == 1) _show();
            if (choice == 2) _exit();
        }
        finally { DestroyMenu(menu); }
    }

    public static void ActivateExisting()
    {
        EnumWindows((hwnd, _) =>
        {
            var title = new StringBuilder(512);
            GetWindowText(hwnd, title, title.Capacity);
            if (!title.ToString().StartsWith("UPCAM ", StringComparison.Ordinal)) return true;
            ShowWindow(hwnd, 9);
            SetForegroundWindow(hwnd);
            return false;
        }, IntPtr.Zero);
    }

    public void Dispose()
    {
        NotifyIconData data = Data();
        NotifyShell(2, ref data);
        RemoveWindowSubclass(_hwnd, _callback, 1);
        SendMessage(_hwnd, 0x0080, UIntPtr.Zero, IntPtr.Zero);
        SendMessage(_hwnd, 0x0080, (UIntPtr)1, IntPtr.Zero);
        if (_trayIcon != IntPtr.Zero) { DestroyIcon(_trayIcon); _trayIcon = IntPtr.Zero; }
        if (_largeWindowIcon != IntPtr.Zero) { DestroyIcon(_largeWindowIcon); _largeWindowIcon = IntPtr.Zero; }
        TrayReady = false;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyIconData
    {
        public uint Size; public IntPtr Window; public uint Id; public uint Flags; public uint Callback; public IntPtr Icon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Tip;
        public uint State; public uint StateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Info;
        public uint TimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string InfoTitle;
        public uint InfoFlags; public Guid Guid; public IntPtr BalloonIcon;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] private struct IconInfo
    {
        [MarshalAs(UnmanagedType.Bool)] public bool IsIcon;
        public uint HotspotX, HotspotY; public IntPtr Mask, Color;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Bitmap
    {
        public int Type, Width, Height, WidthBytes;
        public ushort Planes, BitsPixel; public IntPtr Bits;
    }
    private delegate IntPtr SubclassProc(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam, UIntPtr id, UIntPtr data);
    private delegate bool EnumWindowProc(IntPtr hwnd, IntPtr lparam);
    [DllImport("comctl32.dll", SetLastError = true)] private static extern bool SetWindowSubclass(IntPtr hwnd, SubclassProc proc, UIntPtr id, UIntPtr data);
    [DllImport("comctl32.dll")] private static extern bool RemoveWindowSubclass(IntPtr hwnd, SubclassProc proc, UIntPtr id);
    [DllImport("comctl32.dll")] private static extern IntPtr DefSubclassProc(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam);
    [DllImport("shell32.dll", EntryPoint = "Shell_NotifyIconW", CharSet = CharSet.Unicode)] private static extern bool ShellNotifyIcon(uint message, ref NotifyIconData data);
    [DllImport("user32.dll", EntryPoint = "LoadImageW", CharSet = CharSet.Unicode)] private static extern IntPtr LoadImage(IntPtr instance, string path, uint type, int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern bool DestroyIcon(IntPtr icon);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern int GetSystemMetricsForDpi(int index, uint dpi);
    [DllImport("user32.dll")] private static extern bool GetIconInfo(IntPtr icon, out IconInfo info);
    [DllImport("gdi32.dll", EntryPoint = "GetObjectW")] private static extern int GetObject(IntPtr handle, int size, out Bitmap bitmap);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr handle);
    [DllImport("user32.dll", EntryPoint = "SendMessageW")] private static extern IntPtr SendMessage(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern uint RegisterWindowMessage(string message);
    [DllImport("user32.dll")] private static extern IntPtr CreatePopupMenu();
    [DllImport("user32.dll", EntryPoint = "AppendMenuW", CharSet = CharSet.Unicode)] private static extern bool AppendMenu(IntPtr menu, uint flags, UIntPtr id, string? label);
    [DllImport("user32.dll")] private static extern uint TrackPopupMenu(IntPtr menu, uint flags, int x, int y, int reserved, IntPtr hwnd, IntPtr rect);
    [DllImport("user32.dll")] private static extern bool DestroyMenu(IntPtr menu);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowProc proc, IntPtr lparam);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam);
}
