"""The custom-endpoint route as the app registers it: refusals, status codes and the allowed fields.

The commit helper has its own tests in test_f_roles.py. This file is the thin HTTP layer on top of
it, which is where the action-header refusal, the status mapping and the speech allowlist live.
"""
import contextlib
import json
import os
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
        env = mock.patch.dict(os.environ, {}, clear=False)   # a save sets the key; put it back after
        env.start()
        self.addCleanup(env.stop)
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

    def test_a_blocked_endpoint_refuses_a_save_without_a_key(self):
        self.g['_custom_unavailable'] = 'llm_roles.json: access denied'
        with mock.patch('f_openrouter.load_custom_catalog', return_value=CATALOG), \
             mock.patch('f_openrouter.fetch_custom_models', return_value=['custom/first']):
            response = self.post(body={'base_url': 'https://api.example.test/v1', 'key': '',
                                       'models': BODY['models']})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Enter the key', response.get_json()['error'])
        self.assertFalse((self.root / 'config/custom_models.json').exists())

    def manager_pair(self, base, key):
        manager = mock.MagicMock()
        manager.custom_runtime.return_value = {'base': base, 'key': key}
        manager.custom_write_guard.return_value = contextlib.nullcontext()
        self.g['F_ROLES'] = manager
        return manager

    def test_a_new_endpoint_cannot_reuse_the_saved_key(self):
        self.manager_pair('https://old.test/v1', 'sk-old-provider')
        body = {'base_url': 'https://new.test/v1', 'key': '', 'models': BODY['models']}
        with mock.patch('f_openrouter.load_custom_catalog',
                        return_value={'base': 'https://new.test/v1', 'models': CATALOG['models']}), \
             mock.patch('f_openrouter.fetch_custom_models') as fetch:
            response = self.post(body=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Enter the key', response.get_json()['error'])
        fetch.assert_not_called()                      # the old key never reaches the new host
        self.assertFalse((self.root / 'config/custom_models.json').exists())

    def test_a_model_only_save_on_the_same_endpoint_still_reuses_the_saved_key(self):
        self.manager_pair('https://api.example.test/v1', 'sk-saved')
        self.g['http_requests'] = mock.MagicMock()
        body = {'base_url': 'https://api.example.test/v1', 'key': '', 'models': BODY['models']}
        with mock.patch('f_openrouter.load_custom_catalog', return_value=CATALOG), \
             mock.patch('f_openrouter.fetch_custom_models', return_value=['custom/first']) as fetch:
            response = self.post(body=body)
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][1], 'sk-saved')

    def test_a_dotenv_reference_is_refused_as_a_key(self):
        body = {'base_url': BODY['base_url'], 'key': '${OPENROUTER_API_KEY}', 'models': BODY['models']}
        with mock.patch('f_openrouter.load_custom_catalog', return_value=CATALOG), \
             mock.patch('f_openrouter.fetch_custom_models', return_value=['custom/first']):
            response = self.post(body=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('variable reference', response.get_json()['error'])
        self.assertFalse((self.root / 'config/custom_models.json').exists())

    def test_a_key_with_a_newline_never_reaches_the_test_call(self):
        body = {'base_url': BODY['base_url'], 'key': 'sk-bad\nsuffix'}
        with mock.patch('f_openrouter.fetch_custom_models') as fetch:
            response = self.client.post('/api/build_f/custom/test', json=body,
                                        headers={'X-Build-F-Action': 'custom-provider-test'})
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()
        self.assertNotIn('sk-bad', response.get_json()['error'])

    def test_the_test_route_refuses_a_saved_key_for_another_host(self):
        self.manager_pair('https://old.test/v1', 'sk-old-provider')
        body = {'base_url': 'https://new.test/v1', 'key': ''}
        with mock.patch('f_openrouter.fetch_custom_models') as fetch:
            response = self.client.post('/api/build_f/custom/test', json=body,
                                        headers={'X-Build-F-Action': 'custom-provider-test'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Enter the key', response.get_json()['error'])
        fetch.assert_not_called()

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
