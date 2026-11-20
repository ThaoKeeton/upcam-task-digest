"""MCP batch context binds a single protected token without persisting it."""

import unittest

from collection_batches import MemoryCheckpoint, _Coordinator
from report_contract import updated_selection
from teambition_readonly import Client, CollectionError


class McpBatchTokenTests(unittest.TestCase):
    def make_coordinator(self, credentials):
        checkpoint = MemoryCheckpoint()
        client = Client('synthetic-org', transport=lambda *a, **k: None,
                        credentials=credentials, checkpoint=checkpoint)
        coordinator = _Coordinator(client, 'synthetic-project', False, 10,
                                   updated_selection('2026-11-05'))
        return coordinator, checkpoint

    def test_token_is_bound_but_not_written_to_checkpoint(self):
        coordinator, checkpoint = self.make_coordinator(lambda: 'synthetic-secret-token')
        coordinator._start()
        self.assertEqual(coordinator.credentials, 'synthetic-secret-token')
        self.assertNotIn(b'synthetic-secret-token', checkpoint.context)

    def test_old_app_pairs_and_empty_tokens_are_not_credentials(self):
        for value in (('', ''), ('app', 'secret'), '', None):
            coordinator, _ = self.make_coordinator(lambda: value)
            with self.subTest(value_type=type(value).__name__), self.assertRaises(CollectionError):
                coordinator._start()
