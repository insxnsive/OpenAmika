"""Offline tests for the opt-in custom OpenAI-compatible transport.

No network, no keys: the HTTP layer is a fake, the catalog is a temp file.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'brain'))
from f_roles import NATIVE_KEYS
from f_openrouter import (CUSTOM, CustomClient, custom_base_url, custom_role_config, custom_cost, custom_price,
                          estimate_custom_reservation, fetch_custom_models, load_custom_catalog, make_custom_body,
                          normalize_custom_usage)

MESSAGES = [{'role': 'system', 'content': 'Native persona'}, {'role': 'user', 'content': 'Hello'}]
CATALOG = {'version': 1, 'provider': 'custom', 'endpoint': {'base_url': 'https://api.example.test/v1'},
           'models': [{'model_id': 'custom/glm-5.3-flash', 'api_model': 'glm-5.3-flash', 'context': 131072,
                       'prices': {'input': 1.0, 'output': 2.0, 'cache_read': 0.25}}]}


def write_catalog(payload):
    handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(payload, handle)
    handle.close()
    return handle.name


class Response:
    def __init__(self, data=None, status_code=200):
        self.data, self.status_code, self.closed = data, status_code, False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('http_' + str(self.status_code))

    def json(self):
        return self.data

    def close(self):
        self.closed = True


class HTTP:
    def __init__(self, response):
        self.response, self.posts, self.gets = response, [], []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.response


class BaseUrlTests(unittest.TestCase):
    def test_https_is_normalised(self):
        self.assertEqual(custom_base_url('https://api.example.test/v1'), 'https://api.example.test/v1')
        self.assertEqual(custom_base_url(' https://api.example.test/v1/ '), 'https://api.example.test/v1')

    def test_loopback_http_allowed_and_switched_off_by_the_caller(self):
        self.assertEqual(custom_base_url('http://localhost:8000/v1'), 'http://localhost:8000/v1')
        self.assertEqual(custom_base_url('http://127.0.0.1:1234/v1'), 'http://127.0.0.1:1234/v1')
        with self.assertRaises(ValueError):
            custom_base_url('http://localhost:8000/v1', allow_local_http=False)

    def test_plain_http_hosts_and_smuggling_are_refused(self):
        for bad in ('http://api.example.test/v1', 'https://user:pw@api.example.test/v1',
                    'https://api.example.test/v1?key=x', 'https://api.example.test/v1#frag',
                    'https://api.example.test/v2', 'https://api.example.test', '',
                    'https://api.example.test/v1 extra', 'http://10.0.0.5/v1', 'ftp://api.example.test/v1'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    custom_base_url(bad)


class CatalogTests(unittest.TestCase):
    def test_catalog_round_trip(self):
        catalog = load_custom_catalog(write_catalog(CATALOG))
        self.assertEqual(catalog['base'], 'https://api.example.test/v1')
        self.assertEqual(list(catalog['models']), ['custom/glm-5.3-flash'])
        self.assertEqual(catalog['models']['custom/glm-5.3-flash']['prices']['input'], 1.0)

    def test_catalog_rejects_missing_and_negative_prices(self):
        for prices in ({'input': 1.0}, {'input': 1.0, 'output': -1}, {'input': True, 'output': 1.0},
                       {'output': 1.0}, None):
            payload = json.loads(json.dumps(CATALOG))
            payload['models'][0]['prices'] = prices
            with self.subTest(prices=prices):
                with self.assertRaises(ValueError):
                    load_custom_catalog(write_catalog(payload))

    def test_catalog_rejects_mismatched_model_id_and_empty_models(self):
        payload = json.loads(json.dumps(CATALOG))
        payload['models'][0]['model_id'] = 'custom/other'
        with self.assertRaises(ValueError):
            load_custom_catalog(write_catalog(payload))
        empty = json.loads(json.dumps(CATALOG))
        empty['models'] = []
        with self.assertRaises(ValueError):
            load_custom_catalog(write_catalog(empty))

    def test_zero_prices_are_accepted_for_local_servers(self):
        payload = json.loads(json.dumps(CATALOG))
        payload['endpoint']['base_url'] = 'http://localhost:8000/v1'
        payload['models'][0]['prices'] = {'input': 0, 'output': 0}
        catalog = load_custom_catalog(write_catalog(payload))
        price = custom_price(catalog, 'custom/glm-5.3-flash')
        self.assertEqual(price['cache_read'], 0.0)
        tokens = {'total_input_tokens': 10, 'input_uncached_tokens': 10, 'cache_read_tokens': 0,
                  'cache_write_tokens': 0, 'output_tokens': 5, 'reasoning_tokens': 0, 'total_tokens': 15}
        self.assertEqual(custom_cost(price, tokens)[0], 0.0)


class RoleConfigTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_custom_catalog(write_catalog(CATALOG))

    def test_roles_get_their_own_caps(self):
        self.assertEqual(custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'surface')['max_tokens'], 1024)
        self.assertEqual(custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'deep')['max_tokens'], 4096)
        with self.assertRaises(ValueError):
            custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'surface', max_tokens=9000)

    def test_reasoning_is_refused_rather_than_dropped(self):
        with self.assertRaises(ValueError):
            custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'deep', reasoning_enabled=True)
        self.assertFalse(custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'deep',
                                            reasoning_enabled=False)['reasoning_enabled'])

    def test_memory_writer_needs_a_declared_schema(self):
        with self.assertRaises(ValueError):
            custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'memory_write')
        payload = json.loads(json.dumps(CATALOG))
        payload['models'][0]['json_mode'] = 'json_schema'
        catalog = load_custom_catalog(write_catalog(payload))
        config = custom_role_config(catalog, 'custom/glm-5.3-flash', 'memory_write')
        body = make_custom_body(config, MESSAGES, stream=False)
        self.assertEqual(body['response_format']['type'], 'json_schema')


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_custom_catalog(write_catalog(CATALOG))
        self.config = custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'surface')

    def test_body_uses_the_api_model_and_has_no_openrouter_routing(self):
        body = make_custom_body(self.config, MESSAGES)
        self.assertEqual(body['model'], 'glm-5.3-flash')
        self.assertNotIn('provider', body)
        self.assertEqual(body['max_tokens'], 1024)
        self.assertTrue(body['stream'])
        self.assertEqual(body['stream_options'], {'include_usage': True})
        self.assertNotIn('reasoning', body)

    def test_non_streaming_body_has_no_stream_options(self):
        self.assertNotIn('stream_options', make_custom_body(self.config, MESSAGES, stream=False))

    def test_schema_is_refused_when_the_endpoint_did_not_declare_it(self):
        with self.assertRaises(ValueError):
            make_custom_body(self.config, MESSAGES, json_schema={'type': 'object'})

    def test_reservation_is_positive_and_scales_with_output(self):
        body = make_custom_body(self.config, MESSAGES)
        small = estimate_custom_reservation(self.config, body)
        self.assertGreater(small, 0)
        bigger = custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'deep')
        self.assertGreater(estimate_custom_reservation(bigger, make_custom_body(bigger, MESSAGES)), small)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.price = {'input': 1.0, 'output': 2.0, 'cache_read': 0.25, 'source': 'person'}

    def test_cached_input_and_output_are_priced_at_the_person_rates(self):
        usage = {'prompt_tokens': 1200, 'completion_tokens': 500,
                 'prompt_tokens_details': {'cached_tokens': 200},
                 'completion_tokens_details': {'reasoning_tokens': 100}}
        normalized = normalize_custom_usage(usage, self.price)
        self.assertTrue(normalized['complete'])
        self.assertAlmostEqual(normalized['cost_usd'], (1000 * 1.0 + 200 * 0.25 + 500 * 2.0) / 1e6)
        self.assertEqual(normalized['tokens']['reasoning_tokens'], 100)

    def test_missing_counts_leave_the_cost_unknown(self):
        normalized = normalize_custom_usage({'prompt_tokens': 10}, self.price)
        self.assertFalse(normalized['complete'])
        self.assertIsNone(normalized['cost_usd'])
        self.assertIn('invalid_or_missing_completion_tokens', normalized['issues'])

    def test_missing_price_is_an_issue_not_a_zero(self):
        normalized = normalize_custom_usage({'prompt_tokens': 10, 'completion_tokens': 2}, None)
        self.assertIsNone(normalized['cost_usd'])
        self.assertIn('missing_price', normalized['issues'])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_custom_catalog(write_catalog(CATALOG))
        self.config = custom_role_config(self.catalog, 'custom/glm-5.3-flash', 'surface')
        self.response = {'id': 'req-1', 'model': 'glm-5.3-flash',
                         'usage': {'prompt_tokens': 1000, 'completion_tokens': 100,
                                   'prompt_tokens_details': {'cached_tokens': 0}},
                         'choices': [{'message': {'content': 'Actual speech'}, 'finish_reason': 'stop'}]}

    def client(self, payload=None):
        self.receipts, self.requests = [], []
        def reserve(request):
            self.requests.append(request)
            return 'reserved'
        http = HTTP(Response(self.response if payload is None else payload))
        client = CustomClient(http, 'not-a-real-key', reserve,
                              lambda handle, receipt: self.receipts.append(receipt),
                              base_url=self.catalog['base'])
        return client, http

    def test_completion_settles_at_the_person_rates(self):
        client, http = self.client()
        result = client.complete(self.config, MESSAGES)
        url, kwargs = http.posts[0]
        self.assertEqual(url, 'https://api.example.test/v1/chat/completions')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['model'], 'glm-5.3-flash')
        self.assertEqual(result['text'], 'Actual speech')
        self.assertEqual(self.receipts[0]['transport'], CUSTOM)
        self.assertAlmostEqual(self.receipts[0]['cost_usd'], 0.0012)
        self.assertLessEqual(self.receipts[0]['cost_usd'], self.requests[0]['estimated_max_usd'])

    def test_a_different_reported_model_is_flagged(self):
        payload = json.loads(json.dumps(self.response))
        payload['model'] = 'some-other-model'
        client, _ = self.client(payload)
        with self.assertRaises(Exception):
            client.complete(self.config, MESSAGES)
        self.assertIn('model_mismatch', self.receipts[0]['issues'])

    def test_embedding_is_refused(self):
        client, _ = self.client()
        with self.assertRaises(ValueError):
            client.embed(self.config, ['text'])

    def test_model_listing_is_one_read_only_get(self):
        http = HTTP(Response({'data': [{'id': 'b-model'}, {'id': 'a-model'}]}))
        self.assertEqual(fetch_custom_models(http, 'k', self.catalog['base']), ['a-model', 'b-model'])
        url, kwargs = http.gets[0]
        self.assertEqual(url, 'https://api.example.test/v1/models')
        self.assertFalse(kwargs['allow_redirects'])
        with self.assertRaises(Exception):
            fetch_custom_models(http, '', self.catalog['base'])


class RoleWiringTests(unittest.TestCase):
    """The role manager must accept a custom option without touching any other transport."""

    def setUp(self):
        import contextlib
        import shutil
        import threading
        from f_memory import MemoryRuntime
        from f_roles import NATIVE_KEYS, ROLES, install_roles
        from f_usage import UsageMeter
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        shutil.copyfile(ROOT / 'config' / 'openrouter_models.json', self.root / 'config' / 'openrouter_models.json')
        self.document = {'version': 1, 'options': {}, 'selections': {}}
        for role in ROLES:
            self.document['options'][role] = [
                {'key': 'custom:' + role, 'name': 'Custom ' + role, 'provider': 'custom',
                 'model_id': 'custom/glm-5.3-flash'}]
            self.document['selections'][role] = 'custom:' + role
        (self.root / 'config' / 'llm_roles.json').write_text(json.dumps(self.document), encoding='utf-8')
        self.memory = MemoryRuntime(self.root / 'memory_f' / 'live', lambda events, chunk: {'native': True},
                                    lambda texts, task: [[1.] + [0.] * 767 for _ in texts])
        self.meter = UsageMeter(self.root / 'memory_f' / 'usage.sqlite', {'models': {}}, budget_usd=1)
        natives = {key: {'name': key, 'provider': 'native', 'model': key} for key in NATIVE_KEYS.values()}
        self.g = {'AVAILABLE_MODELS': natives, '_ws_active_lock': threading.RLock(), '_ws_active': {},
                  '_thinking_inflight': 0, 'http_requests': object(), 'OPENROUTER_KEY': 'fake-key',
                  'CUSTOM_OPENAI_KEY': 'fake-key',
                  '_pick_stream': lambda p, c, **kw: iter(['native']),
                  '_openai_messages': lambda p, **kw: [],
                  '_extract_surface_fields': lambda raw: {'mode': 'say', 'status': 'ok'},
                  'parse_surface_response': lambda raw: [], 'parse_response': lambda raw: []}

        @contextlib.contextmanager
        def scope(phase, turn_id='', **kwargs):
            yield
        self.scope = scope
        self.install_roles = install_roles

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def install(self):
        return self.install_roles(self.g, self.root, self.memory, self.meter, self.scope, __import__('threading').local(), {})

    def write_catalog(self):
        payload = json.loads(json.dumps(CATALOG))
        payload['models'][0]['json_mode'] = 'json_schema'
        (self.root / 'config' / 'custom_models.json').write_text(json.dumps(payload), encoding='utf-8')

    def test_configured_option_reaches_the_custom_transport_and_its_rates(self):
        self.write_catalog()
        manager = self.install()
        option = manager.options['surface']['custom:surface']
        self.assertEqual(option['transport'], 'custom')
        self.assertEqual(option['config']['transport'], CUSTOM)
        rates = self.meter.pricing['models']['custom:custom/glm-5.3-flash']
        self.assertEqual(rates, {'input': 1.0, 'output': 2.0, 'cache_read': 0.25})

    def test_a_missing_catalog_keeps_the_provider_unusable(self):
        with self.assertRaises(ValueError):
            self.install()

    def test_a_listed_but_disabled_row_does_not_need_the_catalog(self):
        for role in list(self.document['options']):
            self.document['options'][role][0].update(disabled=True, disabled_reason='not configured')
            native = 'native:' + NATIVE_KEYS[role]
            self.document['options'][role].append({'key': native, 'native_key': NATIVE_KEYS[role], 'name': 'Native'})
            self.document['selections'][role] = native
        (self.root / 'config' / 'llm_roles.json').write_text(json.dumps(self.document), encoding='utf-8')
        manager = self.install()
        self.assertTrue(manager.options['surface']['custom:surface']['disabled'])



class SyncRowsTests(unittest.TestCase):
    """Saving a catalog rewrites only the custom rows and leaves the rest of the document alone."""

    def document(self):
        return {"version": 1, "fallback_preset": "Lean",
                "options": {role: [{"key": "or:" + role, "model_id": "deepseek/deepseek-v4-flash-0731"}] for role in
                            ("surface", "deep", "reflection", "memory_write")},
                "selections": {"surface": "or:surface", "deep": "or:deep", "reflection": "or:reflection",
                               "memory_write": "or:memory_write"},
                "presets": [{"name": "Lean", "selections": {"surface": "or:surface", "deep": "or:deep",
                                                            "reflection": "or:reflection",
                                                            "memory_write": "or:memory_write"}}]}

    def catalog(self, json_mode=None):
        payload = json.loads(json.dumps(CATALOG))
        payload['models'][0]['json_mode'] = json_mode
        return load_custom_catalog(write_catalog(payload))

    def test_rows_skip_the_writer_without_a_declared_schema(self):
        from f_roles import sync_custom_rows
        document = sync_custom_rows(self.document(), self.catalog())
        self.assertEqual([r['key'] for r in document['options']['surface'] if r.get('provider') == 'custom'],
                         ['surface:custom/glm-5.3-flash'])
        self.assertEqual([r for r in document['options']['memory_write'] if r.get('provider') == 'custom'], [])

    def test_a_schema_capable_catalog_also_writes_one_preset(self):
        from f_roles import sync_custom_rows
        document = sync_custom_rows(self.document(), self.catalog('json_schema'))
        custom_presets = [p for p in document['presets'] if p.get('provider') == 'custom']
        self.assertEqual(len(custom_presets), 1)
        self.assertEqual(set(custom_presets[0]['selections']), {'surface', 'deep', 'reflection', 'memory_write'})
        self.assertEqual(document['selections']['surface'], 'or:surface')

    def test_a_selection_on_a_removed_custom_row_falls_back(self):
        from f_roles import sync_custom_rows
        document = self.document()
        document['options']['surface'].append({'key': 'surface:custom/gone', 'provider': 'custom',
                                               'model_id': 'custom/gone'})
        document['selections']['surface'] = 'surface:custom/gone'
        document = sync_custom_rows(document, self.catalog())
        self.assertEqual(document['selections']['surface'], 'or:surface')



if __name__ == '__main__':
    unittest.main()
