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
    private readonly uint _taskbarCreated = RegisterWindowMessage("TaskbarCreated");
    private const uint TrayMessage = 0x8000 + 37;
    public bool TrayReady { get; private set; }

    public NativeDesktop(Window window, Action show, Action exit, Func<bool> closeRequested)
    {
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        _show = show;
        _exit = exit;
        _closeRequested = closeRequested;
        _callback = WindowProcedure;
        if (!SetWindowSubclass(_hwnd, _callback, 1, 0))
            throw new InvalidOperationException("无法初始化窗口消息处理。");
        AddIcon();
    }

    private NotifyIconData Data() => new()
    {
        Size = (uint)Marshal.SizeOf<NotifyIconData>(), Window = _hwnd, Id = 1,
        Flags = 1 | 2 | 4, Callback = TrayMessage, Icon = LoadIcon(IntPtr.Zero, (IntPtr)32516),
        Tip = "UPCAM 站会日报", Info = "", InfoTitle = ""
    };

    private void AddIcon()
    {
        NotifyIconData data = Data();
        TrayReady = ShellNotifyIcon(0, ref data);
    }

    private IntPtr WindowProcedure(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam, UIntPtr id, UIntPtr data)
    {
        if ((message == 0x0010 || message == 0x0112 && (wparam.ToUInt64() & 0xFFF0) == 0xF060) && _closeRequested())
            return IntPtr.Zero;
        if (message == _taskbarCreated) AddIcon();
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

    private void ShowMenu()
    {
        IntPtr menu = CreatePopupMenu();
        if (menu == IntPtr.Zero) return;
        try
        {
            AppendMenu(menu, 0, 1, "显示主窗口");
            AppendMenu(menu, 0x800, 0, null);
            AppendMenu(menu, 0, 2, "退出应用");
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
            if (!title.ToString().StartsWith("UPCAM 站会日报", StringComparison.Ordinal)) return true;
            ShowWindow(hwnd, 9);
            SetForegroundWindow(hwnd);
            return false;
        }, IntPtr.Zero);
    }

    public void Dispose()
    {
        NotifyIconData data = Data();
        ShellNotifyIcon(2, ref data);
        RemoveWindowSubclass(_hwnd, _callback, 1);
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
    private delegate IntPtr SubclassProc(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam, UIntPtr id, UIntPtr data);
    private delegate bool EnumWindowProc(IntPtr hwnd, IntPtr lparam);
    [DllImport("comctl32.dll", SetLastError = true)] private static extern bool SetWindowSubclass(IntPtr hwnd, SubclassProc proc, UIntPtr id, UIntPtr data);
    [DllImport("comctl32.dll")] private static extern bool RemoveWindowSubclass(IntPtr hwnd, SubclassProc proc, UIntPtr id);
    [DllImport("comctl32.dll")] private static extern IntPtr DefSubclassProc(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam);
    [DllImport("shell32.dll", EntryPoint = "Shell_NotifyIconW", CharSet = CharSet.Unicode)] private static extern bool ShellNotifyIcon(uint message, ref NotifyIconData data);
    [DllImport("user32.dll", EntryPoint = "LoadIconW")] private static extern IntPtr LoadIcon(IntPtr instance, IntPtr icon);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern uint RegisterWindowMessage(string message);
    [DllImport("user32.dll")] private static extern IntPtr CreatePopupMenu();
    [DllImport("user32.dll", EntryPoint = "AppendMenuW", CharSet = CharSet.Unicode)] private static extern bool AppendMenu(IntPtr menu, uint flags, UIntPtr id, string? label);
    [DllImport("user32.dll")] private static extern uint TrackPopupMenu(IntPtr menu, uint flags, int x, int y, int reserved, IntPtr hwnd, IntPtr rect);
    [DllImport("user32.dll")] private static extern bool DestroyMenu(IntPtr menu);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowProc proc, IntPtr lparam);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr hwnd, uint message, UIntPtr wparam, IntPtr lparam);
}
