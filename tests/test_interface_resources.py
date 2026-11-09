"""Native interface resources, with an offline probe of the real C# helper.

The probe uses only synthetic values. It needs a .NET 8 SDK, but no WinUI,
NuGet packages, worker, network, saved settings, or credentials.
"""

from __future__ import annotations

from collections import Counter
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "winui" / "UPCAMTaskDigest"
RESOURCE = NATIVE / "Strings" / "en-US.json"
CJK = re.compile(r"[\u3400-\u9fff]")
PLACEHOLDER = re.compile(r"(?<!\{)\{(\d+)(?:,-?\d+)?(?::[^{}]+)?\}(?!\})")


def _csharp_literals(source: str):
    """Read UI string literals, replacing interpolation expressions by slots.

    This is a resource coverage audit, not a C# behavior substitute. Comments
    and expressions inside interpolated literals do not become translation keys.
    """
    def read_string(index: int, interpolated: bool, verbatim: bool = False):
        pieces = []
        argument = 0
        index += 1  # opening quote
        while index < len(source):
            char = source[index]
            if char == '"' and verbatim and source[index:index + 2] == '""':
                pieces.append('"')
                index += 2
                continue
            if char == '"':
                return "".join(pieces), index + 1
            if char == "\\" and not verbatim:
                end = index + (6 if source[index + 1:index + 2] == "u" else 2)
                encoded = source[index:end]
                pieces.append(json.loads('"' + encoded + '"'))
                index = end
                continue
            if interpolated and char == "{" and source[index:index + 2] != "{{":
                depth = 1
                index += 1
                while depth:
                    if source[index] == '"':
                        _, index = read_string(index, False)
                        continue
                    if source[index] == "{":
                        depth += 1
                    elif source[index] == "}":
                        depth -= 1
                    index += 1
                pieces.append("{" + str(argument) + "}")
                argument += 1
                continue
            if interpolated and source[index:index + 2] in ("{{", "}}"):
                pieces.append(source[index:index + 2])
                index += 2
                continue
            pieces.append(char)
            index += 1
        raise AssertionError("Unterminated C# string in resource coverage audit")

    index = 0
    while index < len(source):
        if source[index:index + 2] == "//":
            index = source.find("\n", index)
            if index < 0:
                break
        elif source[index:index + 2] == "/*":
            index = source.index("*/", index + 2) + 2
        elif source[index] == '"':
            text, index = read_string(index, index > 0 and source[index - 1] == "$",
                                      index > 0 and source[index - 1] == "@")
            yield text
        else:
            index += 1


def _resources():
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"Duplicate English resource key: {key!r}")
            result[key] = value
        return result

    return json.loads(RESOURCE.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


class InterfaceResourceTests(unittest.TestCase):
    def test_english_resources_are_complete_and_preserve_format_arguments(self):
        # Catches missing/empty translations and dropped or changed arguments.
        self.assertTrue(RESOURCE.is_file(), "The separate English resource is missing")
        resources = _resources()
        self.assertIsInstance(resources, dict)
        self.assertTrue(resources)
        for key, value in resources.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertTrue(key.strip())
                self.assertTrue(value.strip())
                self.assertFalse(CJK.search(value), "English interface text still contains Chinese")
                self.assertEqual(Counter(PLACEHOLDER.findall(key)), Counter(PLACEHOLDER.findall(value)))

    def test_current_native_interface_literals_have_english_resources(self):
        # A new hard-coded native label without a resource fails this audit.
        self.assertTrue(RESOURCE.is_file(), "The separate English resource is missing")
        resources = _resources()
        expected = set()
        for name in ("Ui.cs", "DesktopState.cs", "NativeDesktop.cs", "WorkerClient.cs",
                     "MainWindow.xaml.cs", "MainWindow.Pages.cs", "MainWindow.General.cs", "MainWindow.Diagnostics.cs", "WorkerProtocol.cs", "ProcessHandoff.cs"):
            expected.update(text for text in _csharp_literals((NATIVE / name).read_text(encoding="utf-8"))
                            if CJK.search(text))
        import xml.etree.ElementTree as ET
        for node in ET.parse(NATIVE / "MainWindow.xaml").iter():
            expected.update(value for value in node.attrib.values() if CJK.search(value))
        self.assertEqual(sorted(expected - resources.keys()), [], "Untranslated fixed native interface text")

    def test_fixed_worker_safety_messages_have_english_resources(self):
        # Fixed statuses can be translated by exact lookup; raw diagnostics
        # and user/project text must stay outside the translation boundary.
        self.assertTrue(RESOURCE.is_file(), "The separate English resource is missing")
        resources = _resources()
        fixed = set()
        for name in ("upcam_app/worker_api.py", "upcam_app/preferences.py", "upcam_app/updates.py",
                     "upcam_app/project_setup.py", "upcam_app/dingtalk_mentions.py",
                     "upcam_app/windows_integration.py", "upcam_app/config.py", "delivery_guard.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            fixed.update(node.value for node in ast.walk(tree)
                         if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value))
        self.assertEqual(sorted(fixed - resources.keys()), [], "Untranslated fixed worker status/error text")

    def test_composed_save_statuses_are_translated_as_complete_fixed_messages(self):
        # Worker save statuses contain concatenated fixed sentences. Checking
        # only their individual fragments would allow an untranslated result.
        self.assertTrue(RESOURCE.is_file(), "The separate English resource is missing")
        resources = _resources()
        for message in (
            "本机设置已保存；服务密钥由 Windows 当前用户 DPAPI 加密。 项目范围只读核验有效。",
            "本机设置已保存；服务密钥由 Windows 当前用户 DPAPI 加密。 "
            "请填写项目和 Teambition 凭据后保存，以完成只读核验。 自动运行已关闭；未调用 OpenAI，未发送钉钉。",
        ):
            self.assertTrue(message in resources, f"Missing complete fixed save status: {message}")

    def test_real_csharp_resolver_resource_loading_and_opaque_format_values(self):
        # Compiles the actual helper and actual embedded JSON. Removing the
        # embedded resource, breaking resolution, or translating an argument
        # causes the process to fail. No implementation is reproduced in Python.
        self.assertTrue((NATIVE / "UiStrings.cs").is_file(), "The native language helper is missing")
        self.assertTrue(RESOURCE.is_file(), "The separate English resource is missing")
        dotnet = shutil.which("dotnet")
        if not dotnet:
            self.skipTest(".NET 8 SDK is unavailable; the real C# resource/resolver probe runs on Windows CI")
        with tempfile.TemporaryDirectory(prefix="upcam-ui-strings-") as directory:
            target = Path(directory)
            (target / "NuGet.Config").write_text(
                "<configuration><packageSources><clear /></packageSources></configuration>", encoding="utf-8")
            (target / "Probe.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                '<OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>'
                '<Nullable>enable</Nullable><ImplicitUsings>enable</ImplicitUsings>'
                '<NuGetAudit>false</NuGetAudit>'
                '</PropertyGroup><ItemGroup>'
                f'<Compile Include="{escape(str(NATIVE / "UiStrings.cs"))}" Link="UiStrings.cs" />'
                f'<EmbeddedResource Include="{escape(str(RESOURCE))}" LogicalName="Upcam.Desktop.Strings.en-US.json" />'
                '</ItemGroup></Project>', encoding="utf-8")
            (target / "Program.cs").write_text(CSHARP_PROBE, encoding="utf-8")
            completed = subprocess.run(
                [dotnet, "run", "--project", str(target / "Probe.csproj"), "--configuration", "Release",
                 "--verbosity", "quiet"],
                cwd=target, capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("Real UiStrings behavior passed", completed.stdout)


CSHARP_PROBE = r'''
using System.Globalization;
using Upcam.Desktop;

void Equal(string actual, string expected)
{
    if (actual != expected) throw new Exception($"Expected [{expected}], got [{actual}]");
}

foreach (var item in new (string Choice, string System, string Expected)[]
{
    ("system", "zh-CN", "zh-CN"), ("system", "zh-TW", "zh-CN"),
    ("system", "zh-Hant-HK", "zh-CN"), ("system", "zh", "zh-CN"),
    ("system", "en-US", "en-US"), ("system", "en-GB", "en-US"),
    ("system", "de-DE", "en-US"), ("system", "", "en-US"),
    ("system", "invalid-language", "en-US"), ("zh-CN", "en-US", "zh-CN"),
    ("en-US", "zh-CN", "en-US"), ("fr-FR", "zh-CN", "en-US"),
    ("zh-TW", "zh-CN", "en-US")
})
{
    UiStrings.Configure(item.Choice, item.System);
    Equal(UiStrings.ResolvedLanguage, item.Expected);
}

var originalUi = CultureInfo.CurrentUICulture;
var originalCulture = CultureInfo.CurrentCulture;
try
{
    CultureInfo.CurrentUICulture = CultureInfo.GetCultureInfo("zh-HK");
    CultureInfo.CurrentCulture = CultureInfo.GetCultureInfo("de-DE");
    UiStrings.Configure("system");
    Equal(UiStrings.ResolvedLanguage, "zh-CN");
    Equal(UiStrings.T("主页"), "主页");
    Equal(CultureInfo.CurrentUICulture.Name, "zh-HK");
    Equal(CultureInfo.CurrentCulture.Name, "de-DE");

    UiStrings.Configure("en-US");
    Equal(UiStrings.T("主页"), "Home");
    Equal(UiStrings.T("未调用 OpenAI；未发送钉钉。"), "OpenAI was not called; nothing was sent to DingTalk.");
    Equal(UiStrings.F("{0} 位成员 · {1} 个状态", 2, 3), "2 members · 3 statuses");
    Equal(UiStrings.F("项目：{0}", "主页 {unsafe} : 原始项目"), "Project: 主页 {unsafe} : 原始项目");
    Equal(UiStrings.F("样本建议：{0}", "未解决"), "Sample suggestion: 未解决");
    Equal(UiStrings.F("{0:N1}", 1234.5), "1,234.5");
    Equal(UiStrings.T("原始任务：主页 / # Markdown {0}"), "原始任务：主页 / # Markdown {0}");
    Equal(UiStrings.T("not a resource"), "not a resource");
    Equal(UiStrings.T(""), "");

    UiStrings.Configure("zh-CN");
    Equal(UiStrings.F("项目：{0}", "Home : untouched"), "项目：Home : untouched");
}
finally
{
    CultureInfo.CurrentUICulture = originalUi;
    CultureInfo.CurrentCulture = originalCulture;
    UiStrings.Configure("system");
}
Console.WriteLine("Real UiStrings behavior passed");
'''


if __name__ == "__main__":
    unittest.main()
