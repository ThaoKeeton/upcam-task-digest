"""Use Python's HTML entity parser to verify display escaping, without network."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import unicodedata

from digest_pipeline import build_digest
from render_digest import render_digest, text

FIXTURES=Path(__file__).parent/'fixtures'


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]

    def handle_data(self, data):
        self.parts.append(data)


def parse_entity_text(value):
    # A real standards-based parser performs one character-reference decode.
    # This isolates the entity interpretation at the Markdown/HTML display boundary;
    # it is not a claim of full DingTalk renderer compatibility.
    parser=TextParser()
    parser.feed('<span>'+value+'</span>')
    parser.close()
    return ''.join(parser.parts)


class EntityRenderingTests(unittest.TestCase):
    def test_entity_parser_exposes_original_injection(self):
        self.assertEqual(parse_entity_text('&rlm;&lrm;'), '\u200f\u200e')
        self.assertEqual(parse_entity_text('&lt;br&gt;'), '<br>')

    def test_display_escape_preserves_literal_entities_after_parsing(self):
        values=['&rlm;', '&lrm;', '&NewLine;', '&Tab;',
                '&lt;br&gt;', '&amp;rlm;', '&rlm;&lrm;&lt;br&gt;', 'A&B']
        for original in values:
            with self.subTest(original=original):
                displayed=parse_entity_text(text(original))
                self.assertEqual(displayed,original)
                self.assertFalse(any(unicodedata.category(c) in ('Cc','Cf') for c in displayed))

    @unittest.skipUnless(shutil.which('pandoc'), 'Optional CommonMark renderer not installed')
    def test_actual_commonmark_to_html_preserves_named_and_numeric_entities(self):
        for original in ('&rlm;', '&lrm;', '&#x202E;', '&#8207;', '&#x200f', '&lt;br&gt;',
                         '&amp;rlm;', '&NewLine;', '&Tab;', 'A&B', '**literal** &rlm;'):
            with self.subTest(original=original):
                rendered=subprocess.run(['pandoc','--from=commonmark','--to=html'],input=text(original),
                                        text=True,capture_output=True,check=True).stdout
                displayed=parse_entity_text(rendered).rstrip('\n')
                self.assertEqual(displayed,original)
                self.assertFalse(any(unicodedata.category(c) in ('Cc','Cf') for c in displayed))

    def test_all_task_text_fields_escape_entities(self):
        original='&rlm;&lrm;&lt;br&gt;'
        task={'category':'other','title':original,'summary':original,'owner':original,
              'status':original,'url':'https://example.com/task'}
        rendered=render_digest({'date':'2026-01-02','tasks':[task]})
        escaped='&amp;rlm;&amp;lrm;&amp;lt;br&amp;gt;'
        self.assertEqual(rendered.count(escaped),4)
        self.assertNotIn('\u200f',parse_entity_text(rendered))
        self.assertNotIn('\u200e',parse_entity_text(rendered))
        self.assertEqual(task['summary'],original)

    def test_pipeline_audit_retains_original_and_review_gate_stays_closed(self):
        data=json.loads((FIXTURES/'collection.json').read_text())
        config=json.loads((FIXTURES/'config.json').read_text())
        original='虚构正文 &rlm; &lrm; &lt;br&gt;'
        payload={'comment':original,'quote':'引用 &rlm;','renderMode':'text',
                 'mentions':{},'isOnlyNotifyMentions':False}
        raw=json.dumps(payload,ensure_ascii=False)
        data['records'][0]['activities'][0]['content']=raw
        report=build_digest(data,config,'2026-01-02')
        evidence=report['summary_inputs'][0]['comments'][0]
        self.assertEqual(evidence['raw_content'],raw)
        self.assertEqual(evidence['body'],original)
        self.assertEqual(evidence['text'],original)
        self.assertEqual(evidence['quote_evidence']['content'],'引用 &rlm;')
        self.assertEqual(report['audit'][0]['activity_evidence'][0]['content'],raw)
        self.assertIn('&amp;rlm; &amp;lrm; &amp;lt;br&amp;gt;',report['markdown'])
        self.assertFalse(report['ready_to_send'])
        self.assertTrue(evidence['semantic_review_required'])


if __name__=='__main__':unittest.main()
