# Teambition Chat Implementation Plan

> For agentic workers: implement and verify each task against its tests. Keep the daily-digest repair isolated.

**Goal:** Add an independent Teambition chat using OpenAI to plan bounded, read-only MCP requests and optionally draft a DingTalk message for explicit human confirmation.

**Architecture:** A worker-owned session stores only bounded in-memory conversation and the latest sendable answer. An OpenAI planner returns a strict bounded plan; local code validates each operation, executes only verified read-only MCP adapters in the saved project, and either displays the source or sends needed source data to OpenAI to answer. A separate durable send journal binds a confirmed request ID, destination and content hash; no model response can send. The WinUI page is developed separately and integrated by the parent.

**Tech stack:** Python 3.12 worker and standard-library HTTP/SQLite; existing MCP and Responses transport, WinUI 3.

**Spec:** User request: separate left navigation chat, not fixed daily report; OpenAI is the preferred planner, and webhook is optional and explicitly initiated by the user.

## Global Constraints

- Preserve daily-digest behavior, scheduling, templates, history, credentials, and delivery ledger.
- Use saved project/tenant and existing OpenAI model credentials; do not read real keys or send real webhooks in development.
- The planner can select only project/task/detail/activity read operations, never write tools, arbitrary URLs, arbitrary TQL, secrets, or webhook calls.
- Bound requests, pages, response bytes, turns, and planning steps; fail closed on malformed, incomplete, or ambiguous data.
- Explain OpenAI receives the chat for planning and may receive relevant source data; source contents are untrusted instructions.
- Explicit user confirmation sends only the exact current answer to the configured DingTalk robot, with no inherited mentions. Journal attempts before dispatch; unknown outcomes never auto-retry.
- Store no plaintext chat or business source data in normal logs or the send journal.

## Tasks

1. **Read-only query boundary:** Write failing tests for listing tasks, reading one task and activities, scope binding, budget failure, and rejecting arbitrary tool names/arguments; implement `ChatReadService.execute(plan)` using existing verified MCP adapters.
2. **OpenAI planner and session:** Write failing tests for request/response schema, untrusted source, conditional source display vs AI answer, multi-step execution bounds, absence of secret leakage, and planner failure/no send; implement `ChatSession.turn()` with injected planner/read service.
3. **Explicit guarded send:** Write failing tests for explicit draft/request identifiers, exact-content binding, unknown and interrupted outcomes, duplicate/concurrent sends, and no inherited mentions; implement separate journal and sender adapter.
4. **Worker integration:** Add `chat_turn`, `chat_reset`, `chat_send` while preserving existing handlers, authorize saved connection, validate active configuration per operation, and pass only sanitized results. Include synthetic-only tests for full flow.
5. **Verification:** Run targeted tests and the full Python suite; parent coordinates WinUI interface, native Windows build and integration review. Do not claim live verification or release until actually tested.
