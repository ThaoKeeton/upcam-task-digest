"""Standalone-only private worker handlers for Teambition chat."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from teambition_readonly import Client
from delivery_guard import DeliveryBlocked
from .chat_delivery import ChatDelivery
from .chat_model import OpenAIChatClient
from .chat_query import ChatReadService
from .chat_session import ChatSession, ChatSessionError
from .collection_runtime import list_mcp_projects
from .dingtalk import DingTalkSender, _markdown_payload, signed_webhook
from .report_credentials import ReportCredentialGuard
from .secrets import mcp_connection_authorized


def send_chat_markdown(webhook, signing_secret, content, *, transport=None):
    """Send only through a separately admitted ChatDelivery callback; no @ targets."""
    payload = _markdown_payload('Teambition 工作助手', content)
    destination = signed_webhook(webhook, signing_secret, __import__('time').time_ns() // 1_000_000)
    response = (transport or DingTalkSender._http_transport)(destination, payload, 15)
    return isinstance(response, dict) and type(response.get('errcode')) is int and response['errcode'] == 0


class _FreshReader:
    """A new bounded MCP budget for each conversational query."""
    def __init__(self, service, settings, credentials):
        self.service, self.settings, self.credentials = service, settings, credentials

    def execute(self, plan):
        token = self.credentials['teambition_mcp_token']
        factory = self.service.client_factory
        if factory is Client:
            client = factory(self.settings['tenant_id'], credentials=lambda: token,
                             max_pages=100, max_requests=300, max_tasks=700,
                             max_records=50000, max_seconds=120,
                             max_response_bytes=32 * 1024 * 1024, concurrent_tasks=1)
        else:
            client = factory(self.settings['tenant_id'], credentials=lambda: token)
        discovery_factory = factory
        if factory is Client:
            def discovery_factory(tenant_id, **kwargs):
                return factory(tenant_id, **kwargs, max_pages=100, max_requests=300,
                               max_tasks=700, max_records=50000, max_seconds=120,
                               max_response_bytes=32 * 1024 * 1024)
        return ChatReadService(client, project_id=self.settings['project_id'],
            include_archived=self.settings['include_archived'],
            list_projects=lambda: list_mcp_projects(self.credentials, client_factory=discovery_factory)).execute(plan)


def _fingerprint(settings, credentials):
    # Never persist or return this digest. It only invalidates a draft if source,
    # model, destination or credentials change after the answer was displayed.
    scope = {key: settings.get(key) for key in ('tenant_id', 'project_id', 'include_archived',
             'openai_model', 'openai_reasoning_effort', 'openai_service_tier')}
    return hashlib.sha256(json.dumps([scope, credentials], ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).digest()


class ChatWorker:
    def __init__(self, service, *, planner_factory=None, reader_factory=None, sender=None):
        self.service = service
        self.planner_factory = planner_factory or self._planner
        self.reader_factory = reader_factory or self._reader
        self.sender = sender or send_chat_markdown
        self.session = self.identity = None
        self.recovery_preview = None

    @staticmethod
    def _planner(settings, credentials, guard):
        return OpenAIChatClient(credentials.get('openai_api_key', ''), settings['openai_model'],
                                settings['openai_reasoning_effort'], settings['openai_service_tier'], guard)

    def _reader(self, settings, credentials, guard):
        return _FreshReader(self.service, settings, credentials)

    def _load(self):
        from .worker_api import WorkerApiError
        settings = self.service.settings_store.load()
        credentials = self.service.secret_store.load()
        if not settings.get('tenant_id') or not settings.get('project_id'):
            raise WorkerApiError('请先在项目范围中保存企业和项目。')
        if not mcp_connection_authorized(credentials):
            raise WorkerApiError('请先保存 Teambition MCP Token 并确认本机连接授权。')
        if not credentials.get('openai_api_key', '').strip():
            raise WorkerApiError('请先在账户与模型中保存 OpenAI Key。')
        return settings, credentials

    def turn(self, payload):
        from .worker_api import WorkerApiError
        if type(payload) is not dict or set(payload) != {'message'}:
            raise WorkerApiError('聊天请求格式无效。')
        if not self.service._job_lock.acquire(blocking=False):
            raise WorkerApiError('已有日报、诊断或聊天任务运行中，请稍后重试。')
        try:
            # Invalidate the previous preview before configuration checks too.
            if self.session is not None:
                self.session.discard_draft()
            settings, credentials = self._load()
            identity = _fingerprint(settings, credentials)
            if self.session is None or self.identity != identity:
                guard = ReportCredentialGuard(credentials)
                self.session = ChatSession(self.planner_factory(settings, credentials, guard),
                                           self.reader_factory(settings, credentials, guard), guard)
                self.identity = identity
            return self.session.turn(payload['message'])
        except WorkerApiError:
            raise
        except ChatSessionError as exc:
            raise WorkerApiError(str(exc)) from None
        except Exception:
            raise WorkerApiError('聊天请求未完成；未发送钉钉。') from None
        finally:
            self.service._job_lock.release()

    def reset(self, payload):
        from .worker_api import WorkerApiError
        if payload != {}:
            raise WorkerApiError('清空聊天请求格式无效。')
        if self.session is not None:
            self.session.clear()
        self.session = self.identity = None
        self.recovery_preview = None
        return {'status': '聊天已清空；未发送钉钉。'}

    def resolve_preview(self, payload):
        """Read a non-secret snapshot before asking the user to confirm recovery."""
        not_resolved = {'state': 'not_resolved', 'status': '未解除发送阻断；请核对钉钉和本机配置。'}
        self.recovery_preview = None
        if payload != {} or not self.service._job_lock.acquire(blocking=False):
            return not_resolved
        guard = None
        try:
            credentials = self.service.secret_store.load()
            webhook = credentials.get('dingtalk_webhook', '')
            signed_webhook(webhook, 'local-format-check', 1)
            guard = ChatDelivery(self.service.data_dir / 'delivery_ledger.sqlite3',
                                 credentials.get('delivery_ledger_id'), persistent_storage_verified=True)
            row = guard.peek_unresolved(webhook)
            if row.get('state') == 'in_flight':
                return {'state': 'in_flight', 'status': '存在仍在发送或中断未确认的记录，暂不能解除；请勿重复发送。'}
            if row.get('state') == 'not_found':
                return {'state': 'not_found', 'status': '当前机器人没有可解除的未知发送记录。'}
            request_id, destination_hash = row.get('request_id'), row.get('destination_hash')
            content_hash, attempted_at = row.get('content_hash'), row.get('attempted_at_utc')
            parsed_id = uuid.UUID(request_id) if type(request_id) is str else None
            if (row.get('state') != 'unknown' or parsed_id is None or parsed_id.version != 4
                    or str(parsed_id) != request_id
                    or not isinstance(destination_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', destination_hash)
                    or not isinstance(content_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', content_hash)
                    or not isinstance(attempted_at, str)):
                return not_resolved
            when = datetime.fromisoformat(attempted_at.replace('Z', '+00:00'))
            if when.tzinfo is None or when.utcoffset() is None:
                return not_resolved
            ticket = str(uuid.uuid4())
            self.recovery_preview = (ticket, destination_hash, request_id, content_hash, attempted_at)
            return {'state': 'unknown', 'status': '请先核对这条未知发送记录。', 'preview_id': ticket,
                    'request_id': request_id, 'destination_label': destination_hash[:12],
                    'content_label': content_hash[:12], 'content_hash': content_hash,
                    'attempted_at_utc': when.astimezone(timezone.utc).isoformat(timespec='seconds')}
        except Exception:
            return not_resolved
        finally:
            if guard is not None:
                guard.close()
            self.service._job_lock.release()

    def resolve_send(self, payload):
        """Resolve only the exact user-previewed record and destination."""
        not_resolved = {'state': 'not_resolved', 'status': '未解除发送阻断；请核对钉钉和本机配置。'}
        if (type(payload) is not dict or set(payload) != {'confirmed', 'preview_id'}
                or payload['confirmed'] is not True or type(payload['preview_id']) is not str):
            return not_resolved
        if not self.service._job_lock.acquire(blocking=False):
            return not_resolved
        guard = None
        try:
            preview = self.recovery_preview
            self.recovery_preview = None  # one confirmation, even if persistence is uncertain
            if preview is None or payload['preview_id'] != preview[0]:
                return not_resolved
            credentials = self.service.secret_store.load()
            webhook = credentials.get('dingtalk_webhook', '')
            signed_webhook(webhook, 'local-format-check', 1)
            guard = ChatDelivery(self.service.data_dir / 'delivery_ledger.sqlite3',
                                 credentials.get('delivery_ledger_id'), persistent_storage_verified=True)
            current = guard.peek_unresolved(webhook)
            if (current.get('state') != 'unknown' or current.get('destination_hash') != preview[1]
                    or current.get('request_id') != preview[2] or current.get('content_hash') != preview[3]
                    or current.get('attempted_at_utc') != preview[4]):
                return not_resolved
            state = guard.resolve_unknown(webhook, preview[2], expected_content_hash=preview[3],
                                          expected_attempted_at_utc=preview[4])
            if state == 'resolved':
                return {'state': 'resolved', 'status': '已记录你的核对，允许后续新消息；原消息不会自动重发。'}
            if state == 'in_flight':
                return {'state': 'in_flight', 'status': '存在仍在发送或中断未确认的记录，暂不能解除；请勿重复发送。'}
            return not_resolved
        except DeliveryBlocked as exc:
            if exc.outcome == 'unknown':
                return {'state': 'unknown', 'status': '未能确认解除结果；不会自动重试。'}
            return not_resolved
        except Exception:
            return not_resolved
        finally:
            if guard is not None:
                guard.close()
            self.service._job_lock.release()

    def send(self, payload):
        """Only called by the separate native preview-and-confirm action."""
        not_sent = {'state': 'not_sent', 'status': '本次未发送；请重新查询、检查配置并再次预览。'}
        if type(payload) is not dict or set(payload) != {'draft_id', 'request_id'}:
            return not_sent
        if not self.service._job_lock.acquire(blocking=False):
            return not_sent
        guard = None
        admission_started = False
        unknown = {'state': 'unknown', 'status': '发送结果不明或存在未解决的发送记录；请先检查钉钉，程序不会自动重试。'}
        try:
            if self.session is None:
                return not_sent
            settings, credentials = self._load()
            if _fingerprint(settings, credentials) != self.identity:
                self.session.clear()
                self.session = self.identity = None
                return not_sent
            content = self.session.consume_draft(payload['draft_id'])
            self.service._validate_dingtalk_config_with(credentials)
            safety = ReportCredentialGuard(credentials)
            safety.require_safe(content)
            ready, _ = self.service._ensure_ledger(credentials)
            if not ready:
                return not_sent
            guard = ChatDelivery(self.service.data_dir / 'delivery_ledger.sqlite3',
                                 credentials.get('delivery_ledger_id'), persistent_storage_verified=True)
            webhook, secret = credentials['dingtalk_webhook'], credentials['dingtalk_signing_secret']
            def deliver(text):
                # Recheck exactly what the transport will see after claim admission.
                safety.require_safe(text)
                return self.sender(webhook, secret, text)
            admission_started = True
            outcome = guard.attempt(payload['request_id'], webhook, content, deliver)
            if outcome == 'confirmed':
                return {'state': 'confirmed', 'status': '钉钉机器人已确认接收；未自动提醒任何人。'}
            if outcome == 'unknown':
                return unknown
            return not_sent
        except Exception:
            return unknown if admission_started else not_sent
        finally:
            if guard is not None:
                guard.close()
            self.service._job_lock.release()
