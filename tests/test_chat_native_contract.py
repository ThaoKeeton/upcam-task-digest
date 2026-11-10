"""Static contracts plus fake worker exercised on Windows by native smoke."""
import ast
import json
from pathlib import Path
import re
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

    def test_chat_multiline_controls_enable_returns_before_assigning_text(self):
        source = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        controls = dict((match.group('id'), match.group('body')) for match in re.finditer(
            r'new TextBox\s*\{(?P<body>.*?)\}, "(?P<id>chat-[^"]+)"', source, re.S))
        for identifier in ('chat-message-input', 'chat-send-preview-text', 'chat-resolve-original-text'):
            with self.subTest(control=identifier):
                body = controls[identifier]
                self.assertLess(body.index('AcceptsReturn = true'), re.search(r'\bText\s*=', body).start())

    def test_native_smoke_covers_multiline_rebuild_and_rejects_content_changes(self):
        source = (UI / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        self.assertIn('VerifyChatMultilineControls();', source)
        self.assertIn('"Chat multiline composer survives navigation with every line intact"', source)
        for case in ('!ChatPreviewMatches(original, "A")',
                     '!ChatPreviewMatches(original, original + newline)',
                     '!ChatPreviewMatches(original, original.Replace("B", "X"))',
                     '!ChatPreviewMatches(original, original.Trim())'):
            self.assertIn(case, source)
        page = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        self.assertIn('expectedLines.SequenceEqual(actualLines, StringComparer.Ordinal)', page)

    def test_send_preview_fails_closed_before_confirmation_and_before_dispatch(self):
        source = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        send = source.split('private async Task SendChatDraftAsync()', 1)[1].split(
            'private async Task ResolveChatSendAsync()', 1)[0]
        guards = list(re.finditer(r'if \(!ChatPreviewMatches\(answer, previewText.Text\)\)\s*'
                                 r'\{\s*SetStatus\(previewMismatch\);\s*return;\s*\}', send))
        self.assertEqual(len(guards), 2)
        show = send.index('await ShowDialogAsync(preview)')
        self.assertLess(guards[0].start(), show)
        self.assertLess(show, guards[1].start())
        self.assertLess(guards[1].end(), send.index('_chatDraftId = null;'))
        self.assertLess(guards[1].end(), send.index('Worker.SendAsync("chat_send"'))
        self.assertIn('_chatDraftId != draftId || _chatDraftAnswer != answer', send)

    def test_optional_recovery_original_fails_closed_before_confirmation_and_resolution(self):
        source = (UI / 'MainWindow.Chat.cs').read_text(encoding='utf-8')
        recovery = source.split('private async Task ResolveChatSendAsync()', 1)[1]
        guard = 'if (original is not null && !ChatPreviewMatches(originalAnswer!, original.Text))'
        self.assertEqual(recovery.count(guard), 2)
        first, second = recovery.index(guard), recovery.rindex(guard)
        show = recovery.index('await ShowDialogAsync(dialog)')
        self.assertLess(first, show)
        self.assertLess(show, second)
        self.assertLess(second, recovery.index('Worker.SendAsync("chat_resolve_send"'))
        for block in recovery.split(guard)[1:]:
            self.assertRegex(block, r'^\s*\{\s*Report\(originalMismatch\);\s*return;\s*\}')

    def test_native_smoke_rejects_altered_preview_then_cancels_and_reopens(self):
        source = (UI / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        self.assertIn('chatPreview.Text = alteredPreview;', source)
        self.assertIn('"Changed synthetic chat preview cannot send or consume its draft"', source)
        self.assertIn('"Canceling a reopened chat preview preserves its draft"', source)

    def test_preview_diagnostics_survive_a_full_text_comparison_failure(self):
        source = (UI / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        chat = source.split('private async Task VerifyChatAsync(string output)', 1)[1]
        chat = chat.split('private async Task VerifyCollectionProgressControlsAsync()', 1)[0]
        # Only newline encoding may differ; all lines and characters must still match.
        self.assertIn('RecordSmokeTextComparison("chat-preview"', chat)
        self.assertLess(chat.index('if (!_smoke)'), chat.index('Navigate("chat")'))
        self.assertLess(chat.index('string answerAtOpen = _chatDraftAnswer;'),
                        chat.index('await InvokeWhenReadyAsync("chat-send-preview");'))
        original_assertion = 'Assert(ChatPreviewMatches(_chatDraftAnswer, previewAtFirstCheck) &&'
        self.assertIn(original_assertion, chat)
        self.assertLess(chat.index('RecordSmokeTextComparison("chat-preview"'),
                        chat.index(original_assertion))
        self.assertIn('DispatcherQueue.TryEnqueue(', chat)
        self.assertLess(chat.index('await dispatcherTurn.Task.WaitAsync('),
                        chat.index('RecordSmokeTextComparison("chat-preview-after-dispatch"'))
        self.assertLess(chat.index('RecordSmokeTextComparison("chat-preview-first-vs-after-dispatch"'),
                        chat.index(original_assertion))
        self.assertLess(chat.index('VerifyChatMultilineControls();'), chat.index(original_assertion))
        self.assertIn('RecordSmokeTextComparison($"textbox-{label}-text-first"', chat)
        self.assertIn('RecordSmokeTextComparison($"textbox-{label}-accepts-return-first"', chat)
        for fixture in (r'("LF", "\n")', r'("CRLF", "\r\n")', r'("CR", "\r")'):
            self.assertIn(fixture, chat)
        finish = source.split('private void FinishSmoke(bool success, string message)', 1)[1]
        self.assertIn('string.Join("\\n", _smokeDiagnostics)', finish)

if __name__ == '__main__': unittest.main()
