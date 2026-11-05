"""The custom-endpoint route as the app registers it: refusals, status codes and the allowed fields.

The commit helper has its own tests in test_f_roles.py. This file is the thin HTTP layer on top of
it, which is where the action-header refusal, the status mapping and the speech allowlist live.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'payload'))

import f_persona


CATALOG = {
    'base': 'https://api.example.test/v1',
    'models': {'custom/first': {'model_id': 'custom/first', 'api_model': 'first', 'context': None,
                                'prices': {'input': 1.0, 'output': 2.0}, 'json_mode': 'json_schema'}},
}
BODY = {'base_url': 'https://api.example.test/v1', 'key': 'sk-test-key',
        'models': [{'model_id': 'custom/first', 'api_model': 'first', 'json_mode': 'json_schema',
                    'prices': {'input': 1.0, 'output': 2.0}}]}


class CustomRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'brain').mkdir()
        (self.root / 'config').mkdir()
        shutil.copyfile(ROOT / 'config/llm_roles.json', self.root / 'config/llm_roles.json')
        patcher = mock.patch.object(f_persona, 'BRAIN_DIR', self.root / 'brain')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.g = {'app': Flask(__name__), 'request': request, 'jsonify': jsonify}
        f_persona.install_persona(self.g)
        self.client = self.g['app'].test_client()

    def post(self, body=None, headers=None):
        return self.client.post('/api/build_f/custom', json=BODY if body is None else body,
                                headers=headers if headers is not None else {'X-Build-F-Action': 'custom-provider'})

    def test_the_page_header_is_required(self):
        self.assertEqual(self.post(headers={}).status_code, 403)
        self.assertEqual(self.post(headers={'Origin': 'http://evil.test',
                                            'X-Build-F-Action': 'custom-provider'}).status_code, 403)

    def test_a_good_save_writes_both_documents(self):
        with mock.patch('f_openrouter.load_custom_catalog', return_value=CATALOG), \
             mock.patch('f_openrouter.fetch_custom_models', return_value=['custom/first']):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertTrue((self.root / 'config/custom_models.json').is_file())
        saved = json.loads((self.root / 'config/custom_models.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['endpoint']['base_url'], 'https://api.example.test/v1')
        self.assertIn('sk-test-key', (self.root / 'brain/.env').read_text(encoding='utf-8'))

    def test_busy_work_answers_409(self):
        busy = mock.MagicMock()
        busy.custom_write_guard.side_effect = RuntimeError('Wait for active conversation/model work')
        self.g['F_ROLES'] = busy
        with mock.patch('f_openrouter.load_custom_catalog', return_value=CATALOG), \
             mock.patch('f_openrouter.fetch_custom_models', return_value=['custom/first']):
            response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['ok'])

    def test_a_catalog_the_transport_refuses_answers_400(self):
        with mock.patch('f_openrouter.load_custom_catalog', side_effect=ValueError('Use the base ending in /v1')):
            response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertIn('/v1', response.get_json()['error'])
        self.assertFalse((self.root / 'config/custom_models.json').exists())

    def test_the_page_reads_the_blocked_state(self):
        response = self.client.get('/api/build_f/custom')
        self.assertEqual(response.status_code, 200)
        self.assertIn('blocked', response.get_json())


class SpeechAllowlistTests(unittest.TestCase):
    def test_the_proxy_forwards_blocked(self):
        # speech/api.py imports torch, so read the allowlist rather than importing the service.
        import ast
        tree = ast.parse((ROOT / 'speech/api.py').read_text(encoding='utf-8'))
        fields = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == '_CUSTOM_FIELDS':
                fields = ast.literal_eval(node.value)
        self.assertIsNotNone(fields)
        self.assertIn('blocked', fields)
        self.assertIn('key_present', fields)
