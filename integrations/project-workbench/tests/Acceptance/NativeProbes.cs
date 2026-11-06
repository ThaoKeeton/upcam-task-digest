using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace Workbench.Digest.Acceptance;

internal static class NativeProbes
{
    internal const string SingletonName = @"Local\UPCAMTaskDigest.Singleton.v1";

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

    internal static int VisibleWindowCount()
    {
        var count = 0;
        EnumWindows((window, _) =>
        {
            GetWindowThreadProcessId(window, out var processId);
            if (processId == Environment.ProcessId && IsWindowVisible(window)) count++;
            return true;
        }, nint.Zero);
        return count;
    }

    internal static string VisibleWindowInventory(nint mainWindow)
    {
        var windows = new List<string>();
        EnumWindows((window, _) =>
        {
            GetWindowThreadProcessId(window, out var processId);
            if (processId != Environment.ProcessId || !IsWindowVisible(window)) return true;
            if (windows.Count >= 16) return true;
            var name = new StringBuilder(128);
            GetClassName(window, name, name.Capacity);
            var safeClass = new string(name.ToString().Where(character => !char.IsControl(character)).Take(127).ToArray());
            var owner = GetWindow(window, 4); // GW_OWNER. No window title or UI content is read.
            windows.Add($"hwnd=0x{window:X};class={safeClass};owner=0x{owner:X};main={(window == mainWindow ? 1 : 0)};ownerIsMain={(owner == mainWindow ? 1 : 0)};style=0x{GetWindowLongPtr(window, -16):X};exstyle=0x{GetWindowLongPtr(window, -20):X};pid={processId}");
            return true;
        }, nint.Zero);
        return "Visible native window inventory (bounded, no titles/text): " + string.Join(" | ", windows);
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
