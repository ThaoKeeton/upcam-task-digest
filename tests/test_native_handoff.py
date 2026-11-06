"""Real Windows process/event/file-sharing probes, using temporary fixtures only.

Links the production helpers into an offline .NET 8 console apphost. No WinUI,
NuGet packages, worker, application profile, credentials, registry or API is used.
The timeout case deliberately spends 15 seconds in the real StartAsync method.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "winui" / "UPCAMTaskDigest"


def _isolated_environment(directory: Path) -> dict[str, str]:
    # Select only platform/toolchain paths, rather than forwarding credentials.
    result = {name: os.environ[name] for name in (
        "PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "PROCESSOR_ARCHITECTURE",
        "DOTNET_ROOT", "DOTNET_ROOT_X64",
    ) if name in os.environ}
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                 "DOTNET_CLI_HOME", "NUGET_PACKAGES", "NUGET_HTTP_CACHE_PATH"):
        target = directory / name.lower()
        target.mkdir(parents=True, exist_ok=True)
        result[name] = str(target)
    result.update(DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
                  DOTNET_NOLOGO="1", DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="1",
                  DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER="1", MSBUILDDISABLENODEREUSE="1")
    return result


@contextmanager
def _owned_job(process: subprocess.Popen):
    """A private job kills only this test's assigned process and descendants."""
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", ctypes.c_uint32), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_uint32),
                    ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
                    ("scheduling", ctypes.c_uint32)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "read_operations", "write_operations", "other_operations",
            "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    kernel.SetInformationJobObject.restype = ctypes.c_int
    kernel.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # The probe waits for a go marker before creating any descendants.
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        kernel.CloseHandle(job)


@unittest.skipUnless(os.name == "nt", "Real Win32 handoff probes require Windows; no native behavior is simulated on Linux")
class NativeHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise unittest.SkipTest(".NET 8 SDK unavailable; real native handoff probes require the Windows CI SDK")
        cls.temporary = tempfile.TemporaryDirectory(prefix="upcam-native-handoff-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.workspace = Path(cls.temporary.name)
        cls.environment = _isolated_environment(cls.workspace / "environment")
        installed = subprocess.run([dotnet, "--list-sdks"], env=cls.environment,
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        versions = re.findall(r"(?m)^(8\.\d+\.\d+)\s", installed.stdout)
        if installed.returncode or not versions:
            raise unittest.SkipTest(".NET 8 SDK unavailable; the apphost probe needs its installed offline reference pack")
        sdk = max(versions, key=lambda version: tuple(map(int, version.split("."))))
        (cls.workspace / "global.json").write_text(json.dumps({"sdk": {"version": sdk}}), encoding="utf-8")
        (cls.workspace / "NuGet.Config").write_text(
            "<configuration><packageSources><clear /></packageSources></configuration>", encoding="utf-8")
        linked = "".join(f'<Compile Include="{escape(str(NATIVE / name))}" Link="{name}" />'
                         for name in ("ProcessHandoff.cs", "AppLaunchOptions.cs", "InterfaceLanguageBootstrap.cs", "UiStrings.cs"))
        (cls.workspace / "NativeHandoffProbe.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
            '<TargetFramework>net8.0</TargetFramework><UseAppHost>true</UseAppHost>'
            '<Nullable>enable</Nullable><ImplicitUsings>enable</ImplicitUsings><NuGetAudit>false</NuGetAudit>'
            '</PropertyGroup><ItemGroup>' + linked +
            f'<EmbeddedResource Include="{escape(str(NATIVE / "Strings" / "en-US.json"))}" '
            'LogicalName="Upcam.Desktop.Strings.en-US.json" /></ItemGroup></Project>', encoding="utf-8")
        (cls.workspace / "Program.cs").write_text(CSHARP_PROBE, encoding="utf-8")
        cls.output = cls.workspace / "compiled"
        built = subprocess.run([dotnet, "build", str(cls.workspace / "NativeHandoffProbe.csproj"),
                                "--configuration", "Release", "--output", str(cls.output),
                                "--verbosity", "quiet", "--disable-build-servers", "-p:UseSharedCompilation=false"],
                               cwd=cls.workspace, env=cls.environment, capture_output=True,
                               text=True, encoding="utf-8", timeout=150)
        if built.returncode:
            raise AssertionError("Real C# handoff probe failed to compile:\n" + built.stdout + built.stderr)
        if not (cls.output / "NativeHandoffProbe.exe").is_file():
            raise AssertionError("The offline build did not produce the required real apphost EXE")

    def _case(self, mode: str):
        directory = Path(tempfile.mkdtemp(prefix=mode + "-", dir=self.workspace))
        binary = directory / "parent"
        shutil.copytree(self.output, binary)
        target = directory / "target"
        if mode == "locked":
            shutil.copytree(self.output, target)
        else:
            target = binary
        config = {"Root": str(directory), "Mode": mode,
                  "Target": str(target / "NativeHandoffProbe.exe")}
        for path in {binary, target}:
            (path / "probe-case.json").write_text(json.dumps(config), encoding="utf-8")
        return directory, binary / "NativeHandoffProbe.exe"

    def _marker(self, directory: Path, name: str, process: subprocess.Popen, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = directory / name
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"Probe exited {process.returncode} before {name}:\n{stdout}{stderr}")
            time.sleep(0.025)
        self.fail(f"Timed out waiting for synthetic marker {name}")

    def _run(self, mode: str):
        directory, executable = self._case(mode)
        process = subprocess.Popen([str(executable), "--probe-parent"], cwd=directory,
                                   env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        try:
            with _owned_job(process):
                (directory / "go").write_text("synthetic", encoding="utf-8")
                if mode in {"success", "locked"}:
                    ack = self._marker(directory, "parent-ack.json", process)
                    self.assertIsNone(process.poll(), "The parent must remain alive until the test releases it")
                    self.assertFalse((directory / "child-complete.json").exists(), "The child ran before its parent exited")
                    self.assertTrue(ack["child_waiting"])
                    if mode == "locked":
                        self.assertTrue(ack["write_denied"] and ack["delete_denied"])
                    (directory / "release-parent").write_text("synthetic", encoding="utf-8")
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    deadline = time.monotonic() + 12
                    while not (directory / "child-complete.json").exists() and time.monotonic() < deadline:
                        time.sleep(0.025)
                    self.assertTrue((directory / "child-complete.json").is_file(), "Child did not finish after the parent exited")
                    child = json.loads((directory / "child-complete.json").read_text(encoding="utf-8"))
                    self.assertTrue(child["parent_exit_observed"] and child["after_update"] and child["background"])
                    self.assertEqual(child["parent_pid"], process.pid)
                elif mode == "timeout":
                    observed = self._marker(directory, "timeout-observed.json", process, timeout=50)
                    self.assertIsNone(process.poll(), "No-ack timeout must preserve the parent")
                    self.assertGreaterEqual(observed["elapsed_ms"], 14000)
                    self.assertLess(observed["elapsed_ms"], 40000)
                    self.assertTrue(observed["child_terminated"] and observed["sentinel_alive"])
                    self.assertEqual(observed["parent_pid"], process.pid)
                    (directory / "release-parent").write_text("synthetic", encoding="utf-8")
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertTrue((directory / "sentinel-complete.json").is_file(), "The unrelated test sentinel must survive timeout cleanup")
                else:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertIn("synthetic checks passed", stdout)
        finally:
            # If assignment itself failed, no descendants were allowed past go.
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_parent_child_handoff_waits_for_parent_exit(self):
        self._run("success")

    def test_no_ack_timeout_preserves_parent_and_kills_only_child(self):
        self._run("timeout")

    def test_read_shared_executable_lock_covers_handoff_and_blocks_write_delete(self):
        self._run("locked")

    def test_invalid_launch_options(self):
        self._run("arguments")

    def test_bootstrap_uses_only_temporary_whitelisted_files(self):
        self._run("bootstrap")


CSHARP_PROBE = r'''
using System.Diagnostics;
using System.Text.Json;
using Upcam.Desktop;

internal sealed record ProbeCase(string Root, string Mode, string Target);

internal static class Program
{
    static ProbeCase Case = null!;
    static string Marker(string name) => Path.Combine(Case.Root, name);
    static void Require(bool condition, string message)
    { if (!condition) throw new InvalidOperationException("Synthetic probe: " + message); }
    static void Write(string name, object value)
    {
        string temporary = Marker(name) + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(value));
        File.Move(temporary, Marker(name), true);
    }
    static async Task WaitFile(string name, int seconds = 12)
    {
        var clock = Stopwatch.StartNew();
        while (!File.Exists(Marker(name)))
        {
            if (clock.Elapsed > TimeSpan.FromSeconds(seconds)) throw new TimeoutException("Synthetic marker: " + name);
            await Task.Delay(20);
        }
    }
    static int RecordedPid(string name) => JsonDocument.Parse(File.ReadAllText(Marker(name))).RootElement.GetProperty("pid").GetInt32();
    static async Task<int> Main(string[] args)
    {
        try
        {
            Case = JsonSerializer.Deserialize<ProbeCase>(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "probe-case.json")))!;
            UiStrings.Configure("en-US", "en-US");
            Require(Path.IsPathFullyQualified(Case.Root) && Directory.Exists(Case.Root), "temporary root required");
            if (args.SequenceEqual(new[] { "--probe-sentinel" }))
            {
                Write("sentinel-started.json", new { pid = Environment.ProcessId });
                await WaitFile("release-sentinel", 60);
                Write("sentinel-complete.json", new { survived = true });
                return 0;
            }
            if (!args.SequenceEqual(new[] { "--probe-parent" })) return await Child(args);
            await WaitFile("go");
            if (Case.Mode == "arguments") { Arguments(); Console.WriteLine("synthetic checks passed"); return 0; }
            if (Case.Mode == "bootstrap") { Bootstrap(); Console.WriteLine("synthetic checks passed"); return 0; }
            if (Case.Mode == "timeout") return await TimeoutParent();
            return await SuccessParent();
        }
        catch (Exception exception)
        { Console.Error.WriteLine(exception); return 1; }
    }
    static async Task<int> Child(string[] args)
    {
        var options = AppLaunchOptions.Parse(args, Environment.ProcessId);
        Require(options.ParentPid is not null && options.HandoffEvent is not null, "real handoff CLI required");
        Write("child-started.json", new { pid = Environment.ProcessId });
        if (Case.Mode == "timeout") { await Task.Delay(TimeSpan.FromMinutes(2)); return 99; }
        bool exited = await ProcessHandoff.WaitForParentAsync(options.ParentPid!.Value, options.HandoffEvent);
        Require(exited, "parent synchronization handle did not signal");
        Require(File.Exists(Marker("parent-ack.json")), "parent did not receive the acknowledgement");
        Write("child-complete.json", new { parent_exit_observed = exited, parent_pid = options.ParentPid,
            after_update = options.AfterUpdate, background = options.Background });
        return 0;
    }
    static async Task<int> SuccessParent()
    {
        FileStream? executableLock = null;
        bool writeDenied = false, deleteDenied = false;
        try
        {
            if (Case.Mode == "locked")
            {
                // The target is a separate, not-yet-running apphost: prove the
                // sharing restriction rather than the OS's running-image lock.
                using (var writable = new FileStream(Case.Target, FileMode.Open, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete)) { }
                string disposable = Case.Target + ".unlocked";
                File.Copy(Case.Target, disposable);
                File.Delete(Case.Target);
                File.Move(disposable, Case.Target);
                executableLock = new FileStream(Case.Target, FileMode.Open, FileAccess.Read, FileShare.Read);
                try { using var writer = new FileStream(Case.Target, FileMode.Open, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete); }
                catch (IOException) { writeDenied = true; }
                try { File.Delete(Case.Target); }
                catch (IOException) { deleteDenied = true; }
                Require(writeDenied && deleteDenied, "shared-read lock did not deny write/delete");
            }
            using var child = await ProcessHandoff.StartAsync(Case.Target, afterUpdate: true, background: true);
            await Task.Delay(250);
            Require(!child.HasExited && !File.Exists(Marker("child-complete.json")), "child did not wait for parent exit");
            Write("parent-ack.json", new { child_waiting = true, child_pid = child.Id, write_denied = writeDenied, delete_denied = deleteDenied });
            await WaitFile("release-parent");
            return 0;
        }
        finally { executableLock?.Dispose(); }
    }
    static async Task<int> TimeoutParent()
    {
        var sentinelStart = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false };
        sentinelStart.ArgumentList.Add("--probe-sentinel");
        using var sentinel = Process.Start(sentinelStart)!;
        await WaitFile("sentinel-started.json");
        var clock = Stopwatch.StartNew();
        Task<Process> launch = ProcessHandoff.StartAsync(Case.Target, afterUpdate: true);
        await WaitFile("child-started.json");
        using var observedChild = Process.GetProcessById(RecordedPid("child-started.json"));
        _ = observedChild.Handle; // Bind the exact child before timeout cleanup.
        bool timedOut = false;
        try { using var unexpected = await launch; }
        catch (InvalidOperationException exception)
        { timedOut = exception.Message == UiStrings.T("新进程未完成启动交接；当前应用和旧版本均已保留。"); }
        Require(timedOut, "no-ack launch did not report the expected timeout");
        await observedChild.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        Require(!sentinel.HasExited, "timeout cleanup killed another test process");
        Write("timeout-observed.json", new { parent_pid = Environment.ProcessId, elapsed_ms = clock.ElapsedMilliseconds,
            child_terminated = observedChild.HasExited, sentinel_alive = !sentinel.HasExited });
        await WaitFile("release-parent");
        File.WriteAllText(Marker("release-sentinel"), "synthetic");
        await sentinel.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        Require(sentinel.ExitCode == 0, "sentinel was terminated instead of released");
        return 0;
    }
    static void Arguments()
    {
        string token = Guid.NewGuid().ToString("N");
        int current = Environment.ProcessId;
        string parent = (current == 1 ? 2 : 1).ToString(System.Globalization.CultureInfo.InvariantCulture);
        var good = AppLaunchOptions.Parse(new[] { "--wait-for-parent", parent, "--handoff-event", token, "--after-update", "--background" }, current);
        Require(good.AfterUpdate && good.Background && good.HandoffEvent == token, "valid handoff options rejected");
        var restart = AppLaunchOptions.Parse(new[] { "--wait-for-parent", parent, "--handoff-event", token }, current);
        Require(!restart.AfterUpdate, "restart was treated as an update");
        foreach (var invalid in new[] {
            new[] { "--after-update" }, new[] { "--wait-for-parent" }, new[] { "--wait-for-parent", "-1" },
            new[] { "--wait-for-parent", "0" }, new[] { "--wait-for-parent", "2147483648" },
            new[] { "--wait-for-parent", current.ToString() }, new[] { "--background", "--background" },
            new[] { "--handoff-event", token }, new[] { "--wait-for-parent", parent, "--handoff-event", "invalid" },
            new[] { "--wait-for-parent", parent, "--after-update" },
            new[] { "--wait-for-parent", parent, "--wait-for-parent", parent }, new[] { "--untrusted" } })
        {
            bool rejected = false;
            try { AppLaunchOptions.Parse(invalid, current); } catch (ArgumentException) { rejected = true; }
            Require(rejected, "invalid/self/duplicate CLI accepted");
        }
    }
    static void Bootstrap()
    {
        string path = Marker("synthetic-language.json");
        Require(InterfaceLanguageBootstrap.Read(path) == "system", "missing bootstrap did not fall back");
        foreach (string language in new[] { "zh-CN", "en-US", "system", "unsupported" })
        {
            File.WriteAllText(path, JsonSerializer.Serialize(new { language }));
            Require(InterfaceLanguageBootstrap.Read(path) == (language is "zh-CN" or "en-US" ? language : "system"), "language whitelist mismatch");
        }
        foreach (string payload in new[] { "{", "[]", "{}", "{\"language\":12}", "{\"language\":\"en-US\",\"padding\":\"" + new string('x', 4096) + "\"}" })
        {
            File.WriteAllText(path, payload);
            Require(InterfaceLanguageBootstrap.Read(path) == "system", "invalid/oversized bootstrap accepted");
        }
    }
}
'''


if __name__ == "__main__":
    unittest.main()
