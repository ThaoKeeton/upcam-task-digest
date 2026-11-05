"""Display settings preserve legacy data and remain separate from collection filters."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from upcam_app.config import DEFAULT_SETTINGS, SettingsError, SettingsStore, runtime_config, validate_settings


class DisplaySettingsTests(unittest.TestCase):
    def test_legacy_settings_gain_empty_display_preferences_and_keep_schema(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.pop("task_number_prefix")
        settings.pop("task_link_view_id")
        clean = validate_settings(settings)
        self.assertEqual(clean["schema_version"], 1)
        self.assertEqual(clean["task_number_prefix"], "")
        self.assertEqual(clean["task_link_view_id"], "")

    def test_project_all_view_url_normalizes_persists_and_reaches_runtime(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(project_id="synthetic-project", task_number_prefix="DEMO",
                        task_link_view_id="https://www.teambition.com/project/synthetic-project/tasks/view/" + "f" * 24)
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            store.save(settings)
            clean = store.load()
            self.assertEqual(clean["task_link_view_id"], "f" * 24)
            self.assertEqual(clean["view"], settings["view"])
            self.assertEqual(runtime_config(clean)["task_number_prefix"], "DEMO")

    def test_cross_project_view_and_unconfirmed_prefix_shapes_fail(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(project_id="synthetic-project",
                        task_link_view_id="https://www.teambition.com/project/other-project/tasks/view/" + "f" * 24)
        with self.assertRaises(SettingsError):
            validate_settings(settings)
        settings["task_link_view_id"] = ""
        for prefix in (None, "", "DEMO", "bad prefix", "DEMO-"):
            settings["task_number_prefix"] = prefix
            if prefix in ("", "DEMO"):
                validate_settings(settings)
            else:
                with self.assertRaises(SettingsError):
                    validate_settings(settings)

    def test_saved_display_preferences_are_bound_to_tenant_and_project(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(tenant_id="synthetic-tenant", project_id="synthetic-project",
                        task_number_prefix="DEMO", task_link_view_id="f" * 24)
        clean = validate_settings(settings)
        self.assertEqual(clean.get('task_display_scope'),
                         {'tenant_id': 'synthetic-tenant', 'project_id': 'synthetic-project'})
        for field in ('tenant_id', 'project_id'):
            changed = dict(clean)
            changed[field] = 'another-synthetic-source'
            with self.assertRaises(SettingsError):
                validate_settings(changed)

    def test_cleared_preferences_allow_a_new_source_without_reusing_old_identity(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(tenant_id='new-tenant', project_id='new-project',
                        task_number_prefix='', task_link_view_id='',
                        task_display_scope={'tenant_id': 'old-tenant', 'project_id': 'old-project'})
        self.assertIsNone(validate_settings(settings).get('task_display_scope'))
