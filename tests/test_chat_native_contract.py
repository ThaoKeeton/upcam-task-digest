"""Static contracts plus fake worker exercised on Windows by native smoke."""
import ast
import json
from pathlib import Path
import unittest
from tests.test_interface_resources import _csharp_literals, CJK

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'winui' / 'UPCAMTaskDigest'


class ChatNativeContractTests(unittest.TestCase):
    def test_chat_has_its_own_left_navigation_and_page(self):
        xaml = (UI / 'MainWindow.xaml').read_text(encoding='utf-8')
        routes = (UI / 'MainWindow.xaml.cs').read_text(encoding='utf-8')
        self.assertIn('Tag="chat"', xaml)
        self.assertIn('["chat"]', routes)
        self.assertIn('"chat" => BuildChat()', routes)
        self.assertTrue((UI / 'MainWindow.Chat.cs').is_file())

    def test_chat_turn_has_bounded_long_deadline_not_ordinary_30_seconds(self):
        source = (UI / 'WorkerClient.cs').read_text(encoding='utf-8')
        self.assertIn('IsChat(operation)', source)
        self.assertIn('TimeSpan.FromMinutes(5)', source)
        self.assertIn('operation is "chat_turn"', source)

    def test_chat_discloses_planning_and_optional_source_transfer(self):
        source = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        self.assertIn('聊天内容会交给 OpenAI 理解', source)
        self.assertIn('相关项目内容', source)
        self.assertIn('确认前不会发送', source)

    def test_english_resources_cover_chat_page_and_worker_fixed_messages(self):
        resources = json.loads((UI / 'Strings' / 'en-US.json').read_text(encoding='utf-8'))
        page = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        expected = {s for s in _csharp_literals(page) if CJK.search(s)}
        for module in ('chat_worker.py', 'chat_session.py', 'chat_query.py', 'chat_model.py'):
            tree = ast.parse((ROOT / 'upcam_app' / module).read_text(encoding='utf-8'))
            expected.update(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                            and isinstance(node.value, str) and CJK.search(node.value)
                            and '\n' not in node.value and len(node.value) < 300 and '(?:' not in node.value)
        self.assertEqual(sorted(expected - resources.keys()), [])

    def test_chat_scroll_and_composer_have_native_layout_checks(self):
        page = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        smoke = (UI / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        self.assertIn('"chat-scroll"', page)
        self.assertIn('Chat conversation remains visible at', smoke)
        self.assertIn('Chat composer remains visible at', smoke)

    def test_send_recovery_previews_a_specific_record_before_confirmation(self):
        page = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        self.assertIn('chat_resolve_preview', page)
        self.assertIn('preview_id', page)
        self.assertLess(page.index('chat_resolve_preview'), page.index('chat-resolve-confirm'))
        self.assertIn('request_id', page)
        self.assertIn('destination_label', page)
        self.assertIn('content_label', page)
        self.assertIn('attempted_at_utc', page)

    def test_synthetic_windows_smoke_exercises_real_chat_page(self):
        source = (UI / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        self.assertIn('await VerifyChatAsync(', source)
        self.assertIn('chat-ask', source)
        self.assertIn('chat-send-preview', source)
        self.assertIn('CI 虚构聊天', source)

if __name__ == '__main__': unittest.main()
