"""In-memory chat orchestration; the model cannot invoke a sender."""
from __future__ import annotations

import copy
import json
import re
import uuid

from .chat_query import ChatQueryError, _validate_plan
from .chat_model import ChatModelError

MAX_QUESTION_BYTES = 16 * 1024
MAX_ANSWER_BYTES = 220 * 1024
MAX_CONTEXT_ANSWER_BYTES = 24 * 1024
MAX_CONTEXT_TURNS = 6


class ChatSessionError(ValueError):
    """Safe chat failure with no remote body or credentials."""


def _no_summary(text):
    value = text.casefold()
    return bool(re.search(r'(?:不要|不需要|无需|别|不必|不用|不想).{0,16}(?:整理|总结|摘要|归纳)', value)
                or re.search(r"(?:do not|don't|no)\s+(?:use\s+(?:openai|ai)\s+to\s+)?(?:summari[sz]e|summary)", value))


def _forbids_openai_planning(text):
    """Honor an explicit no-OpenAI request before sending even the question."""
    value = text.casefold()
    target = r'(?:openai|(?<![a-z])ai(?![a-z])|模型)'
    negative = r'(?:不要|不许|禁止|不允许|不得|不用|不使用|别用|别|无需)'
    no_data = re.search(negative + r'.{0,24}(?:数据|内容|结果|任务|项目).{0,16}(?:发给|发送|传给|提交|上传|发往).{0,8}' + target, value)
    chinese = re.search(negative + r'.{0,18}' + target, value)
    english = re.search(r"(?:do not|don't|never|no)\s+.{0,24}" + target, value)
    summary_only = re.search(negative + r'.{0,4}' + target + r'\s*(?:来|进行)?(?:整理|总结|摘要|归纳)', value)
    english_summary_only = re.search(r"(?:do not|don't|never)\s+(?:use\s+)?" + target + r'\s+(?:to\s+)?(?:summari[sz]e|organize)', value)
    return bool(no_data or ((chinese or english) and not (summary_only or english_summary_only)))


class ChatSession:
    """The caller must recreate this session whenever saved source/destination changes."""
    def __init__(self, planner, reader, credential_guard):
        self.planner, self.reader, self.guard = planner, reader, credential_guard
        self.history = []
        self._draft = None
        self._last_answer = None

    def clear(self):
        self.history.clear()
        self._draft = None
        self._last_answer = None

    def discard_draft(self):
        self._draft = None

    def consume_draft(self, draft_id):
        if (type(draft_id) is not str or self._draft is None or draft_id != self._draft[0]):
            raise ChatSessionError('草稿已失效，请重新查询；未发送。')
        content = self._draft[1]
        self._draft = None
        return content

    def turn(self, question):
        # A subsequent turn invalidates the old send target even if the new
        # request fails; a caller can never send a stale preview by accident.
        self._draft = None
        if (type(question) is not str or not question.strip()
                or len(question.encode('utf-8', errors='replace')) > MAX_QUESTION_BYTES):
            raise ChatSessionError('请输入不超过大小限制的聊天内容。')
        if _forbids_openai_planning(question):
            raise ChatSessionError('聊天模式需要 OpenAI 理解问题；按你的要求本次未调用 OpenAI、未查询 MCP，也未发送钉钉。')
        try:
            self.guard.require_safe(question)
            history = copy.deepcopy(self.history)
            self.guard.require_safe(history)
            plan = self.planner.plan(question, history)
            self.guard.require_safe(plan)
            required = {'operation', 'search', 'task_id', 'start', 'end', 'summarize', 'send_requested', 'reply'}
            if (type(plan) is not dict or set(plan) != required
                    or type(plan['summarize']) is not bool or type(plan['send_requested']) is not bool):
                raise ChatSessionError('OpenAI 查询计划格式无效，未执行操作。')
            if plan['operation'] == 'reply':
                if (any(plan[k] is not None for k in ('search', 'task_id', 'start', 'end'))
                        or type(plan['reply']) is not str or not plan['reply'].strip()):
                    raise ChatSessionError('OpenAI 查询计划格式无效，未执行操作。')
                answer = plan['reply']
                status = 'OpenAI 已理解聊天内容；未查询 MCP，未发送钉钉。'
            elif plan['operation'] == 'previous':
                if (any(plan[k] is not None for k in ('search', 'task_id', 'start', 'end', 'reply'))
                        or plan['summarize'] or self._last_answer is None):
                    raise ChatSessionError('没有可复用的上一条答复，请先查询。')
                answer = self._last_answer
                status = '已准备上一条答复；请预览并确认，未发送钉钉。'
            else:
                if plan['reply'] is not None:
                    raise ChatSessionError('OpenAI 查询计划格式无效，未执行操作。')
                query = {key: plan[key] for key in ('operation', 'search', 'task_id', 'start', 'end')}
                try:
                    _validate_plan(query)
                except ChatQueryError:
                    raise ChatSessionError('OpenAI 查询计划超出可用的只读范围；未执行操作。') from None
                source = self.reader.execute(query)
                if type(source) is not dict:
                    raise ChatSessionError('查询结果格式无效。')
                source = copy.deepcopy(source)
                source['chat_query'] = {**query, 'search_field': 'task_title' if query['search'] is not None else None}
                self.guard.require_safe(source)
                use_summary = plan['summarize'] and not _no_summary(question)
                if isinstance(source, dict) and source.get('kind') == 'choose_task':
                    use_summary = False
                if use_summary:
                    answer = self.planner.summarize(question, source, history)
                    conditions = []
                    if type(source.get('project_id')) is str:
                        conditions.append('项目 ID：' + source['project_id'])
                    if query['search'] is not None:
                        conditions.append('任务标题包含：' + query['search'])
                    if query['task_id'] is not None:
                        conditions.append('任务 ID：' + query['task_id'])
                    if query['start'] is not None or query['end'] is not None:
                        basis = '任务更新时间' if query['operation'] == 'tasks' else '动态时间'
                        conditions.append(basis + '：[' + (query['start'] or '不限') + '，' + (query['end'] or '不限') + ')')
                    if conditions:
                        answer += '\n\n检索条件：' + '；'.join(conditions)
                    if type(source.get('coverage')) is str:
                        answer += '\n\n来源范围：' + source['coverage']
                    status = 'OpenAI 已理解请求并整理只读查询结果；未发送钉钉。'
                else:
                    answer = json.dumps(source, ensure_ascii=False, indent=2, allow_nan=False)
                    status = 'OpenAI 已理解请求；显示 MCP 只读结果，未用 OpenAI 整理，未发送钉钉。'
            if type(answer) is not str or not answer.strip() or len(answer.encode('utf-8')) > MAX_ANSWER_BYTES:
                raise ChatSessionError('回答无效或超过显示上限，请缩小查询范围。')
            self.guard.require_safe(answer)
            context_answer = answer
            if len(answer.encode('utf-8')) > MAX_CONTEXT_ANSWER_BYTES:
                context_answer = '上次回答过长，未带入后续对话上下文。'
                status += ' 上次结果过长，不带入后续对话。'
            self._last_answer = answer
            self.history.append({'question': question, 'answer': context_answer})
            if len(self.history) > MAX_CONTEXT_TURNS:
                del self.history[:-MAX_CONTEXT_TURNS]
                status += ' 后续仅保留最近六轮对话。'
            draft_id = str(uuid.uuid4())
            self._draft = (draft_id, answer)
            return {'answer': answer, 'status': status, 'draft_id': draft_id,
                    'send_requested': plan['send_requested']}
        except ChatSessionError:
            raise
        except ChatQueryError as exc:
            raise ChatSessionError(str(exc)) from None
        except ChatModelError as exc:
            try:
                self.guard.require_safe(str(exc))
            except Exception:
                raise ChatSessionError('聊天查询或安全检查未完成；未发送钉钉。') from None
            raise ChatSessionError(str(exc)) from None
        except Exception:
            raise ChatSessionError('聊天查询或安全检查未完成；未发送钉钉。') from None
