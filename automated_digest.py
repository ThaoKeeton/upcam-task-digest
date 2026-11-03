"""Compose an unattended digest with deterministic checks and AI summaries."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from digest_pipeline import build_digest
from openai_summarizer import OpenAIResponsesClient, SummaryError
from render_digest import TEMPLATE, render_digest
from runtime_mappings import validate_mappings


MAX_SUMMARY_CHARS = 200


class AutomatedDigestBlocked(RuntimeError):
    """Automated processing cannot safely produce a sendable report."""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_editorial_rules() -> list[str]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))["editorial_rules"]


def _summary_inputs(base_report: dict) -> tuple[list[dict], dict[str, str], dict[str, list[str]]]:
    by_task = {entry["task_id"]: entry for entry in base_report.get("summary_inputs", [])}
    digest_tasks = base_report["digest"]["tasks"]
    request_rows, row_to_task, substantive = [], {}, {}
    for index, task in enumerate(digest_tasks, 1):
        # Digest order and audit order are both stable, but IDs never go to the model.
        audit = base_report["audit"][index - 1]
        task_id = audit["task_id"]
        entry = by_task.get(task_id)
        if not entry:
            continue
        comments = [comment for comment in entry["comments"] if not comment.get("omit_from_summary")]
        texts = [comment.get("text") for comment in comments if isinstance(comment.get("text"), str) and comment["text"]]
        if not texts:
            continue
        task_key = f"task-{index}"
        request_rows.append({"task_key": task_key, "title": task["title"], "comment_bodies": texts})
        row_to_task[task_key] = task_id
        substantive[task_id] = texts
    return request_rows, row_to_task, substantive


def _base_failure_issues(report: dict) -> list[str]:
    hard_task_ids = {item["task_id"] for item in report.get("audit", []) if item.get("hard_review")}
    failures = []
    for issue in report.get("issues", []):
        if issue.startswith("任务 ") and issue.endswith(" 待复核"):
            task_id = issue[len("任务 "):-len(" 待复核")]
            if task_id not in hard_task_ids:
                continue
        failures.append(issue)
    return failures


def _apply_summaries(base_report: dict, summary_by_task: dict[str, str],
                     substantive: dict[str, list[str]]) -> dict:
    result = deepcopy(base_report)
    hard_task_ids = {item["task_id"] for item in result.get("audit", []) if item.get("hard_review")}
    result["issues"] = _base_failure_issues(result)
    audit_by_task = {entry["task_id"]: entry for entry in result.get("audit", [])}
    summary_by_input = {entry["task_id"]: entry for entry in result.get("summary_inputs", [])}
    for task, audit in zip(result["digest"]["tasks"], result["audit"]):
        task_id = audit["task_id"]
        if task_id in hard_task_ids:
            continue
        proposed = summary_by_task.get(task_id, "")
        if task_id in substantive and not proposed:
            raise AutomatedDigestBlocked("OpenAI omitted a nonempty comment summary; this digest was not sent")
        if (len(proposed) > MAX_SUMMARY_CHARS or any(ord(char) < 32 for char in proposed)
                or any(char in "\u2028\u2029" for char in proposed)
                or proposed != proposed.strip()):
            raise AutomatedDigestBlocked("Summary text failed local validation; this digest was not sent")
        pieces = [piece for piece in task.get("summary", "").split("；")
                  if piece != "评论内容语义归纳待复核" and not piece.startswith("原文摘录：")]
        if proposed:
            pieces.append(proposed)
        task["summary"] = "；".join(pieces)
        audit["needs_review"] = False
        if task_id in summary_by_input:
            summary_by_input[task_id]["semantic_summary"] = proposed
            summary_by_input[task_id]["needs_review"] = False
    result["markdown"] = render_digest(result["digest"])
    result["ready_to_send"] = not result["issues"]
    return result


def requires_openai_summary(collection: dict, config: dict, day, window=None) -> bool:
    """Preflight locally and report whether substantive comment text needs AI."""
    clean_config = validate_mappings(config)
    base = build_digest(collection, clean_config, day, window=window)
    if _base_failure_issues(base):
        raise AutomatedDigestBlocked("Automatic source or event validation failed; no text was sent to OpenAI")
    request_rows, _, _ = _summary_inputs(base)
    return bool(request_rows)


def build_automated_digest(collection: dict, config: dict, day, api_key: str, model: str, effort: str,
                           *, client: OpenAIResponsesClient | None = None, window=None,
                           generated_at: datetime | None = None,
                           service_tier: str = "default") -> dict:
    """Summarize the minimal local evidence, then locally validate before delivery."""
    clean_config = validate_mappings(config)
    base = build_digest(collection, clean_config, day, window=window)
    # Do not send task text to OpenAI when deterministic source, mapping, event,
    # or query validation has already produced a blocking issue.
    if _base_failure_issues(base):
        raise AutomatedDigestBlocked("Automatic source or event validation failed; no text was sent to OpenAI")
    request_rows, row_to_task, substantive = _summary_inputs(base)
    summary_by_task = {}
    if request_rows:
        try:
            generated = (client or OpenAIResponsesClient()).summarize(
                api_key, model, effort, request_rows, load_editorial_rules(),
                service_tier=service_tier)
        except SummaryError as exc:
            raise AutomatedDigestBlocked(str(exc)) from None
        summary_by_task = {row_to_task[key]: value for key, value in generated.items()}
    # Keep an explicit empty result for acknowledgement-only comment groups.
    for entry in base.get("summary_inputs", []):
        summary_by_task.setdefault(entry["task_id"], "")
    result = _apply_summaries(base, summary_by_task, substantive)
    result["report_type"] = "automated-v1"
    result["automation"] = {
        "source_sha256": fingerprint(collection),
        "config_sha256": fingerprint(clean_config),
        "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
        "summaries_sha256": fingerprint(summary_by_task),
        "model": model,
        "reasoning_effort": effort,
        "service_tier": service_tier,
        "generated_at_utc": (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "summary_count": len(summary_by_task),
    }
    result["automation"]["report_sha256"] = fingerprint(result)
    return result


def validate_automated_report(report: dict, collection: dict, config: dict,
                              current_model: str, current_effort: str,
                              current_service_tier: str = "default") -> None:
    """Verify report provenance, content, current rules and local data before delivery."""
    if not isinstance(report, dict) or report.get("report_type") != "automated-v1":
        raise AutomatedDigestBlocked("Only an automated report with current provenance can be sent")
    if report.get("ready_to_send") is not True or report.get("issues"):
        raise AutomatedDigestBlocked("Automated validation found unresolved issues")
    automation = report.get("automation")
    if (not isinstance(automation, dict) or automation.get("model") != current_model
            or automation.get("reasoning_effort") != current_effort
            or automation.get("service_tier") != current_service_tier):
        raise AutomatedDigestBlocked("Model settings changed after summary generation")
    if (automation.get("source_sha256") != fingerprint(collection)
            or automation.get("config_sha256") != fingerprint(validate_mappings(config))
            or automation.get("template_sha256") != hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()):
        raise AutomatedDigestBlocked("Source, settings or editorial rules changed after generation")
    copy = deepcopy(report)
    attested = copy["automation"].pop("report_sha256", None)
    if not isinstance(attested, str) or fingerprint(copy) != attested:
        raise AutomatedDigestBlocked("Automated report changed after generation")
    # Recompute deterministic data and bind the rendered output to the model summary.
    base = build_digest(collection, validate_mappings(config), report["digest"]["date"],
                        window=(report["scope"]["start"], report["scope"]["end_exclusive"]))
    request_rows, row_to_task, substantive = _summary_inputs(base)
    summaries = report.get("summary_inputs")
    if not isinstance(summaries, list):
        raise AutomatedDigestBlocked("Summary provenance is incomplete")
    by_task = {}
    for entry in summaries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("task_id"), str)
                or entry["task_id"] in by_task or not isinstance(entry.get("semantic_summary"), str)):
            raise AutomatedDigestBlocked("Summary provenance is incomplete")
        by_task[entry["task_id"]] = entry["semantic_summary"]
    comment_task_ids = {entry["task_id"] for entry in base.get("summary_inputs", [])}
    if (set(by_task) != comment_task_ids
            or fingerprint(by_task) != automation.get("summaries_sha256")):
        raise AutomatedDigestBlocked("Summary content does not match the local task evidence")
    if not request_rows and any(by_task.values()):
        raise AutomatedDigestBlocked("Unexpected summary text for acknowledgement-only comments")
    expected = _apply_summaries(base, by_task, substantive)
    if not expected["ready_to_send"] or expected["markdown"] != report.get("markdown"):
        raise AutomatedDigestBlocked("Rendered report differs from locally validated evidence")

