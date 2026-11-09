"""Static version coherence for the standalone-focused collection repair."""
from pathlib import Path
import unittest

from upcam_app import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


class StandaloneCollectionIntegrationTests(unittest.TestCase):
    def test_shared_packaging_guards_follow_the_new_backend_version(self):
        for path, marker in (
            ('.github/workflows/workbench-plugin.yml', f"$metadata.backendVersion -cne '{APP_VERSION}'"),
            ('integrations/project-workbench/shipping-pairing/ShippingInstall/Program.cs',
             f'value.GetProperty("backendVersion").GetString() != "{APP_VERSION}"'),
            ('integrations/project-workbench/shipping-pairing/test-shipping-host.ps1',
             f"$inventory.backendVersion -ceq '{APP_VERSION}'"),
        ):
            with self.subTest(path=path):
                self.assertTrue(marker in (ROOT / path).read_text(encoding='utf-8-sig'), f'Backend guard version mismatch in {path}')

    def test_concurrency_controls_normalize_fractional_values_before_saving(self):
        for path in ('winui/UPCAMTaskDigest/MainWindow.Pages.cs',
                     'plugins/UPCAM.Workbench/DigestPage.Settings.cs'):
            with self.subTest(path=path):
                source = (ROOT / path).read_text()
                start = source.index('concurrent.ValueChanged')
                handler = source[start:source.index('\n        };', start)]
                self.assertTrue('concurrent.Value = count;' in handler,
                                f'Concurrency input is not normalized in {path}')
                self.assertLess(handler.index('"max_concurrent_tasks"'), handler.index('concurrent.Value = count;'),
                                'Persist the normalized count before a setter whose reentrant event can be suppressed')

    def test_standalone_ci_is_enabled_only_for_the_authorized_test_branch(self):
        workflow = (ROOT / '.github/workflows/windows-build.yml').read_text()
        self.assertIn('fix/standalone-filter-concurrency', workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.publish_stable == true && github.ref == 'refs/heads/main'", workflow)
        self.assertIn('UPCAMTaskDigest-WinUI3-windows-x64', workflow)
