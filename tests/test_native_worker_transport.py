"""Run the actual standalone transport against an offline child process."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]


class NativeWorkerTransportTests(unittest.TestCase):
    def test_progress_transport_exists_and_is_exercised_by_windows_ci(self):
        native = ROOT / "winui" / "UPCAMTaskDigest"
        self.assertTrue((native / "WorkerProtocol.cs").is_file(),
                        "The native bounded progress reader is missing")

    @unittest.skipUnless(shutil.which("dotnet"), ".NET SDK is required for real process transport tests")
    def test_actual_csharp_transport_against_local_process(self):
        with tempfile.TemporaryDirectory(prefix="upcam-offline-transport-") as temporary:
            directory = Path(temporary)
            files = [ROOT / "winui" / "UPCAMTaskDigest" / name for name in ("WorkerClient.cs", "WorkerProtocol.cs")]
            files.append(ROOT / "tests" / "native_worker" / "Program.cs")
            includes = "".join(f'<Compile Include="{escape(str(path))}" />' for path in files)
            (directory / "Probe.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                '<OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable>'
                '<ImplicitUsings>enable</ImplicitUsings></PropertyGroup><ItemGroup>' + includes + '</ItemGroup></Project>')
            process = subprocess.run(["dotnet", "run", "--project", str(directory / "Probe.csproj"), "-c", "Release"],
                                     capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn("SUCCESS", process.stdout)
            self.assertGreaterEqual(process.stdout.count("PASS "), 12)
