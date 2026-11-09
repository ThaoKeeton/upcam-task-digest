#requires -Version 5.1
<#
.SYNOPSIS
Pairs the exact Workbench 0.4.0 shipping host with UPCAM production 0.1.1.
.DESCRIPTION
Only a fresh GitHub-hosted Windows runner with an interactive desktop is allowed.
The host's normal user directory must not exist. This script never removes that
directory, reads an existing profile, or supplies service credentials. The only
diagnostic call has empty project/tenant and is required to reject before collection. Use Windows PowerShell 5.1 with -STA for built-in WPF UIA.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$HostExe,
    [Parameter(Mandatory = $true)][string]$ReportPath,
    [string]$ScreenshotPath,
    [Parameter(Mandatory = $true)][string]$PackageDirectory,
    [Parameter(Mandatory = $true)][string]$InstallerExe,
    [Parameter(Mandatory = $true)][string]$InstallReportPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:phase = 'preflight'
$script:failureCode = 'preflight-failed'
$script:owned = $null
$script:window = $null
$script:reportFile = $null
$script:canWriteReport = $false
$script:screenshotFile = $null
$script:userRoot = $null
$script:forcedCleanup = $false
$script:selectorProbe = $null
$script:checks = [ordered]@{
    ephemeralWindowsCi = $null
    freshNormalUserRoot = $null
    productionInstalledPackage = $null
    shippingPackageStoreInstall = $null
    exactShippingHostBytes = $null
    diagnosticPrerequisiteRejected = $null
    diagnosticCompletedWithinTenSeconds = $null
    cancelDisabledAfterRejection = $null
    disableStopsOwnedWorker = $null
    reenableAndReconnect = $null
    singletonInitiallyAbsent = $null
    normalHostWindow = $null
    initialDisabledUntrusted = $null
    explicitTrustDialog = $null
    enabledByNativeUi = $null
    enableDoesNotStartWorker = $null
    embeddedDigestPage = $null
    productionLocalProfileReady = $null
    namedOutputsPresent = $null
    noGeneratedReport = $null
    emptyHistory = $null
    emptyProjectAndTenant = $null
    savedAutomaticSendingOff = $null
    automaticSendingToggleOff = $null
    ownedWorkerObserved = $null
    singletonWhileActive = $null
    oneOwnedTopLevelWindow = $null
    windowPatternCloseRequested = $null
    hostExitedNormally = $null
    noOwnedWorkerOrProcessRemains = $null
    singletonReleased = $null
    hostLeaseReleased = $null
}
$script:result = [ordered]@{
    schemaVersion = 1
    test = 'shipping-0.4.0-production-0.1.1-pairing'
    hostSourceCommit = '655d7b6496067daad7c5e4cefb1f1da1b8379536'
    pluginVersion = '0.1.1'
    backendVersion = '0.4.4'
    syntheticAcceptance = $false
    activeDiagnosticCancellation = 'not-executed; unconfigured production rejects before collection'
    installMethod = 'shipping Core PackageStore; native folder-picker not exercised'
    hostMode = 'normal'
    pluginId = 'com.upcam.task-digest'
    succeeded = $false
    status = 'running'
    phase = 'preflight'
    failureCode = $null
    cleanup = 'not-started'
    screenshot = 'not-requested'
    selectorDiagnostic = $null
    failureClassification = $null
    hashCmdletProbe = $null
    shippingHostHashes = $null
    serviceOperationsInvoked = $false
    localPrerequisiteDiagnosticInvoked = $false
    serviceCredentialEnvironmentInherited = $false
    checks = $script:checks
}
if ($ScreenshotPath) { $script:result.screenshot = 'pending' }

function Set-Phase([string]$Name, [string]$Failure) {
    $script:phase = $Name
    $script:failureCode = $Failure
}

function Assert-Check([string]$Name, [bool]$Condition) {
    $script:checks[$Name] = $Condition
    if (-not $Condition) { throw 'A fixed native UI check failed.' }
}

# Do not depend on cross-edition PowerShell module auto-loading for byte identity.
# This .NET Framework-compatible implementation performs the same SHA256 check.
function Read-Sha256([string]$Path) {
    $stream = $null
    $algorithm = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        $algorithm = [Security.Cryptography.SHA256]::Create()
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $algorithm) { $algorithm.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-SafeFailure($Record) {
    # No exception message, target value, path, source line text or environment.
    $kind = 'other'
    $errorType = $Record.Exception.GetType().FullName
    if ($errorType -ceq 'System.Management.Automation.CommandNotFoundException') { $kind = 'command-unavailable' }
    elseif ($errorType -match '^System\.Management\.Automation\.ParameterBinding') { $kind = 'parameter-binding' }
    elseif ($errorType -ceq 'System.IO.FileNotFoundException') { $kind = 'missing-file' }
    elseif ($errorType -ceq 'System.IO.DirectoryNotFoundException') { $kind = 'missing-directory' }
    elseif ($errorType -ceq 'System.UnauthorizedAccessException') { $kind = 'access-denied' }
    elseif ($errorType -ceq 'System.Management.Automation.PropertyNotFoundException') { $kind = 'property-unavailable' }
    elseif ($errorType -ceq 'System.Management.Automation.RuntimeException') { $kind = 'runtime-evaluation' }
    $command = 'other'
    if ($null -ne $Record.InvocationInfo -and $null -ne $Record.InvocationInfo.MyCommand) {
        foreach ($allowed in @('Get-FileHash', 'Join-Path', 'Split-Path', 'Read-Sha256', 'Assert-Check')) {
            if ($Record.InvocationInfo.MyCommand.Name -ceq $allowed) { $command = $allowed }
        }
    }
    $line = if ($null -ne $Record.InvocationInfo) { [int]$Record.InvocationInfo.ScriptLineNumber } else { 0 }
    return [ordered]@{ kind = $kind; command = $command; line = $line }
}

function Wait-For([scriptblock]$Probe, [int]$Seconds = 30) {
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    while ($deadline.Elapsed.TotalSeconds -lt $Seconds) {
        if ($null -ne $script:owned -and $script:owned.HostExited) {
            throw 'The owned host exited before the expected UI state.'
        }
        try {
            $value = & $Probe
            if ($value) { return $value }
        }
        catch [System.Windows.Automation.ElementNotAvailableException] { }
        catch [System.Windows.Automation.ElementNotEnabledException] { }
        Start-Sleep -Milliseconds 200
    }
    throw 'The expected native UI state did not become available before its deadline.'
}

function Set-SelectorIssue([ValidateSet('ambiguous', 'missing', 'pattern', 'disabled')][string]$Category) {
    if ($null -ne $script:selectorProbe) { $script:selectorProbe.category = $Category }
}

function Find-Exact([string]$Name, [string]$Id = '', [string]$Role = '', [string]$Key = '') {
    # Keys come from fixed call-site labels or the fixed phase/role, never the UI name.
    $selectorKey = if ($Key) { $Key } elseif ($Role) { $script:phase + '-' + $Role } else { $script:phase + '-lookup' }
    if ($selectorKey -cnotmatch '^[a-z][a-z0-9-]{0,79}$') { $selectorKey = 'selector' }
    $script:selectorProbe = [ordered]@{
        key = $selectorKey
        category = 'missing'
        matchCount = 0
        idMatchCount = 0
        eligibleCount = 0
        candidates = @()
    }
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
    $matches = $script:window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $found = $null
    foreach ($item in $matches) {
        if ($item.Current.ProcessId -ne $script:owned.HostId) { continue }
        $script:selectorProbe.matchCount++
        $automationId = $item.Current.AutomationId
        if ($script:selectorProbe.candidates.Count -lt 8) {
            # Only a bounded identifier alphabet is allowed; no names, text, paths, or values.
            $safeId = if ($automationId -cmatch '^[A-Za-z0-9_.-]{0,80}$') { $automationId } else { 'redacted' }
            $controlType = $item.Current.ControlType.ProgrammaticName
            if ($controlType -cnotmatch '^ControlType\.[A-Za-z]+$') { $controlType = 'ControlType.Unknown' }
            $script:selectorProbe.candidates += [ordered]@{ controlType = $controlType; automationId = $safeId }
        }
        if ($Id -and $automationId -cne $Id) { continue }
        $script:selectorProbe.idMatchCount++
        $pattern = $null
        if ($Role -eq 'select') {
            Set-SelectorIssue 'pattern'
            if (-not $item.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern) -and
                -not $item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { continue }
        }
        elseif ($Role -eq 'invoke') {
            Set-SelectorIssue 'pattern'
            if (-not $item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { continue }
        }
        elseif ($Role -eq 'dialog') {
            # Actual WinUI exposes both a Window peer and its Text title with this name.
            # Require the observed dialog role instead of accepting the first same-name element.
            if ($item.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window) { continue }
        }
        $script:selectorProbe.eligibleCount++
        $found = $item
    }
    if ($script:selectorProbe.eligibleCount -gt 1) {
        Set-SelectorIssue 'ambiguous'
        throw 'The exact native UI selector was ambiguous.'
    }
    if ($null -ne $found) { $script:selectorProbe.category = 'matched' }
    elseif ($script:selectorProbe.idMatchCount -eq 0) { Set-SelectorIssue 'missing' }
    return $found
}

function Get-Exact([string]$Name, [string]$Id = '', [string]$Role = '', [string]$Key = '') {
    return Wait-For { Find-Exact $Name $Id $Role $Key }
}

function Select-Exact([string]$Name, [string]$Id = '') {
    $item = Get-Exact $Name $Id 'select'
    if (-not $item.Current.IsEnabled) { Set-SelectorIssue 'disabled'; throw 'The named navigation control was disabled.' }
    $pattern = $null
    if ($item.Current.IsOffscreen -and $item.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$pattern)) {
        $pattern.ScrollIntoView()
    }
    $pattern = $null
    try {
        if ($item.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
            $pattern.Select()
        }
        elseif ($item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
            $pattern.Invoke()
        }
        else { Set-SelectorIssue 'pattern'; throw 'The native navigation control exposed no supported UIA pattern.' }
    }
    catch [System.Windows.Automation.ElementNotEnabledException] { Set-SelectorIssue 'disabled'; throw }
    catch [System.Windows.Automation.ElementNotAvailableException] { Set-SelectorIssue 'missing'; throw }
    catch { Set-SelectorIssue 'pattern'; throw }
}

function Invoke-Exact([string]$Name, [string]$Key = '') {
    $item = Get-Exact $Name '' 'invoke' $Key
    if (-not $item.Current.IsEnabled) { Set-SelectorIssue 'disabled'; throw 'The named host button was disabled.' }
    $pattern = $null
    if ($item.Current.IsOffscreen -and $item.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$pattern)) {
        $pattern.ScrollIntoView()
    }
    $pattern = $null
    if (-not $item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        Set-SelectorIssue 'pattern'
        throw 'The named host button exposed no invoke pattern.'
    }
    try { $pattern.Invoke() }
    catch [System.Windows.Automation.ElementNotEnabledException] { Set-SelectorIssue 'disabled'; throw }
    catch [System.Windows.Automation.ElementNotAvailableException] { Set-SelectorIssue 'missing'; throw }
    catch { Set-SelectorIssue 'pattern'; throw }
}

function Read-NamedText([string]$Name, [string]$Id) {
    $item = Get-Exact $Name $Id
    $pattern = $null
    if ($item.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
        return $pattern.DocumentRange.GetText(256).TrimEnd([char[]]"`r`n")
    }
    if ($item.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        return $pattern.Current.Value
    }
    Set-SelectorIssue 'pattern'
    throw 'The named output exposed no readable UIA pattern.'
}

function Get-ValuePattern([string]$Name, [string]$Id) {
    $item = Get-Exact $Name $Id
    $pattern = $null
    if (-not $item.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        Set-SelectorIssue 'pattern'
        throw 'The named editor exposed no value pattern.'
    }
    return $pattern
}

function Wait-ForExit([int]$Seconds) {
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    while ($deadline.Elapsed.TotalSeconds -lt $Seconds) {
        if ($script:owned.HostExited) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Wait-ForOwnedCleanup([int]$Seconds) {
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    while ($deadline.Elapsed.TotalSeconds -lt $Seconds) {
        if ($script:owned.LiveProcessCount -eq 0 -and -not $script:owned.TrackedWorkerAlive) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows is required.' }
    if ($PSVersionTable.PSEdition -ne 'Desktop' -or [Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
        throw 'Run the driver with Windows PowerShell 5.1 and -STA.'
    }
    Assert-Check 'ephemeralWindowsCi' ($env:CI -ceq 'true' -and $env:GITHUB_ACTIONS -ceq 'true' -and
        $env:RUNNER_ENVIRONMENT -ceq 'github-hosted' -and [Environment]::UserInteractive)
    $script:userRoot = [IO.Path]::Combine([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData), 'ProjectWorkbench')
    $hostFile = [IO.Path]::GetFullPath($HostExe)
    $script:reportFile = [IO.Path]::GetFullPath($ReportPath)
    if ($ScreenshotPath) { $script:screenshotFile = [IO.Path]::GetFullPath($ScreenshotPath) }
    $rootPrefix = $script:userRoot.TrimEnd('\') + '\'
    foreach ($destination in @($script:reportFile, $script:screenshotFile)) {
        if ($destination -and ($destination.Equals($script:userRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $destination.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase))) {
            $script:reportFile = $null
            throw 'Evidence must be outside the normal user root.'
        }
    }

    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing
    Add-Type -ReferencedAssemblies System.dll, System.Core.dll, System.Drawing.dll -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

// Only the host created suspended here and descendants contained in its job are inspected.
public sealed class UpcamRealHostRun : IDisposable
{
    private sealed class Handle : SafeHandleZeroOrMinusOneIsInvalid
    {
        public Handle() : base(true) { }
        public Handle(IntPtr value) : base(true) { SetHandle(value); }
        protected override bool ReleaseHandle() { return CloseHandle(handle); }
    }
    private Handle job;
    private Handle host;
    private readonly Dictionary<int, Handle> workers = new Dictionary<int, Handle>();
    public int HostId { get; private set; }
    private const uint WaitObject = 0, WaitTimeout = 258;
    public bool HostExited { get { return Exited(host); } }
    public int HostExitCode
    {
        get { uint code; if (!HostExited || !GetExitCodeProcess(host, out code)) throw new InvalidOperationException(); return unchecked((int)code); }
    }
    public int LiveProcessCount { get { return JobIds().Length; } }
    public bool TrackedWorkerAlive
    {
        get { foreach (var item in workers.Values) if (!Exited(item)) return true; return false; }
    }
    private static bool Exited(Handle process)
    {
        if (process == null || process.IsInvalid) throw new InvalidOperationException();
        uint state = WaitForSingleObject(process, 0);
        if (state != WaitObject && state != WaitTimeout) throw new InvalidOperationException();
        return state == WaitObject;
    }
    public static bool PathExists(string path)
    {
        uint attributes = GetFileAttributesW(path);
        if (attributes != 0xffffffff) return true;
        int error = Marshal.GetLastWin32Error();
        if (error == 2 || error == 3) return false;
        throw new InvalidOperationException();
    }
    public static void RejectLinks(string path)
    {
        for (string part = Path.GetFullPath(path); part != null; part = Path.GetDirectoryName(part))
        {
            uint attributes = GetFileAttributesW(part);
            if (attributes != 0xffffffff) { if ((attributes & 0x400) != 0) throw new InvalidOperationException(); }
            else { int error = Marshal.GetLastWin32Error(); if (error != 2 && error != 3) throw new InvalidOperationException(); }
        }
    }
    public static bool SingletonExists()
    {
        using (Handle handle = OpenMutexW(0x00100000, false, @"Local\UPCAMTaskDigest.Singleton.v1"))
        {
            if (!handle.IsInvalid) return true;
            if (Marshal.GetLastWin32Error() == 2) return false;
            throw new InvalidOperationException();
        }
    }
    private static IntPtr CleanEnvironment()
    {
        // An allowlist of OS profile/runtime variables excludes all business credentials.
        string[] names = { "SystemRoot", "WINDIR", "SystemDrive", "ComSpec", "TEMP", "TMP", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
            "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "OS",
            "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "SESSIONNAME" };
        var values = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string name in names)
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (!String.IsNullOrEmpty(value)) values[name] = value;
        }
        string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        values["PATH"] = Path.Combine(windows, "System32") + ";" + windows;
        var block = new StringBuilder();
        foreach (var pair in values) block.Append(pair.Key).Append('=').Append(pair.Value).Append('\0');
        block.Append('\0');
        return Marshal.StringToHGlobalUni(block.ToString());
    }
    public static UpcamRealHostRun Start(string executable)
    {
        var run = new UpcamRealHostRun();
        Handle thread = null;
        IntPtr environment = IntPtr.Zero;
        try
        {
            run.job = CreateJobObjectW(IntPtr.Zero, null);
            if (run.job.IsInvalid) throw new InvalidOperationException();
            var limits = new ExtendedLimits();
            limits.Basic.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE: fail-safe for this owned process tree only.
            if (!SetInformationJobObject(run.job, 9, ref limits, Marshal.SizeOf(typeof(ExtendedLimits)))) throw new InvalidOperationException();
            var startup = new StartupInfo(); startup.Size = Marshal.SizeOf(typeof(StartupInfo));
            ProcessInfo created;
            environment = CleanEnvironment();
            // No CLI switches: this is the platform's normal application and normal user root.
            if (!CreateProcessW(executable, new StringBuilder("\"" + executable + "\""), IntPtr.Zero, IntPtr.Zero, false,
                0x00000004 | 0x00000400, environment, Path.GetDirectoryName(executable), ref startup, out created)) throw new InvalidOperationException();
            run.host = new Handle(created.Process);
            thread = new Handle(created.Thread);
            run.HostId = unchecked((int)created.ProcessId);
            // The owned main thread is still suspended: no host or worker code can run before containment.
            if (!AssignProcessToJobObject(run.job, run.host)) throw new InvalidOperationException();
            if (ResumeThread(thread) == 0xffffffff) throw new InvalidOperationException();
            return run;
        }
        catch
        {
            if (run.host != null && !run.host.IsInvalid) { TerminateProcess(run.host, 1); WaitForSingleObject(run.host, 5000); }
            run.Dispose();
            throw;
        }
        finally { if (thread != null) thread.Dispose(); if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment); }
    }
    private int[] JobIds()
    {
        int bytes = 8 + IntPtr.Size * 32;
        for (int attempt = 0; attempt < 4; attempt++, bytes *= 4)
        {
            IntPtr buffer = Marshal.AllocHGlobal(bytes);
            try
            {
                int returned;
                if (!QueryInformationJobObject(job, 3, buffer, bytes, out returned))
                {
                    if (Marshal.GetLastWin32Error() == 234) continue;
                    throw new InvalidOperationException();
                }
                int count = Marshal.ReadInt32(buffer, 4);
                if (count < 0 || count > (bytes - 8) / IntPtr.Size) throw new InvalidOperationException();
                var ids = new int[count];
                for (int i = 0; i < count; i++) ids[i] = checked((int)Marshal.ReadIntPtr(buffer, 8 + i * IntPtr.Size).ToInt64());
                return ids;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        throw new InvalidOperationException();
    }
    public int ObserveWorkers(string expectedExecutable)
    {
        int count = 0;
        foreach (int id in JobIds())
        {
            if (id == HostId) continue;
            Handle process = OpenProcess(0x1000 | 0x00100000, false, id);
            try
            {
                if (process.IsInvalid) { if (Marshal.GetLastWin32Error() == 87) continue; throw new InvalidOperationException(); }
                bool belongs;
                if (!IsProcessInJob(process, job, out belongs)) throw new InvalidOperationException();
                if (!belongs || Exited(process)) continue; // A recycled PID is never inspected or terminated.
                var path = new StringBuilder(32768); int size = path.Capacity;
                if (!QueryFullProcessImageNameW(process, 0, path, ref size))
                {
                    if (Exited(process)) continue;
                    throw new InvalidOperationException();
                }
                if (!String.Equals(Path.GetFullPath(path.ToString()), expectedExecutable, StringComparison.OrdinalIgnoreCase)) continue;
                count++;
                Handle previous;
                if (workers.TryGetValue(id, out previous)) previous.Dispose();
                workers[id] = process; process = null;
            }
            finally { if (process != null) process.Dispose(); }
        }
        return count;
    }
    public IntPtr FindHostWindow()
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr window, IntPtr unused)
        {
            uint id; GetWindowThreadProcessId(window, out id);
            if (id == (uint)HostId && IsWindowVisible(window))
            {
                var text = new StringBuilder(256); GetWindowTextW(window, text, text.Capacity);
                if (text.ToString() == "Project Workbench · 原生插件工作台") found = window;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }
    public int VisibleOwnedWindows()
    {
        var ids = new HashSet<int>(JobIds()); int count = 0;
        if (!EnumWindows(delegate(IntPtr window, IntPtr unused)
        {
            uint id; GetWindowThreadProcessId(window, out id);
            if (ids.Contains(unchecked((int)id)) && IsWindowVisible(window)) count++;
            return true;
        }, IntPtr.Zero)) throw new InvalidOperationException();
        return count;
    }
    public bool Capture(IntPtr window, string destination)
    {
        uint id; GetWindowThreadProcessId(window, out id);
        if (id != (uint)HostId) throw new InvalidOperationException();
        Rect rect; if (!GetWindowRect(window, out rect)) return false;
        int width = rect.Right - rect.Left, height = rect.Bottom - rect.Top;
        if (width < 100 || height < 100 || width > 8000 || height > 8000) return false;
        using (var bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb))
        {
            using (var graphics = Graphics.FromImage(bitmap))
            {
                IntPtr dc = graphics.GetHdc();
                try { if (!PrintWindow(window, dc, 2)) return false; }
                finally { graphics.ReleaseHdc(dc); }
            }
            // Reject blank DirectComposition captures. Never substitute a rendered or desktop image.
            int baseline = bitmap.GetPixel(width / 2, height / 2).ToArgb(), different = 0;
            for (int y = height / 5; y < height * 9 / 10; y += 4)
                for (int x = width / 3; x < width * 9 / 10; x += 4)
                    if (bitmap.GetPixel(x, y).ToArgb() != baseline) different++;
            if (different < 32) return false;
            using (var file = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None)) bitmap.Save(file, ImageFormat.Png);
            return true;
        }
    }
    public void TerminateOwned() { if (!TerminateJobObject(job, 1)) throw new InvalidOperationException(); }
    public void Dispose()
    {
        foreach (var process in workers.Values) process.Dispose(); workers.Clear();
        if (host != null) { host.Dispose(); host = null; }
        if (job != null) { job.Dispose(); job = null; }
    }

    [StructLayout(LayoutKind.Sequential)] private struct BasicLimits { public long ProcessTime, JobTime; public uint LimitFlags; public UIntPtr Minimum, Maximum; public uint Active; public UIntPtr Affinity; public uint Priority, Scheduling; }
    [StructLayout(LayoutKind.Sequential)] private struct IoCounters { public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits { public BasicLimits Basic; public IoCounters Io; public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct StartupInfo { public int Size; public string Reserved, Desktop, Title; public uint X, Y, Width, Height, XCount, YCount, Fill, Flags; public ushort Show, ReservedSize; public IntPtr ReservedPointer, Input, Output, Error; }
    [StructLayout(LayoutKind.Sequential)] private struct ProcessInfo { public IntPtr Process, Thread; public uint ProcessId, ThreadId; }
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    private delegate bool WindowCallback(IntPtr window, IntPtr unused);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern uint GetFileAttributesW(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern Handle OpenMutexW(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern Handle CreateJobObjectW(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(Handle job, int kind, ref ExtendedLimits value, int length);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool CreateProcessW(string executable, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo created);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(Handle job, Handle process);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint ResumeThread(Handle thread);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint WaitForSingleObject(Handle handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetExitCodeProcess(Handle process, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool QueryInformationJobObject(Handle job, int kind, IntPtr buffer, int length, out int returned);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern Handle OpenProcess(uint access, bool inherit, int processId);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool IsProcessInJob(Handle process, Handle job, out bool belongs);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool QueryFullProcessImageNameW(Handle process, uint flags, StringBuilder path, ref int size);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateJobObject(Handle job, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateProcess(Handle process, uint code);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool EnumWindows(WindowCallback callback, IntPtr unused);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr window, StringBuilder text, int length);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr window, IntPtr dc, uint flags);
}
'@

    # PathExists distinguishes absence from access failure and does not inspect directory contents.
    Set-Phase 'fresh-root' 'normal-user-root-not-fresh'
    Assert-Check 'freshNormalUserRoot' (-not [UpcamRealHostRun]::PathExists($script:userRoot))
    [UpcamRealHostRun]::RejectLinks($script:userRoot)
    Set-Phase 'fixture' 'production-fixture-invalid'
    foreach ($path in @($hostFile, $script:reportFile, $script:screenshotFile)) {
        if ($path) { [UpcamRealHostRun]::RejectLinks($path) }
    }
    if ([IO.Path]::GetFileName($hostFile) -cne 'Workbench.App.exe' -or -not [IO.File]::Exists($hostFile)) { throw 'The actual platform executable is missing.' }
    if ([IO.Path]::GetExtension($script:reportFile) -ine '.json' -or [UpcamRealHostRun]::PathExists($script:reportFile)) {
        $script:reportFile = $null
        throw 'A new JSON report destination is required.'
    }
    if ($script:screenshotFile -and ([IO.Path]::GetExtension($script:screenshotFile) -ine '.png' -or [UpcamRealHostRun]::PathExists($script:screenshotFile))) {
        throw 'A new PNG screenshot destination is required.'
    }
    # Grant evidence writes only after the fresh-root gate and every evidence path check.
    # In particular, an existing root reached through an 8.3 alias cannot reach this grant.
    $script:canWriteReport = $true
    Set-Phase 'shipping-hash' 'shipping-host-byte-check-failed'
    # One bounded reproduction of the old expression supplies sanitized evidence
    # for the WinPS 5.1-only harness failure. It never substitutes for SHA validation.
    try {
        $legacyMatches = (((Get-FileHash -LiteralPath $hostFile -Algorithm SHA256).Hash -eq '48598e37f84e1f0ca60b0fadaa33a540876d5b83b6db08da593d8b48e33c35ac') -and
            ((Get-FileHash -LiteralPath (Join-Path (Split-Path $hostFile -Parent) 'Workbench.App.dll') -Algorithm SHA256).Hash -eq '25aa56d72caf6b137250b7be75d4e5c5f1e49a3ed4339081f50a7e209dbfd69b'))
        $script:result.hashCmdletProbe = [ordered]@{ succeeded = $true; matches = $legacyMatches; failure = $null }
    }
    catch { $script:result.hashCmdletProbe = [ordered]@{ succeeded = $false; matches = $null; failure = (Get-SafeFailure $_) } }
    $hostDll = [IO.Path]::Combine([IO.Path]::GetDirectoryName($hostFile), 'Workbench.App.dll')
    [UpcamRealHostRun]::RejectLinks($hostDll)
    $actualExeHash = Read-Sha256 $hostFile
    $actualDllHash = Read-Sha256 $hostDll
    $script:result.shippingHostHashes = [ordered]@{ executableSha256 = $actualExeHash; dllSha256 = $actualDllHash; algorithm = 'SHA256' }
    Assert-Check 'exactShippingHostBytes' ($actualExeHash -ceq '48598e37f84e1f0ca60b0fadaa33a540876d5b83b6db08da593d8b48e33c35ac' -and
        $actualDllHash -ceq '25aa56d72caf6b137250b7be75d4e5c5f1e49a3ed4339081f50a7e209dbfd69b')
    $sourcePackage = [IO.Path]::GetFullPath($PackageDirectory)
    $installer = [IO.Path]::GetFullPath($InstallerExe)
    $installReport = [IO.Path]::GetFullPath($InstallReportPath)
    foreach ($path in @($sourcePackage, $installer, $installReport)) { [UpcamRealHostRun]::RejectLinks($path) }
    Set-Phase 'package-install' 'shipping-package-store-install-failed'
    # Installer independently requires this exact fresh CI root and checks the shipping Core hash.
    $installProcess = Start-Process -FilePath $installer -ArgumentList @(('"' + $sourcePackage + '"'), ('"' + $installReport + '"')) -PassThru
    try {
        if (-not $installProcess.WaitForExit(60000)) { $installProcess.Kill(); throw 'Shipping PackageStore install timed out.' }
        if ($installProcess.ExitCode -ne 0) { throw 'Shipping PackageStore install failed.' }
    } finally { $installProcess.Dispose() }
    $installResult = Get-Content -LiteralPath $installReport -Raw | ConvertFrom-Json
    Assert-Check 'shippingPackageStoreInstall' ($installResult.succeeded -eq $true -and $installResult.installedBytesMatch -eq $true -and
        $installResult.pluginVersion -ceq '0.1.1' -and $installResult.syntheticAcceptance -eq $false)
    $packageDirectory = [IO.Path]::Combine($script:userRoot, 'Plugins', 'com.upcam.task-digest')
    [UpcamRealHostRun]::RejectLinks($packageDirectory)
    $manifest = Get-Content -LiteralPath (Join-Path $packageDirectory 'plugin.json') -Raw | ConvertFrom-Json
    $inventory = Get-Content -LiteralPath (Join-Path $packageDirectory 'plugin-package.json') -Raw | ConvertFrom-Json
    $workerExe = [IO.Path]::Combine($packageDirectory, 'worker', 'UPCAMPluginWorker.exe')
    [UpcamRealHostRun]::RejectLinks($workerExe)
    Assert-Check 'productionInstalledPackage' ($manifest.id -ceq 'com.upcam.task-digest' -and $manifest.name -ceq 'UPCAM Task Digest' -and
        $manifest.entryAssembly -ceq 'UPCAM.Workbench.dll' -and $manifest.entryType -ceq 'Upcam.Workbench.DigestPlugin' -and
        $manifest.version -ceq '0.1.1' -and $inventory.backendVersion -ceq '0.4.4' -and
        $inventory.pluginId -ceq 'com.upcam.task-digest' -and $inventory.syntheticAcceptance -is [bool] -and
        $inventory.syntheticAcceptance -eq $false -and [IO.File]::Exists($workerExe))
    Set-Phase 'singleton-preflight' 'preexisting-upcam-session'
    Assert-Check 'singletonInitiallyAbsent' (-not [UpcamRealHostRun]::SingletonExists())
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($script:reportFile)) | Out-Null
    if ($script:screenshotFile) { [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($script:screenshotFile)) | Out-Null }

    Set-Phase 'host-start' 'normal-host-window-unavailable'
    $script:owned = [UpcamRealHostRun]::Start($hostFile)
    $script:result.cleanup = 'pending'
    $handle = Wait-For { $candidate = $script:owned.FindHostWindow(); if ($candidate -ne [IntPtr]::Zero) { $candidate } } 60
    $script:window = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
    Assert-Check 'normalHostWindow' ($script:window.Current.ProcessId -eq $script:owned.HostId -and
        $script:window.Current.Name -ceq 'Project Workbench · 原生插件工作台')

    Set-Phase 'plugin-discovery' 'initial-production-plugin-state-invalid'
    Select-Exact '插件'
    $initialStatus = "状态：Disabled · 已信任：False · 下次启动启用：False`n" + $packageDirectory
    $null = Get-Exact $initialStatus
    $null = Get-Exact '启用 UPCAM Task Digest' '' 'invoke'
    Assert-Check 'initialDisabledUntrusted' $true

    Set-Phase 'native-trust' 'explicit-native-trust-failed'
    Invoke-Exact '启用 UPCAM Task Digest' 'enable-plugin'
    $null = Get-Exact '信任并启用 UPCAM Task Digest？' '' 'dialog' 'trust-title'
    $null = Get-Exact '信任并启用' '' 'invoke' 'trust-primary'
    Assert-Check 'explicitTrustDialog' $true
    # Authorization is limited to the controlled production fixture in this fresh CI account.
    Invoke-Exact '信任并启用' 'trust-primary'
    $null = Get-Exact ("状态：Enabled · 已信任：True · 下次启动启用：True`n" + $packageDirectory)
    $null = Get-Exact '停用 UPCAM Task Digest' '' 'invoke'
    Assert-Check 'enabledByNativeUi' $true
    Assert-Check 'enableDoesNotStartWorker' ($script:owned.ObserveWorkers($workerExe) -eq 0 -and -not [UpcamRealHostRun]::SingletonExists())

    Set-Phase 'digest-page' 'native-digest-page-unavailable'
    Select-Exact 'UPCAM Task Digest'
    # Layout-only Grid nodes need not have UIA peers; the named native TabView does.
    $null = Get-Exact 'UPCAM digest tabs' 'digest-tabs'
    Assert-Check 'embeddedDigestPage' $true
    Set-Phase 'backend-ready' 'production-local-profile-not-ready'
    $null = Wait-For { (Read-NamedText 'UPCAM backend status' 'plugin-status') -ceq 'Ready · local profile' } 60
    Assert-Check 'productionLocalProfileReady' $true
    $null = Get-Exact 'UPCAM operation result' 'operation-result'
    $null = Get-Exact 'Last saved schedule check' 'last-schedule-check'
    Assert-Check 'namedOutputsPresent' $true
    $null = Get-Exact '已保存的自动发送已关闭。'
    Assert-Check 'savedAutomaticSendingOff' $true
    Assert-Check 'ownedWorkerObserved' ($script:owned.ObserveWorkers($workerExe) -gt 0)
    Assert-Check 'singletonWhileActive' ([UpcamRealHostRun]::SingletonExists())
    Assert-Check 'oneOwnedTopLevelWindow' ($script:owned.VisibleOwnedWindows() -eq 1)

    Set-Phase 'empty-local-outputs' 'fresh-local-output-state-invalid'
    Select-Exact 'Run & diagnostics' 'tab-run-diagnostics'
    $output = Get-ValuePattern 'Digest and diagnostic output' 'diagnostic-output'
    $initialOutput = $output.Current.Value
    Assert-Check 'noGeneratedReport' ($output.Current.IsReadOnly -and
        ($initialOutput.Length -eq 0 -or $initialOutput -ceq '选择时间范围后，先运行只读诊断。'))
    Set-Phase 'diagnostic-prerequisite' 'unconfigured-diagnostic-not-rejected'
    $diagnosticClock = [Diagnostics.Stopwatch]::StartNew()
    $script:result.localPrerequisiteDiagnosticInvoked = $true
    Invoke-Exact 'Read-only diagnosis' 'diagnose'
    $null = Wait-For { (Read-NamedText 'UPCAM operation result' 'operation-result') -match '^[0-9]+ · diagnose · failed · 请先保存 Tenant ID 和 Project ID。$' } 10
    Assert-Check 'diagnosticPrerequisiteRejected' $true
    Assert-Check 'diagnosticCompletedWithinTenSeconds' ($diagnosticClock.Elapsed.TotalSeconds -lt 10)
    $null = Wait-For { (Read-NamedText 'UPCAM backend status' 'plugin-status') -ceq 'Ready · local profile' } 10
    $cancel = Get-Exact 'Cancel current operation' 'cancel-operation'
    Assert-Check 'cancelDisabledAfterRejection' (-not $cancel.Current.IsEnabled)
    # The failed diagnostic cannot generate a report or history. Navigation must still work.
    $output = Get-ValuePattern 'Digest and diagnostic output' 'diagnostic-output'
    Assert-Check 'noGeneratedReport' ($output.Current.Value -ceq $initialOutput)
    Select-Exact 'History' 'tab-history'
    $history = Get-Exact 'Local digest run history' 'history-list'
    $itemCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)
    $details = Get-ValuePattern 'Selected digest run details' 'history-detail'
    Assert-Check 'emptyHistory' ($history.FindAll([System.Windows.Automation.TreeScope]::Descendants, $itemCondition).Count -eq 0 -and
        $details.Current.IsReadOnly -and $details.Current.Value.Length -eq 0)

    Set-Phase 'empty-local-settings' 'fresh-local-settings-invalid'
    Select-Exact 'UPCAM Task Digest · 设置'
    $null = Get-Exact 'UPCAM settings tabs' 'settings-tabs'
    $null = Wait-For { (Read-NamedText 'UPCAM backend status' 'plugin-status') -ceq 'Ready · local profile' }
    Select-Exact 'Project' 'tab-project'
    $project = Get-ValuePattern 'Project ID' 'project-id'
    $tenant = Get-ValuePattern 'Tenant / organization ID' 'tenant-id'
    Assert-Check 'emptyProjectAndTenant' ($project.Current.Value.Length -eq 0 -and $tenant.Current.Value.Length -eq 0)
    Select-Exact 'Schedule' 'tab-schedule'
    $automatic = Get-Exact 'Automatic digest sending' 'auto-run'
    $toggle = $null
    if (-not $automatic.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$toggle)) {
        throw 'The automatic sending switch exposed no toggle pattern.'
    }
    Assert-Check 'automaticSendingToggleOff' ($toggle.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::Off)

    Set-Phase 'single-window' 'additional-owned-top-level-window'
    Select-Exact 'UPCAM Task Digest'
    Select-Exact 'Overview' 'tab-overview'
    $null = Wait-For { (Read-NamedText 'UPCAM backend status' 'plugin-status') -ceq 'Ready · local profile' }
    $null = Get-Exact '已保存的自动发送已关闭。'
    Assert-Check 'oneOwnedTopLevelWindow' ($script:owned.VisibleOwnedWindows() -eq 1)
    if ($script:screenshotFile) {
        Set-Phase 'screenshot' 'owned-window-capture-failed'
        $script:result.screenshot = 'failed'
        if (-not $script:owned.Capture($handle, $script:screenshotFile)) { throw 'The actual window could not be captured.' }
        $script:result.screenshot = 'captured'
    }

    Set-Phase 'disable-reenable' 'production-plugin-lifecycle-failed'
    Select-Exact '插件'
    Invoke-Exact '停用 UPCAM Task Digest' 'disable-plugin'
    $null = Get-Exact ("状态：Disabled · 已信任：True · 下次启动启用：False`n" + $packageDirectory)
    $null = Wait-For { ($script:owned.ObserveWorkers($workerExe) -eq 0) -and -not $script:owned.TrackedWorkerAlive -and -not [UpcamRealHostRun]::SingletonExists() } 20
    Assert-Check 'disableStopsOwnedWorker' $true
    Invoke-Exact '启用 UPCAM Task Digest' 'reenable-plugin'
    $null = Get-Exact ("状态：Enabled · 已信任：True · 下次启动启用：True`n" + $packageDirectory)
    Select-Exact 'UPCAM Task Digest'
    $null = Wait-For { (Read-NamedText 'UPCAM backend status' 'plugin-status') -ceq 'Ready · local profile' } 60
    Assert-Check 'reenableAndReconnect' ($script:owned.ObserveWorkers($workerExe) -gt 0 -and [UpcamRealHostRun]::SingletonExists())

    Set-Phase 'normal-shutdown' 'normal-window-shutdown-failed'
    $close = $null
    if (-not $script:window.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$close)) {
        throw 'The owned host window exposed no window pattern.'
    }
    $close.Close()
    Assert-Check 'windowPatternCloseRequested' $true
    Assert-Check 'hostExitedNormally' ((Wait-ForExit 30) -and $script:owned.HostExitCode -eq 0)
    Assert-Check 'noOwnedWorkerOrProcessRemains' (Wait-ForOwnedCleanup 10)
    Assert-Check 'singletonReleased' (-not [UpcamRealHostRun]::SingletonExists())
    $lease = $null
    try {
        $lease = [IO.File]::Open([IO.Path]::Combine($script:userRoot, 'host.lock'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        Assert-Check 'hostLeaseReleased' $true
    }
    finally { if ($null -ne $lease) { $lease.Dispose() } }
    $script:result.cleanup = 'graceful'
    $script:result.succeeded = $true
    $script:result.status = 'passed'
    $script:phase = 'completed'
}
catch {
    # Deliberately omit exception messages, UI strings, paths, stacks, and response values from evidence.
    $script:result.status = 'failed'
    $script:result.failureCode = $script:failureCode
    $script:result.failureClassification = Get-SafeFailure $_
    if ($null -ne $script:selectorProbe -and $script:selectorProbe.category -ne 'matched') {
        $script:result.selectorDiagnostic = $script:selectorProbe
    }
}
finally {
    if ($null -ne $script:owned) {
        if (-not $script:result.succeeded) {
            # Preserve the actual failing owned window before requesting close or terminating its job.
            # The P2 permission gate also covers this optional evidence write.
            if ($script:canWriteReport -and $script:screenshotFile -and $script:result.screenshot -ne 'captured') {
                $script:result.screenshot = 'unavailable-on-failure'
                try {
                    if (-not $script:owned.HostExited) {
                        $failureWindow = $script:owned.FindHostWindow()
                        if ($failureWindow -ne [IntPtr]::Zero -and $script:owned.Capture($failureWindow, $script:screenshotFile)) {
                            $script:result.screenshot = 'captured-on-failure'
                        }
                    }
                }
                catch { $script:result.screenshot = 'unavailable-on-failure' }
            }
            try {
                if (-not $script:owned.HostExited -and $null -ne $script:window) {
                    $close = $null
                    if ($script:window.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$close)) {
                        $close.Close()
                        $null = Wait-ForExit 10
                    }
                }
            }
            catch { } # A failing provider must not skip bounded cleanup of the owned job.
            try {
                if ($script:owned.LiveProcessCount -ne 0 -or $script:owned.TrackedWorkerAlive) {
                    $script:forcedCleanup = $true
                    $script:owned.TerminateOwned()
                }
                if (Wait-ForOwnedCleanup 10) {
                    $script:result.cleanup = if ($script:forcedCleanup) { 'forced' } else { 'graceful-after-failure' }
                }
                else { $script:result.cleanup = 'incomplete' }
            }
            catch { $script:result.cleanup = 'incomplete' }
        }
        # The job's close fallback is limited to this run's contained process tree.
        $script:owned.Dispose()
    }
    $script:result.phase = $script:phase
    if ($script:canWriteReport -and $script:reportFile) {
        try {
            $parent = [IO.Path]::GetDirectoryName($script:reportFile)
            [IO.Directory]::CreateDirectory($parent) | Out-Null
            $json = $script:result | ConvertTo-Json -Depth 5
            $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($json)
            $reportStream = $null
            try {
                $reportStream = [IO.File]::Open($script:reportFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $reportStream.Write($bytes, 0, $bytes.Length)
            }
            finally { if ($null -ne $reportStream) { $reportStream.Dispose() } }
        }
        catch {
            $script:result.succeeded = $false
            [Console]::Error.WriteLine('Actual platform native UI smoke could not write its sanitized report.')
        }
    }
}
if ($script:result.succeeded) {
    [Console]::WriteLine('Actual platform production plugin native UI smoke passed.')
    exit 0
}
[Console]::Error.WriteLine('Actual platform native UI smoke failed. Consult the sanitized JSON report when available.')
exit 1
