"""Contracts for the pinned Workbench 0.4.0 shipping-pair driver.

The reference host in this repository uses a different window title. These
checks lock the shipping driver to the confirmed 0.4.0 Default workspace
title and to a SHA256 implementation that does not depend on Get-FileHash.
They do not open a host, download artifacts, or claim a Windows pass.
"""
import hashlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'integrations/project-workbench/shipping-pairing/test-shipping-host.ps1'
HELPER = ROOT / 'integrations/project-workbench/shipping-pairing/test-hash-helper.ps1'
WORKFLOW = ROOT / '.github/workflows/workbench-plugin.yml'
REFERENCE_HOST = ROOT / 'integrations/project-workbench/test-real-host.ps1'
HOST_EXE_SHA256 = '48598e37f84e1f0ca60b0fadaa33a540876d5b83b6db08da593d8b48e33c35ac'
HOST_DLL_SHA256 = '25aa56d72caf6b137250b7be75d4e5c5f1e49a3ed4339081f50a7e209dbfd69b'
SHIPPING_TITLE = 'Default · Project Workbench'
REFERENCE_TITLE = 'Project Workbench · 原生插件工作台'


class ShippingHostContractTests(unittest.TestCase):
    def test_shipping_driver_waits_for_the_pinned_default_workspace_title(self):
        source = DRIVER.read_text(encoding='utf-8-sig')
        self.assertTrue(DRIVER.read_bytes().startswith(b'\xef\xbb\xbf'))
        window_fn = source.split('public IntPtr FindHostWindow()', 1)[1].split(
            'public int VisibleOwnedWindows()', 1)[0]
        self.assertIn(SHIPPING_TITLE, window_fn)
        self.assertNotIn(REFERENCE_TITLE, window_fn)
        self.assertNotIn(REFERENCE_TITLE, source)
        start = source.split("Set-Phase 'host-start'", 1)[1].split("Set-Phase 'plugin-discovery'", 1)[0]
        self.assertNotIn(
            '$script:window = [System.Windows.Automation.AutomationElement]::FromHandle($handle)', start)
        self.assertLess(start.index('FromHandle($handle)'), start.index("Current.Name -ceq '%s'" % SHIPPING_TITLE))
        self.assertIn("$script:window.Current.Name -ceq '%s'" % SHIPPING_TITLE, start)
        reference = REFERENCE_HOST.read_text(encoding='utf-8-sig')
        self.assertIn(REFERENCE_TITLE, reference)
        self.assertNotIn(SHIPPING_TITLE, reference)

    def test_byte_identity_uses_read_sha256_and_keeps_get_filehash_as_a_probe(self):
        source = DRIVER.read_text(encoding='utf-8-sig')
        phase = source.split("Set-Phase 'shipping-hash'", 1)[1].split("Set-Phase 'package-install'", 1)[0]
        self.assertEqual(source.count('Get-FileHash -LiteralPath'), 2)
        self.assertEqual(phase.count('Get-FileHash -LiteralPath'), 2)
        self.assertLess(phase.index('Get-FileHash -LiteralPath'), phase.index('Read-Sha256 $hostFile'))
        self.assertLess(phase.index('Read-Sha256 $hostDll'), phase.index("Assert-Check 'exactShippingHostBytes'"))
        check = next(line for line in phase.splitlines() if "Assert-Check 'exactShippingHostBytes'" in line)
        self.assertNotIn('Get-FileHash', check)
        self.assertIn("-ceq '%s'" % HOST_EXE_SHA256, phase)
        self.assertIn("-ceq '%s'" % HOST_DLL_SHA256, phase)
        self.assertIn("-eq '%s'" % HOST_EXE_SHA256, phase)
        self.assertIn('hashCmdletProbe', phase)
        self.assertIn("backendVersion = '0.4.6'", source)
        self.assertIn("$inventory.backendVersion -ceq '0.4.6'", source)

    def test_failure_classification_omits_exception_text(self):
        source = DRIVER.read_text(encoding='utf-8-sig')
        helper = source.split('function Get-SafeFailure', 1)[1].split('function Wait-For', 1)[0]
        self.assertIn('kind = $kind; command = $command; line = $line', helper)
        self.assertNotIn('Exception.Message', helper)
        self.assertNotIn('InvocationInfo.Line', helper)
        self.assertEqual(source.count('function Get-SafeFailure'), 1)
        self.assertEqual(source.count('function Read-Sha256'), 1)
        self.assertIn('$script:result.failureClassification = Get-SafeFailure $_', source)

    def test_hash_helper_checks_known_vectors_without_opening_a_host(self):
        helper = HELPER.read_text(encoding='utf-8')
        abc = hashlib.sha256(b'abc').hexdigest()
        abcd = hashlib.sha256(b'abcd').hexdigest()
        self.assertEqual(abc, 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
        self.assertIn(abc, helper)
        self.assertIn(abcd, helper)
        self.assertIn("if ($PSVersionTable.PSEdition -cne 'Desktop')", helper)
        self.assertIn('FunctionDefinitionAst', helper)
        self.assertIn('No UI, app, profile or provider is opened.', helper)
        self.assertNotIn('Workbench.App.exe', helper)
        self.assertNotIn('oapi.dingtalk.com', helper)
        self.assertNotIn('webhook', helper.lower())

    def test_plugin_workflow_runs_the_helper_before_uia_and_does_not_publish(self):
        workflow = WORKFLOW.read_text(encoding='utf-8')
        self.assertLess(
            workflow.index('Verify pure SHA256 and safe failure helpers in Windows PowerShell'),
            workflow.index('Install, trust, diagnose prerequisite, disable and close exact shipping pair'))
        self.assertIn('test-hash-helper.ps1', workflow)
        self.assertIn("backendVersion -cne '0.4.6'", workflow)
        self.assertNotIn('0.4.4', workflow)
        self.assertNotIn('42558432795', workflow)
        self.assertNotIn('389ccb1ef578e0cd01db91d4df540578e1851378', workflow)
        self.assertNotIn('publish-stable', workflow)
        windows = (ROOT / '.github/workflows/windows-build.yml').read_text(encoding='utf-8')
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.publish_stable == true && github.ref == 'refs/heads/main'",
            windows)
        self.assertIn("Assert-Check 'savedAutomaticSendingOff'", DRIVER.read_text(encoding='utf-8-sig'))
        self.assertIn('已保存的自动发送已关闭。', DRIVER.read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
