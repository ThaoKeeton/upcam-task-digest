"""Legacy save boundary uses synthetic credentials and no actual Tk window."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_app import DigestDesktopApp
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.secrets import SECRET_FIELDS


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class LocalStore:
    def __init__(self, contents):
        self.contents = deepcopy(contents)
        self.writes = []

    def load(self):
        return deepcopy(self.contents)

    def save(self, value):
        self.writes.append(deepcopy(value))
        self.contents = deepcopy(value)


class LegacyMentionPrivacyTests(unittest.TestCase):
    def clear_app(self, saved_target='', draft_target='demo-user-one', entered=''):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update(dingtalk_mention_mode='user_id', dingtalk_mentions=saved_target)
        secrets = {'dingtalk_signing_secret': 'SEC_SYNTHETIC_OLD_DO_NOT_USE',
                   'delivery_ledger_id': '00000000-0000-4000-8000-000000000001'}
        values = {key: Value('') for key in SECRET_FIELDS}
        values.update(dingtalk_mention_mode=Value('user_id'), dingtalk_mentions=Value(draft_target),
                      auto_send_enabled=Value(False))
        values['dingtalk_signing_secret'] = Value(entered)
        app = SimpleNamespace(settings=settings, saved_secrets=dict(secrets), values=values,
                              secret_store=LocalStore(secrets), settings_store=LocalStore(settings),
                              root=None, _set_status=lambda _message: None)
        for key in SECRET_FIELDS:
            setattr(app, '_secret_state_' + key, SimpleNamespace(configure=lambda **_kwargs: None))
        return app

    def test_clear_rejects_saved_or_unsaved_credentials_before_discarding_comparison_values(self):
        old, new = 'SEC_SYNTHETIC_OLD_DO_NOT_USE', 'SEC_SYNTHETIC_NEW_DO_NOT_USE'
        for saved_target, draft_target, entered in ((old, 'demo-user-one', ''), ('', old, ''), ('', new, new)):
            with self.subTest(saved_collision=bool(saved_target), new_draft=bool(entered)):
                app = self.clear_app(saved_target, draft_target, entered)
                original_settings, original_secrets = deepcopy(app.settings), dict(app.saved_secrets)
                with patch('desktop_app.messagebox.askyesno', return_value=True), patch('desktop_app.messagebox.showerror') as error:
                    DigestDesktopApp._clear_secrets(app)
                self.assertTrue(error.called)
                self.assertEqual(app.secret_store.writes, [])
                self.assertEqual(app.settings_store.writes, [])
                self.assertEqual(app.settings, original_settings)
                self.assertEqual(app.saved_secrets, original_secrets)
                self.assertNotIn(old, str(error.call_args))
                self.assertNotIn(new, str(error.call_args))

    def test_clear_preserves_valid_mention_draft_and_ledger_identity(self):
        app = self.clear_app(saved_target='demo-user-one')
        identity = app.saved_secrets['delivery_ledger_id']
        with patch('desktop_app.messagebox.askyesno', return_value=True), patch('desktop_app.messagebox.showerror') as error:
            DigestDesktopApp._clear_secrets(app)
        self.assertFalse(error.called)
        self.assertEqual(app.saved_secrets, {'delivery_ledger_id': identity})
        self.assertEqual(app.secret_store.contents, {'delivery_ledger_id': identity})
        self.assertEqual(app.values['dingtalk_mentions'].get(), 'demo-user-one')
        self.assertTrue(all(app.values[key].get() == '' for key in SECRET_FIELDS))

    def test_existing_new_and_rotated_credentials_are_rejected_before_any_save(self):
        old = 'SEC_SYNTHETIC_OLD_DO_NOT_USE'
        new = 'SEC_SYNTHETIC_NEW_DO_NOT_USE'
        for target, entered in ((old, ''), (new, new), (old, new)):
            with self.subTest(rotating=bool(entered), target_is_new=target == new):
                settings = deepcopy(DEFAULT_SETTINGS)
                values = {key: Value('') for key in SECRET_FIELDS}
                values.update(dingtalk_mention_mode=Value('user_id'), dingtalk_mentions=Value(target))
                values['dingtalk_signing_secret'] = Value(entered)
                secrets = {'dingtalk_signing_secret': old}
                app = SimpleNamespace(
                    settings=settings, values=values, owner_map={}, status_map={},
                    resolved_status_ids=set(), view_filters={}, _setup_probe=None,
                    scope_confirm_var=Value(False), root=None,
                    secret_store=LocalStore(secrets), settings_store=LocalStore(settings),
                    saved_secrets=secrets, _load_model_cache=lambda: None,
                    _set_status=lambda _message: None,
                )
                for key in SECRET_FIELDS:
                    setattr(app, '_secret_state_' + key, SimpleNamespace(configure=lambda **_kwargs: None))
                with patch('desktop_app.messagebox.showerror') as error, patch('desktop_app.messagebox.showinfo'):
                    DigestDesktopApp._save(app)
                self.assertTrue(error.called, 'Misplaced credential must be rejected in the application')
                self.assertEqual(app.secret_store.writes, [])
                self.assertEqual(app.settings_store.writes, [])
                self.assertEqual(app.secret_store.contents, secrets)
                self.assertEqual(app.settings_store.contents, settings)
                self.assertNotIn(old, str(error.call_args))
                self.assertNotIn(new, str(error.call_args))


if __name__ == '__main__':
    unittest.main()
