"""Only fictional maps are injected; the real environment map is never inspected."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from digest_pipeline import build_digest, main
from runtime_mappings import load_runtime_mappings, parse_config_json, validate_mappings
from teambition_readonly import CollectionError

FIXTURES=Path(__file__).parent/'fixtures'
VAR='TEAMBITION_USER_MAP_JSON'


class RuntimeMappingTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((FIXTURES/'config.json').read_text())
        self.collection=json.loads((FIXTURES/'collection.json').read_text())

    def test_merge_exact_duplicates_and_distinct_ids_without_mutating_input(self):
        original=copy.deepcopy(self.config)
        supplied={'demo-person':'虚构成员','fictional-unused':'虚构未引用人员'}
        result=load_runtime_mappings(self.config,environ={VAR:json.dumps(supplied)})
        self.assertEqual(result['owner_names'],supplied)
        self.assertEqual(self.config,original)
        self.assertEqual(result['status_names'],self.config['status_names'])
        self.assertEqual(load_runtime_mappings(self.config,environ={}),self.config)

    def test_duplicate_json_keys_bad_types_and_empty_entries_fail_redacted(self):
        invalid=['','   ','{}','null','[]','true',
                 '{"fictional-private-id":"fictional-private-name","fictional-private-id":"fictional-private-name"}',
                 '{"fictional-private-id":""}','{"fictional-private-id":null}',
                 '{"fictional-private-id":123}','{"fictional-private-id":"  "}',
                 '{"fictional-private-id":" leading"}','{"bad id":"fictional-private-name"}',
                 '{"fictional-private-id":"name\\nline"}',
                 '{"fictional-private-id":"name\\u202e"}',
                 '{"fictional-private-id":NaN}','{"fictional-private-id":{"name":"nested"}}']
        for source in invalid:
            with self.subTest(kind=type(source).__name__),self.assertRaises(CollectionError) as error:
                load_runtime_mappings(self.config,environ={VAR:source})
            self.assertNotIn('fictional-private',str(error.exception))
            self.assertNotIn('leading',str(error.exception))

    def test_conflicting_sources_do_not_override(self):
        original=copy.deepcopy(self.config)
        with self.assertRaises(CollectionError) as error:
            load_runtime_mappings(self.config,environ={VAR:'{"demo-person":"fictional-conflict-name"}'})
        self.assertNotIn('fictional-conflict-name',str(error.exception))
        self.assertEqual(self.config,original)

    def test_config_json_duplicate_keys_rejected_at_all_levels(self):
        for source in ('{"owner_names":{"a":"first","a":"second"}}',
                       '{"status_names":{"s":"first","s":"second"}}',
                       '{"status_names":{},"status_names":{}}'):
            with self.assertRaises(CollectionError):parse_config_json(source)

    def test_explicit_state_mapping_requires_no_api_and_unknown_id_stays_review(self):
        config=load_runtime_mappings(self.config,environ={VAR:'{"demo-person":"虚构成员"}'})
        self.assertEqual(config['status_names']['demo-done'],'已解决')
        self.collection['records'][0]['task']['executorId']='fictional-unknown-user'
        self.collection['records'][0]['task']['tfsId']='fictional-unknown-status'
        report=build_digest(self.collection,config,'2026-01-02')
        self.assertEqual(report['digest']['tasks'][0]['owner'],'待复核')
        self.assertEqual(report['digest']['tasks'][0]['status'],'待复核')
        self.assertFalse(report['ready_to_send'])
        absent=dict(config,status_names={},resolved_status_ids=[],owner_names={})
        self.assertFalse(build_digest(self.collection,absent,'2026-01-02')['ready_to_send'])

    def test_invalid_explicit_state_configuration_fails_closed(self):
        for values in ({'status_names':{'s':''}},{'status_names':[]},
                       {'resolved_status_ids':['missing']},
                       {'resolved_status_ids':['demo-done','demo-done']},
                       {'resolved_status_ids':'demo-done'}):
            with self.subTest(values=values),self.assertRaises(CollectionError):
                validate_mappings(dict(self.config,**values))

    def test_live_entry_loads_only_fake_env_map_and_never_serializes_complete_map(self):
        stdout=io.StringIO()
        fake_map={'demo-person':'虚构成员','fictional-unused-id':'fictional-unused-name'}
        with patch('sys.argv',['digest_pipeline.py','--config',str(FIXTURES/'config.json'),
                               '--collect','--date','2026-01-02']), \
             patch('runtime_mappings.os.environ',{VAR:json.dumps(fake_map)}), \
             patch('digest_pipeline.Client') as client,patch('sys.stdout',stdout):
            client.return_value.collect.return_value=self.collection
            self.assertEqual(main(),2)
        report=json.loads(stdout.getvalue())
        self.assertEqual(report['digest']['tasks'][0]['owner'],'虚构成员')
        self.assertNotIn('owner_names',stdout.getvalue())
        self.assertNotIn('fictional-unused-id',stdout.getvalue())
        self.assertNotIn('fictional-unused-name',stdout.getvalue())
        self.assertNotIn(VAR,stdout.getvalue())

    def test_invalid_env_blocks_before_collection_without_leaking(self):
        stderr=io.StringIO();stdout=io.StringIO()
        with patch('sys.argv',['digest_pipeline.py','--config',str(FIXTURES/'config.json'),
                               '--collect','--date','2026-01-02']), \
             patch('runtime_mappings.os.environ',{VAR:'{"demo-person":"fictional-confidential-conflict"}'}), \
             patch('digest_pipeline.Client') as client,patch('sys.stderr',stderr),patch('sys.stdout',stdout):
            self.assertEqual(main(),1)
            client.assert_not_called()
        self.assertEqual(stdout.getvalue(),'')
        self.assertNotIn('fictional-confidential',stderr.getvalue())
        self.assertNotIn('demo-person',stderr.getvalue())

    def test_fixture_cli_does_not_read_environment_map(self):
        with patch('sys.argv',['digest_pipeline.py','--config',str(FIXTURES/'config.json'),
                               '--fixture',str(FIXTURES/'collection.json'),'--date','2026-01-02']), \
             patch('digest_pipeline.load_runtime_mappings',side_effect=AssertionError('must not read environment')), \
             patch('sys.stdout',io.StringIO()):
            self.assertEqual(main(),2)


if __name__=='__main__':unittest.main()
