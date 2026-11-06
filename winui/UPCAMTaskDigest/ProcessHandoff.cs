// Adapted process-handle ordering from Microsoft PowerToys (MIT).
// Copyright (c) Microsoft Corporation. All rights reserved.
// Source attribution and differences: THIRD_PARTY_NOTICES.md / docs/POWERTOYS_ADAPTATION.md.
using Microsoft.Win32.SafeHandles;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;

namespace Upcam.Desktop;

internal static class ProcessHandoff
{
    private const string EventPrefix = @"Local\UPCAMTaskDigest.Handoff.";
    private sealed class ParentExitHandle : WaitHandle
    {
        internal ParentExitHandle(IntPtr handle) => SafeWaitHandle = new SafeWaitHandle(handle, true);
    }

    internal static async Task<bool> WaitForParentAsync(int parentPid, string? eventToken)
    {
        EventWaitHandle? ready = null;
        try
        {
            if (eventToken is not null) ready = EventWaitHandle.OpenExisting(EventPrefix + eventToken);
            // Acquire SYNCHRONIZE only, before acknowledging that the caller may exit.
            IntPtr handle = OpenProcess(0x00100000, false, parentPid);
            if (handle == IntPtr.Zero)
            {
                if (Marshal.GetLastWin32Error() != 87) return false;
                ready?.Set();
                return true;
            }
            using var parent = new ParentExitHandle(handle);
            ready?.Set();
            return await Task.Run(() => parent.WaitOne(TimeSpan.FromSeconds(30)));
        }
        catch (Exception ex) when (ex is WaitHandleCannotBeOpenedException or UnauthorizedAccessException or IOException)
        { return false; }
        finally { ready?.Dispose(); }
    }

    internal static async Task<Process> StartAsync(string executable, bool afterUpdate, bool background = false)
    {
        string token = Guid.NewGuid().ToString("N");
        using var ready = new EventWaitHandle(false, EventResetMode.ManualReset, EventPrefix + token, out bool created);
        if (!created) throw new InvalidOperationException(UiStrings.T("无法建立应用重启交接；当前版本仍在运行。"));
        var start = new ProcessStartInfo(executable) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(executable)! };
        start.ArgumentList.Add("--wait-for-parent");
        start.ArgumentList.Add(Environment.ProcessId.ToString(CultureInfo.InvariantCulture));
        start.ArgumentList.Add("--handoff-event");
        start.ArgumentList.Add(token);
        if (afterUpdate) start.ArgumentList.Add("--after-update");
        if (background) start.ArgumentList.Add("--background");
        Process process = Process.Start(start) ?? throw new InvalidOperationException(UiStrings.T("无法启动已校验更新，当前版本已保留。"));
        if (await Task.Run(() => ready.WaitOne(TimeSpan.FromSeconds(15)))) return process;
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        process.Dispose();
        throw new InvalidOperationException(UiStrings.T("新进程未完成启动交接；当前应用和旧版本均已保留。"));
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);
}
