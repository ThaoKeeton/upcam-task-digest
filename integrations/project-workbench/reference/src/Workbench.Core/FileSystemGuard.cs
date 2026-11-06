using System.Runtime.InteropServices;

namespace Workbench.Core;

internal static class FileSystemGuard
{
    internal static void RejectLinks(string path)
    {
        for (var current = Path.GetFullPath(path); current is not null; current = Path.GetDirectoryName(current))
        {
            try
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException($"Symbolic links and reparse points are not allowed: '{current}'.");
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
        }
    }

    internal static string ContainedPath(string root, string relative)
    {
        var fullPath = Path.GetFullPath(Path.Combine(root, relative));
        var fromRoot = Path.GetRelativePath(root, fullPath);
        if (Path.IsPathRooted(fromRoot) || fromRoot == ".." || fromRoot.StartsWith($"..{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            throw new IOException($"Path '{relative}' escapes '{root}'.");
        RejectLinks(fullPath);
        return fullPath;
    }

    internal static void EnsureRegularFile(string path)
    {
        RejectLinks(path);
        if (OperatingSystem.IsWindows())
        {
            if ((File.GetAttributes(path) & (FileAttributes.Directory | FileAttributes.Device)) != 0)
                throw new IOException($"Only regular files are allowed: '{path}'.");
            return;
        }
        ushort mode;
        if (OperatingSystem.IsLinux())
        {
            // statx has a fixed ABI across Linux architectures. FileAttributes on
            // Unix omits the file type and would allow a FIFO to block scanning.
            if (Statx(-100, path, 0x100, 1, out var status) != 0)
                throw new IOException($"Cannot inspect '{path}' (errno {Marshal.GetLastPInvokeError()}).");
            mode = status.Mode;
        }
        else if (OperatingSystem.IsMacOS())
        {
            if (DarwinLStat(path, out var status) != 0)
                throw new IOException($"Cannot inspect '{path}' (errno {Marshal.GetLastPInvokeError()}).");
            mode = status.Mode;
        }
        else
        {
            throw new IOException("Package file validation currently supports Windows, Linux and macOS.");
        }
        if ((mode & 0xF000) != 0x8000)
            throw new IOException($"Only regular files are allowed: '{path}'.");
    }

    [StructLayout(LayoutKind.Explicit, Size = 256)]
    private struct LinuxStatx
    {
        [FieldOffset(28)] public ushort Mode;
    }

    [StructLayout(LayoutKind.Explicit, Size = 256)]
    private struct DarwinStat
    {
        [FieldOffset(4)] public ushort Mode;
    }

    [DllImport("libc", EntryPoint = "statx", SetLastError = true)]
    private static extern int Statx(int directory, string path, int flags, uint mask, out LinuxStatx status);

    [DllImport("libc", EntryPoint = "lstat", SetLastError = true)]
    private static extern int DarwinLStat(string path, out DarwinStat status);
}
