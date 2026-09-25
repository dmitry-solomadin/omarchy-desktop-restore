"""Claude placement uses explicit, session-scoped metadata, never conversation text."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import agent_titles


class ClaudeTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.session = {'session': 'known-session', 'cwd': '/work/a_project.with spaces',
                        'agent_env': {'CLAUDE_CONFIG_DIR': str(self.home)}}
        project = re.sub(r'[^a-zA-Z0-9]', '-', self.session['cwd'])
        self.path = self.home / 'projects' / project / 'known-session.jsonl'
        self.path.parent.mkdir(parents=True)

    def record(self, title, kind='ai-title', session='known-session'):
        field = 'aiTitle' if kind == 'ai-title' else 'customTitle'
        return json.dumps({'type': kind, field: title, 'sessionId': session}) + '\n'

    def test_latest_title_in_custom_config_directory(self):
        self.path.write_text(self.record('Old') + self.record('Current') + self.record('Other', session='other'))
        self.assertEqual(agent_titles.claude_title(self.session), 'Current')

    def test_custom_title_takes_precedence_over_ai_title(self):
        self.path.write_text(self.record('Chosen name', 'custom-title') + self.record('Generated name'))
        self.assertEqual(agent_titles.claude_title(self.session), 'Chosen name')

    def test_missing_malformed_and_conversation_records_do_not_invent_titles(self):
        self.assertIsNone(agent_titles.claude_title(self.session))
        self.path.write_text('invalid json\n' + json.dumps({'type': 'user', 'content': '"ai-title"'}) + '\n'
                             + self.record('Wrong session', session='other'))
        self.assertIsNone(agent_titles.claude_title(self.session))

    def test_bounded_tail_discards_partial_record_and_ignores_old_title(self):
        self.path.write_text(self.record('Old') + 'x' * 1000 + '\n' + self.record('Recent'))
        with patch.object(agent_titles, 'TITLE_BYTES', 256):
            self.assertEqual(agent_titles.claude_title(self.session), 'Recent')
        self.path.write_text(self.record('Old') + 'x' * 1000)
        with patch.object(agent_titles, 'TITLE_BYTES', 256):
            self.assertIsNone(agent_titles.claude_title(self.session))

    def test_activity_indicator_is_removed_without_prefix_matching(self):
        for title in ('✳ Project work', '◑ Project work', 'Project work'):
            self.assertEqual(agent_titles.claude_label(title), 'Project work')
        self.assertEqual(agent_titles.claude_label('✳ Project…'), 'Project…')
        self.assertEqual(agent_titles.claude_label('Project work ✳'), 'Project work ✳')
