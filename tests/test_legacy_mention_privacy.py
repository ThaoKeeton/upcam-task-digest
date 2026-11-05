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
