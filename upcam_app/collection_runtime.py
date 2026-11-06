"""Construct the same bounded collection path for every desktop entry point."""

from pathlib import Path

from teambition_readonly import Client, CollectionError

from .collection_checkpoint import DpapiCollectionCheckpoint
from .config import collection_client_limits


def make_collection_client(settings, credentials, data_dir, *, client_factory=Client,
                           checkpoint_factory=None):
    """Freeze explicit credentials; injected offline clients retain their narrow API.

    A batch collector owns the checkpoint after construction and closes it on
    both success and failure. Construction itself never opens the lazy store.
    """
    pair = tuple(credentials.get(key) for key in ("teambition_app_id", "teambition_app_secret"))
    if any(not isinstance(value, str) or not value for value in pair):
        raise CollectionError("Missing credentials or invalid token lifetime")
    explicit_credentials = lambda: pair
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
