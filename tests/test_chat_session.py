import unittest

try:
    from upcam_app.chat_session import ChatSession, ChatSessionError
except ModuleNotFoundError:
    ChatSession = None
    ChatSessionError = ValueError


def make_plan(op='tasks', *, summarize=False, send=False):
    return {'operation': op, 'search': None, 'task_id': None, 'start': None, 'end': None,
            'summarize': summarize, 'send_requested': send, 'reply': None}


class Planner:
    def __init__(self, plan): self.value, self.summary_calls, self.history, self.summary_history, self.plan_calls = plan, [], None, None, 0
    def plan(self, question, history): self.history = history; self.plan_calls += 1; return dict(self.value)
    def summarize(self, question, source, history=None):
        self.summary_history = history
        self.summary_calls.append((question, source)); return '这是虚构整理结果'


class Reader:
    def __init__(self): self.calls = []
    def execute(self, query):
        self.calls.append(query)
        return {'kind': 'tasks', 'rows': [{'id': 't1', 'content': '虚构任务'}], 'snapshot_guaranteed': False}


class Guard:
    def __init__(self): self.values = []
    def require_safe(self, obj): self.values.append(obj)


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ChatSession, 'chat session not implemented')
        self.planner, self.reader, self.guard = Planner(make_plan()), Reader(), Guard()
        self.session = ChatSession(self.planner, self.reader, self.guard)

    def test_raw_result_is_not_sent_to_summarizer(self):
        result = self.session.turn('只展示原始任务')
        self.assertIn('虚构任务', result['answer'])
        self.assertEqual(self.planner.summary_calls, [])
        self.assertEqual(len(self.reader.calls), 1)
        self.assertTrue(result['draft_id'])
        self.assertFalse(result['send_requested'])

    def test_summary_and_send_suggestion_do_not_send(self):
        self.planner.value = make_plan(summarize=True, send=True)
        result = self.session.turn('整理任务，准备发送')
        self.assertEqual(result['answer'], '这是虚构整理结果')
        self.assertTrue(result['send_requested'])
        self.assertEqual(len(self.planner.summary_calls), 1)
        self.assertIn('未发送', result['status'])
        self.assertEqual(self.session.consume_draft(result['draft_id']), result['answer'])
        with self.assertRaises(ChatSessionError): self.session.consume_draft(result['draft_id'])

    def test_user_explicitly_forbids_summary_even_if_model_requests_it(self):
        self.planner.value = make_plan(summarize=True)
        result = self.session.turn('不要用 OpenAI 整理，直接展示')
        self.assertEqual(self.planner.summary_calls, [])
        self.assertIn('虚构任务', result['answer'])

    def test_explicit_request_not_to_use_openai_stops_before_model_and_mcp(self):
        for question in ('这次不要使用 OpenAI，直接查询', '不要把任务内容发给 OpenAI 整理'):
            with self.subTest(question=question), self.assertRaises(ChatSessionError):
                self.session.turn(question)
        self.assertEqual(self.planner.plan_calls, 0)
        self.assertEqual(self.reader.calls, [])

    def test_request_to_send_previous_answer_prepares_exact_previous_text_without_sending(self):
        prior = self.session.turn('列任务')
        self.planner.value = make_plan(op='previous', send=True)
        latest = self.session.turn('把上一条发送到钉钉')
        self.assertEqual(latest['answer'], prior['answer'])
        self.assertTrue(latest['send_requested'])
        self.assertNotEqual(latest['draft_id'], prior['draft_id'])
        with self.assertRaises(ChatSessionError): self.session.consume_draft(prior['draft_id'])
        self.assertEqual(self.session.consume_draft(latest['draft_id']), prior['answer'])

    def test_previous_answer_is_unavailable_before_a_reply_and_after_reset(self):
        self.planner.value = make_plan(op='previous', send=True)
        with self.assertRaisesRegex(ChatSessionError, '没有可复用'): self.session.turn('发送上一条')
        self.planner.value = make_plan()
        self.session.turn('查询')
        self.session.clear()
        self.planner.value = make_plan(op='previous', send=True)
        with self.assertRaisesRegex(ChatSessionError, '没有可复用'): self.session.turn('发送上一条')

    def test_new_turn_and_reset_destroy_previous_draft_even_on_failure(self):
        draft = self.session.turn('列任务')['draft_id']
        self.planner.value = make_plan(op='task.delete')
        with self.assertRaises(ChatSessionError): self.session.turn('下一条')
        with self.assertRaises(ChatSessionError): self.session.consume_draft(draft)
        self.planner.value = make_plan()
        draft = self.session.turn('再来')['draft_id']
        self.session.clear()
        with self.assertRaises(ChatSessionError): self.session.consume_draft(draft)

    def test_summary_preserves_query_coverage_warning(self):
        self.planner.value = make_plan(summarize=True)
        self.reader.execute = lambda query: {'kind': 'activities', 'rows': [], 'coverage': '未推进更新时间的动态可能遗漏。'}
        result = self.session.turn('整理最近动态')
        self.assertIn('可能遗漏', result['answer'])

    def test_safe_model_errors_remain_actionable(self):
        from upcam_app.chat_model import ChatModelError
        def fail(question, history):
            raise ChatModelError('聊天内容或查询结果过大，请缩小范围。')
        self.planner.plan = fail
        with self.assertRaisesRegex(ChatSessionError, '过大.*缩小范围'):
            self.session.turn('查询')

    def test_model_error_cannot_echo_a_known_local_credential(self):
        from upcam_app.chat_model import ChatModelError
        from upcam_app.report_credentials import ReportCredentialGuard
        self.session.guard = ReportCredentialGuard({'openai_api_key': 'fictional-private-key'})
        def fail(question, history):
            raise ChatModelError('fictional-private-key')
        self.planner.plan = fail
        with self.assertRaises(ChatSessionError) as caught: self.session.turn('查询')
        self.assertNotIn('fictional-private-key', str(caught.exception))

    def test_summary_discloses_that_search_is_for_task_title_not_owner(self):
        self.planner.value = dict(make_plan(summarize=True), search='张三')
        result = self.session.turn('整理标题含张三的任务')
        self.assertIn('任务标题包含：张三', result['answer'])
        self.assertEqual(self.planner.summary_calls[0][1]['chat_query']['search_field'], 'task_title')

    def test_source_and_model_output_are_guarded(self):
        self.planner.value = make_plan(summarize=True)
        self.session.turn('整理')
        self.assertTrue(any(isinstance(v, dict) and v.get('kind') == 'tasks' for v in self.guard.values))
        self.assertIn('这是虚构整理结果', self.guard.values)

    def test_invalid_plan_never_reaches_mcp(self):
        self.planner.value = {**make_plan(), 'url': 'https://untrusted'}
        with self.assertRaises(ChatSessionError): self.session.turn('查询')
        self.assertEqual(self.reader.calls, [])

    def test_synthesis_sees_prior_answer_for_follow_up_comparison(self):
        self.session.turn('第一次')
        self.planner.value = make_plan(summarize=True)
        self.session.turn('与上次结果对比')
        self.assertEqual(self.planner.summary_history[-1]['question'], '第一次')
        self.assertIn('虚构任务', self.planner.summary_history[-1]['answer'])

    def test_previous_turns_provide_bounded_conversation_context(self):
        self.session.turn('第一次')
        self.session.turn('然后呢')
        self.assertEqual(self.planner.history[-1]['question'], '第一次')

if __name__ == '__main__': unittest.main()
