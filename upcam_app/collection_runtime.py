"""Construct the same bounded collection path for every desktop entry point."""

from pathlib import Path
import unicodedata

from teambition_readonly import Client, CollectionError, identifier
from task_display import TaskDisplayError, validate_task_prefix

from .collection_checkpoint import DpapiCollectionCheckpoint
from .config import collection_client_limits
from .secrets import mcp_connection_authorized


def make_collection_client(settings, credentials, data_dir, *, client_factory=Client,
                           checkpoint_factory=None):
    """Freeze explicit credentials; injected offline clients retain their narrow API.

    A batch collector owns the checkpoint after construction and closes it on
    both success and failure. Construction itself never opens the lazy store.
    """
    token = credentials.get("teambition_mcp_token")
    if not isinstance(token, str) or not token.strip():
        raise CollectionError("Teambition MCP personal token is required")
    if not mcp_connection_authorized(credentials):
        raise CollectionError("Teambition MCP connection requires local authorization")
    explicit_credentials = lambda: token
    if client_factory is not Client:
        return client_factory(settings["tenant_id"], credentials=explicit_credentials)

    limits = collection_client_limits(settings)
    checkpoint = None
    try:
        factory = checkpoint_factory if checkpoint_factory is not None else DpapiCollectionCheckpoint
        checkpoint = factory(Path(data_dir) / "collection-progress.sqlite3")
        return client_factory(settings["tenant_id"], credentials=explicit_credentials,
                              checkpoint=checkpoint, **limits)
    except Exception as exc:
        if checkpoint is not None:
            try:
                checkpoint.close()
            except Exception:
                pass
        if isinstance(exc, CollectionError):
            raise
        raise CollectionError("Local collection checkpoint is unavailable") from None


def list_mcp_projects(credentials, *, client_factory=Client):
    """Return credential-safe project choices after the MCP client pagination checks."""
    if not mcp_connection_authorized(credentials):
        raise CollectionError("Teambition MCP connection requires local authorization")
    token = credentials["teambition_mcp_token"]
    client = client_factory(None, credentials=lambda: token, project_discovery=True)
    rows = client.list_projects()
    if not isinstance(rows, list) or len(rows) > 10000:
        raise CollectionError("Invalid or incomplete MCP project list")
    result, seen = [], {}
    secret_values = [value for value in credentials.values() if isinstance(value, str) and value]
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionError("Invalid or incomplete MCP project list")
        project, organization, name = identifier(row.get("id")), identifier(row.get("organizationId")), row.get("name")
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 512
                or any(unicodedata.category(char) in ("Cc", "Cf") for char in name)):
            raise CollectionError("Invalid or incomplete MCP project list")
        clean = {"id": project, "name": name, "organizationId": organization}
        prefix = row.get("uniqueIdPrefix")
        if prefix is not None and prefix != "":
            try:
                clean["uniqueIdPrefix"] = validate_task_prefix(prefix, allow_empty=False)
            except TaskDisplayError:
                raise CollectionError("Invalid MCP project task prefix") from None
        if any(secret in value for value in clean.values() for secret in secret_values):
            raise CollectionError("MCP project list contained sensitive credential data")
        if project in seen:
            if seen[project] != clean:
                raise CollectionError("Conflicting project discovery result")
            continue
        seen[project] = clean
        result.append(clean)
    return result
