using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace Workbench.Digest.Acceptance;

internal sealed record NativeWindowInfo(nint Handle, nint Owner, uint ProcessId, string ClassName, uint Style, uint ExtendedStyle, bool Visible);

internal static class NativeProbes
{
    internal const string SingletonName = @"Local\UPCAMTaskDigest.Singleton.v1";
    private const string VerifiedPopupClass = "Microsoft.UI.Content.PopupWindowSiteBridge";
    private const uint VerifiedPopupStyle = 0x94000000; // WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS, observed in actual run 42266298714.
    private const uint VerifiedPopupExtendedStyle = 0x08200000; // WS_EX_NOACTIVATE | WS_EX_NOREDIRECTIONBITMAP.
    private static readonly List<string> IgnoredPopups = [];

    internal static IReadOnlyList<string> IgnoredFrameworkPopups
    {
        get { lock (IgnoredPopups) return IgnoredPopups.ToArray(); }
    }

    internal static bool SingletonExists()
    {
        var handle = OpenMutex(0x00100000, false, SingletonName); // SYNCHRONIZE; never acquire or signal another owner's mutex.
        if (handle != nint.Zero)
        {
            CloseHandle(handle);
            return true;
        }
        var error = Marshal.GetLastWin32Error();
        if (error == 2) return false; // ERROR_FILE_NOT_FOUND
        throw new Win32Exception(error);
    }

    internal static IReadOnlyList<int> OwnedWorkers(string packageDirectory)
    {
        // Windows temp roots may be 8.3 aliases while MainModule reports the physical long path.
        // Compare canonical owned fixture directories so a live child is never mistaken for absence.
        var canonicalRoot = Directory.Exists(packageDirectory)
            ? Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(packageDirectory, mustExist: true)
            : Path.GetFullPath(packageDirectory);
        List<int> ids = [];
        foreach (var process in Process.GetProcessesByName("UPCAMPluginWorker"))
        {
            using (process)
            {
                try
                {
                    if (!process.HasExited && process.MainModule?.FileName is { } file && OwnedStore.IsWithin(canonicalRoot, file))
                        ids.Add(process.Id);
                }
                catch (InvalidOperationException) { /* The enumerated process exited before its module was inspected. */ }
                // Access failures remain failures: no-process claims must not silently ignore an unobservable worker.
            }
        }
        return ids;
    }

    internal static IReadOnlyList<NativeWindowInfo> VisibleWindows()
    {
        var windows = new List<NativeWindowInfo>();
        EnumWindows((window, _) =>
        {
            GetWindowThreadProcessId(window, out var processId);
            if (processId != Environment.ProcessId || !IsWindowVisible(window)) return true;
            var name = new StringBuilder(128);
            GetClassName(window, name, name.Capacity);
            var safeClass = new string(name.ToString().Where(character => !char.IsControl(character)).Take(127).ToArray());
            windows.Add(new(window, GetWindow(window, 4), processId, safeClass,
                unchecked((uint)GetWindowLongPtr(window, -16).ToInt64()),
                unchecked((uint)GetWindowLongPtr(window, -20).ToInt64()), true));
            return true;
        }, nint.Zero);
        return windows;
    }

    internal static bool IsVerifiedFrameworkPopup(NativeWindowInfo window, nint mainWindow, uint processId) =>
        mainWindow != nint.Zero && window.Handle != nint.Zero && window.Handle != mainWindow && window.Visible &&
        window.ProcessId == processId && window.Owner == mainWindow &&
        string.Equals(window.ClassName, VerifiedPopupClass, StringComparison.Ordinal) &&
        (window.Style & 0x80000000) != 0 && (window.Style & 0x40000000) == 0 && // popup, never child
        (window.ExtendedStyle & 0x08000000) != 0 && // must not activate independently
        window.Style == VerifiedPopupStyle && window.ExtendedStyle == VerifiedPopupExtendedStyle;

    internal static void RecordIgnoredPopup(NativeWindowInfo window, nint mainWindow)
    {
        var detail = DescribeWindow(window, mainWindow);
        lock (IgnoredPopups)
        {
            if (IgnoredPopups.Count < 16 && !IgnoredPopups.Contains(detail, StringComparer.Ordinal)) IgnoredPopups.Add(detail);
        }
    }

    internal static string VisibleWindowInventory(IEnumerable<NativeWindowInfo> windows, nint mainWindow) =>
        "Visible native window inventory (bounded, no titles/text): " + string.Join(" | ", windows.Take(16).Select(window => DescribeWindow(window, mainWindow)));

    private static string DescribeWindow(NativeWindowInfo window, nint mainWindow) =>
        $"hwnd=0x{window.Handle:X};class={window.ClassName};owner=0x{window.Owner:X};main={(window.Handle == mainWindow ? 1 : 0)};ownerIsMain={(window.Owner == mainWindow ? 1 : 0)};style=0x{window.Style:X8};exstyle=0x{window.ExtendedStyle:X8};pid={window.ProcessId}";

    internal static void VerifyWindowClassification()
    {
        nint main = 0x6012A;
        const uint pid = 7712;
        var observed = new NativeWindowInfo(0x501DE, main, pid, VerifiedPopupClass, VerifiedPopupStyle, VerifiedPopupExtendedStyle, true);
        NativeHost.Require(IsVerifiedFrameworkPopup(observed, main, pid), "The proven owned WinUI no-activate popup was not classified correctly.");
        NativeWindowInfo[] rejected =
        [
            observed with { Owner = nint.Zero }, observed with { Owner = 0x99 }, observed with { ProcessId = pid + 1 },
            observed with { ClassName = "WinUIDesktopWin32WindowClass" }, observed with { ClassName = "Microsoft.UI.Content.PopupWindowSiteBridge.Other" },
            observed with { Handle = main }, observed with { Handle = nint.Zero }, observed with { Visible = false },
            observed with { Style = VerifiedPopupStyle & ~0x80000000u }, observed with { Style = VerifiedPopupStyle | 0x40000000u },
            observed with { ExtendedStyle = VerifiedPopupExtendedStyle & ~0x08000000u },
            observed with { ExtendedStyle = VerifiedPopupExtendedStyle | 0x00000008u }
        ];
        NativeHost.Require(rejected.All(window => !IsVerifiedFrameworkPopup(window, main, pid)) &&
            !IsVerifiedFrameworkPopup(observed, nint.Zero, pid),
            "The popup classifier admitted an independent, unowned, wrong-class, activating or unexpected-style window.");
    }

    /// <summary>Only fixed aggregate queries against this run's own databases; never report row bodies or identities.</summary>
    internal static long SqlCount(string path, string sql)
    {
        OwnedStore.RejectLinks(path);
        NativeHost.Require(File.Exists(path), "An expected isolated SQLite database is absent.");
        nint database = nint.Zero;
        nint statement = nint.Zero;
        try
        {
            NativeHost.Require(SqliteOpen(path, out database, 1, nint.Zero) == 0, "The isolated SQLite database could not be opened read-only.");
            NativeHost.Require(SqlitePrepare(database, sql, -1, out statement, nint.Zero) == 0,
                "The fixed acceptance SQLite aggregate query failed.");
            NativeHost.Require(SqliteStep(statement) == 100, "The isolated SQLite aggregate did not return a row.");
            var count = SqliteColumnInt64(statement, 0);
            NativeHost.Require(SqliteStep(statement) == 101, "The isolated SQLite aggregate returned unexpected rows.");
            return count;
        }
        finally
        {
            if (statement != nint.Zero) SqliteFinalize(statement);
            if (database != nint.Zero) SqliteClose(database);
        }
    }

    private delegate bool EnumWindowsCallback(nint window, nint parameter);

    [DllImport("kernel32.dll", EntryPoint = "OpenMutexW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint OpenMutex(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, string name);
    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(nint handle);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumWindows(EnumWindowsCallback callback, nint parameter);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(nint window, out uint processId);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowVisible(nint window);
    [DllImport("user32.dll", EntryPoint = "GetClassNameW", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(nint window, StringBuilder className, int count);
    [DllImport("user32.dll")]
    private static extern nint GetWindow(nint window, uint command);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern nint GetWindowLongPtr(nint window, int index);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_open_v2", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SqliteOpen([MarshalAs(UnmanagedType.LPUTF8Str)] string path, out nint database, int flags, nint vfs);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_prepare_v2", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SqlitePrepare(nint database, [MarshalAs(UnmanagedType.LPUTF8Str)] string sql, int length, out nint statement, nint tail);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_step", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SqliteStep(nint statement);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_int64", CallingConvention = CallingConvention.Cdecl)]
    private static extern long SqliteColumnInt64(nint statement, int column);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_finalize", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SqliteFinalize(nint statement);
    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_close", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SqliteClose(nint database);
}
