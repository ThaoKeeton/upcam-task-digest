"""Recognize observed Teambition display notices without verifying code objects."""

import re


# These fields introduce a second payload, source claim or interpretation that
# the observed title/name envelope does not establish. Ordinary event identity,
# binding and time fields remain the collector's responsibility. Spellings with
# underscores or hyphens are equivalent for this rejection check only.
_PROVENANCE_FIELDS = frozenset({
    "provider", "providerid", "providertype", "source", "sourceid", "sourcetype", "sourceurl",
    "appid", "applicationid", "integration", "integrationid", "integrationtype", "provenance",
    "repository", "repositoryid", "repositoryurl", "repo", "repoid", "repourl",
    "commit", "commitid", "commits", "commithash", "commiturl", "sha", "shaid", "sha1", "sha256", "hash",
    "mergerequest", "mergerequestid", "mergerequesturl", "pullrequest", "pullrequestid", "pullrequesturl",
    "url", "href", "htmlurl", "weburl", "objectlink", "objecturl", "link", "links",
    "payload", "evidence", "customcreator", "title", "parsedpayload", "pattern",
    "sourcecreatorname", "sourceverified", "objectverified", "associationkind", "evidencescope", "needsreview",
})


def standard_association_notification(raw: dict) -> dict | None:
    """Parse only an observed display notification, retaining local text evidence.

    A matching notice establishes neither provider identity nor a real commit,
    merge request, merge result or task completion. The caller validates event
    identity, task binding and the report window before counting notifications.
    """
    if not isinstance(raw, dict) or raw.get("action") != "standard":
        return None
    if any(isinstance(key, str) and key.lower().replace("_", "").replace("-", "")
           in _PROVENANCE_FIELDS for key in raw):
        return None

    # Imported at call time so activity_adapter can expose this parser without
    # a module import cycle; these helpers retain its existing JSON/text guards.
    from activity_adapter import bounded_object, unsafe_controls, unsupported_markup

    try:
        data = bounded_object(raw.get("content"))
    except UnicodeError:
        return None
    if data is None or set(data) != {"title", "customCreator"}:
        return None
    title, creator = data["title"], data["customCreator"]
    if not isinstance(creator, dict) or set(creator) != {"name"}:
        return None
    name = creator["name"]
    for value in (title, name):
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            value.encode("utf-8")
        except UnicodeError:
            return None
        if unsafe_controls(value) or unsupported_markup(value):
            return None
    if (any(char in name for char in "\r\n\t") or "\t" in title
            or "\r" in title.replace("\r\n", "")):
        return None
    match = re.fullmatch(r"关联了(提交|合并请求) 「([\s\S]+)」", title)
    if match is None or not match[2].strip():
        return None
    return {
        "pattern": "association-display-notice",
        "association_kind": "commit" if match[1] == "提交" else "merge-request",
        "evidence_scope": "teambition-display-notice",
        "source_verified": False, "object_verified": False, "needs_review": False,
        "title": title, "source_creator_name": name, "parsed_payload": data,
    }
