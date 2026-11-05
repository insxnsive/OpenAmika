import contextlib
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'payload'))
from f_memory import MemoryRuntime
from f_roles import install_roles, ROLES, NATIVE_KEYS
from f_usage import UsageMeter
from test_f_openrouter import Response


class HTTP:
    def __init__(self):
        self.responses, self.calls = [], []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        shutil.copyfile(ROOT/'config/openrouter_models.json', self.root/'config/openrouter_models.json')
        self.document = {'version':1, 'options':{}, 'selections':{}}
        for role in ROLES:
            self.document['options'][role] = [
                {'key':'native:'+NATIVE_KEYS[role], 'native_key':NATIVE_KEYS[role], 'name':'Native'},
                {'key':'or:'+role, 'model_id':'deepseek/deepseek-v4-flash-0731', 'name':'Reviewed '+role}]
            self.document['selections'][role] = 'or:'+role
        self.save()
        self.memory = MemoryRuntime(self.root/'memory_f/live', lambda events, chunk: {'native':True},
                                    lambda texts, task: [[1.]+[0.]*767 for t in texts])
        self.meter = UsageMeter(self.root/'memory_f/usage.sqlite', {'models':{}}, budget_usd=1)
        self.http = HTTP()
        self.local = threading.local()
        self.g = {'AVAILABLE_MODELS':{key:{'name':key,'provider':'native','model':key} for key in NATIVE_KEYS.values()},
                  '_ws_active_lock':threading.RLock(), '_ws_active':{}, '_thinking_inflight':0,
                  'http_requests':self.http, 'OPENROUTER_KEY':'fake-key',
                  '_pick_stream':lambda p,c,**kw:iter(['native']),
                  '_openai_messages':lambda p,**kw:[{'role':'system','content':p.get('system','')},
                                                   {'role':'user','content':p['user_content']}],
                  '_extract_surface_fields':lambda raw:{'mode':'think' if 'mode: think' in raw else 'say','status':'ok'},
                  'parse_surface_response':lambda raw:[{'speech':raw.split('reply: ')[-1]}],
                  'parse_response':lambda raw:[{'speech':'hello' if 'hello' in raw else ''}]}
        @contextlib.contextmanager
        def scope(phase, turn_id='', **kwargs):
            yield
        self.scope = scope
        self.manager = None

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def save(self):
        (self.root/'config/llm_roles.json').write_text(json.dumps(self.document),encoding='utf-8')

    def install(self):
        self.manager = install_roles(self.g,self.root,self.memory,self.meter,self.scope,self.local,{})
        return self.manager

    def data(self, content, *, stream=False):
        usage={'prompt_tokens':100,'completion_tokens':20,'cost':.00001}
        if stream:
            return Response(events=[{'id':'gen-1','model':'deepseek/deepseek-v4-flash-0731','provider':'DeepInfra',
                                     'choices':[{'delta':{'content':content}}]},
                                    {'choices':[{'finish_reason':'stop','delta':{}}]},
                                    {'usage':usage,'choices':[]}, '[DONE]'])
        return Response(data={'id':'gen-1','model':'deepseek/deepseek-v4-flash-0731','provider':'DeepInfra',
                              'choices':[{'message':{'content':content},'finish_reason':'stop'}],'usage':usage})

    def test_install_has_no_calls_and_populates_defaults(self):
        manager=self.install()
        self.assertEqual(self.http.calls,[])
        self.assertEqual(self.g['DEFAULT_SURFACE_MODEL'],'or:surface')
        self.assertEqual(self.g['DEFAULT_DEEP_MODEL'],'or:deep')
        self.assertEqual(self.g['DEFAULT_THINKING_MODEL'],'or:reflection')
        self.assertEqual(set(manager.get()['options']),set(ROLES))

    def test_selection_is_whitelisted_atomic_and_persistent(self):
        manager=self.install()
        choices={role:'native:'+NATIVE_KEYS[role] for role in ROLES}
        manager.set(choices)
        self.assertEqual(self.g['DEFAULT_SURFACE_MODEL'],'grok-4.3-fast')
        self.assertEqual(json.loads((self.root/'config/llm_roles.json').read_text())['selections'],choices)
        with self.assertRaises(ValueError):
            manager.set(dict(choices, surface='arbitrary:model'))
        self.assertEqual(manager.get()['selections'],choices)

    def test_busy_conversation_and_memory_queue_reject_switch(self):
        manager=self.install()
        self.g['_ws_active']['turn']={}
        with self.assertRaises(RuntimeError):
            manager.set(manager.selections)
        self.g['_ws_active'].clear()
        self.memory.archive({'id':'x','time':'2026-09-26T10:00:00','user':'Remember Nessa','assistant':'Okay'})
        with self.assertRaises(RuntimeError):
            manager.set(manager.selections)

    def test_surface_uses_content_only_and_actual_meter_cost(self):
        self.install()
        self.http.responses.append(self.data('status: ok\nmode: say\nreply: hello',stream=True))
        text=''.join(self.g['_pick_stream']({'user_content':'Hi','_f_phase':'surface','_f_turn_id':'turn-a'},
                                           self.g['AVAILABLE_MODELS']['or:surface']))
        self.assertIn('reply: hello',text)
        summary=self.meter.summary()
        self.assertAlmostEqual(summary['priced_known_usd'],.00001)
        receipt=summary['recent_requests'][0]
        self.assertEqual(receipt['phase'],'surface')
        self.assertEqual(receipt['turn_id'],'turn-a')
        self.assertEqual(receipt['provider'],'openrouter')
        archived=json.loads(next((self.root/'memory_f/openrouter_receipts').glob('*.json')).read_text())
        self.assertIsNotNone(archived['timing']['first_speech_s'])

    def test_think_mode_filler_counts_as_surface_speech(self):
        # CONTRACT C14 (PromptOptimized): a think-mode filler is spoken, so it is first audible speech.
        self.install()
        self.http.responses.append(self.data('status: ok\nmode: think\nreply: private handoff',stream=True))
        list(self.g['_pick_stream']({'user_content':'Hi','_f_phase':'surface'},self.g['AVAILABLE_MODELS']['or:surface']))
        archived=json.loads(next((self.root/'memory_f/openrouter_receipts').glob('*.json')).read_text())
        self.assertIsNotNone(archived['timing']['first_speech_s'])

    def test_memory_writer_preserves_quote_validation_and_source_time(self):
        self.install()
        fact={'id':'one','text':'The user knows Nessa.','source_ids':['source1'],
              'quotes':[{'source_id':'source1','speaker':'user','quote':'I know Nessa'}],
              'date':'2026-09-26','source_kind':'user','entities':['Nessa'],'subject':'user',
              'predicate':'knows','value':'Nessa','status':'active','valid_from':None,'valid_to':None,
              'supersedes':[],'replaces_prior':False,'fact_type':'user_fact'}
        invalid=copy.deepcopy(fact)
        invalid['id']='two'
        invalid['quotes'][0]['quote']='Fabricated quote'
        self.http.responses.append(self.data(json.dumps({'facts':[fact,invalid]})))
        result=self.memory.extract_fn([{'id':'source1','time':'2026-09-26T10:00:00','user':'I know Nessa','assistant':'Fine.'}],'chunk01')
        self.assertEqual(len(result['facts']),1)
        self.assertEqual(result['facts'][0]['date'],'2026-09-26T10:00:00')
        self.assertEqual(result['facts'][0]['conversation_date'],'2026-09-26')
        self.assertEqual(len(result['rejected']),1)
        self.assertEqual(len(list((self.memory.folder/'quarantine').glob('*.json'))),1)
        self.assertTrue(self.http.calls[0][1]['json']['response_format']['json_schema']['strict'])

    def test_native_writer_kept_when_selected(self):
        manager=self.install()
        manager.set(dict(manager.selections,memory_write='native:claude-sonnet-5'))
        self.assertEqual(self.memory.extract_fn([], 'x'),{'native':True})
        self.assertEqual(self.http.calls,[])

    def test_embedding_openrouter_keeps_prefix_dimension_and_normalization(self):
        self.document['embedding_openrouter']=True
        self.document['embedding_endpoint']={'model_id':'google/gemini-embedding-2','provider_tag':'google-ai-studio',
            'provider_name':'Google AI Studio','usd_per_million_tokens':{'prompt':.2,'completion':0}}
        self.save()
        self.install()
        self.http.responses.append(Response(data={'model':'google/gemini-embedding-2',
            'data':[{'index':0,'embedding':[2.]+[0.]*767}],
            'usage':{'prompt_tokens':5,'completion_tokens':0,'cost':.000001}}))
        vectors=self.memory.embed_fn(['Who is Nessa?'],'query')
        self.assertEqual(vectors[0][0],1)
        body=self.http.calls[0][1]['json']
        self.assertEqual(body['dimensions'],768)
        self.assertEqual(body['input'],['task: search result | query: Who is Nessa?'])


if __name__=='__main__':
    unittest.main()

class CustomReloadTests(unittest.TestCase):
    """reload_custom swaps the custom rows without a restart, and refuses without half-applying."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        shutil.copyfile(ROOT / 'config/openrouter_models.json', self.root / 'config/openrouter_models.json')
        self.document = {'version': 1, 'options': {}, 'selections': {}}
        for role in ROLES:
            self.document['options'][role] = [
                {'key': 'native:' + NATIVE_KEYS[role], 'native_key': NATIVE_KEYS[role], 'name': 'Native'},
                {'key': 'or:' + role, 'model_id': 'deepseek/deepseek-v4-flash-0731', 'name': 'Reviewed ' + role}]
            self.document['selections'][role] = 'or:' + role
        self._save_document()
        self.memory = MemoryRuntime(self.root / 'memory_f/live', lambda events, chunk: {'native': True},
                                    lambda texts, task: [[1.] + [0.] * 767 for t in texts])
        self.meter = UsageMeter(self.root / 'memory_f/usage.sqlite', {'models': {}}, budget_usd=1)
        self.local = threading.local()

        @contextlib.contextmanager
        def scope(phase, turn_id='', **kwargs):
            yield
        self.g = {'AVAILABLE_MODELS': {key: {'name': key, 'provider': 'native', 'model': key}
                                       for key in NATIVE_KEYS.values()},
                  '_ws_active_lock': threading.RLock(), '_ws_active': {}, '_thinking_inflight': 0,
                  'http_requests': HTTP(), 'OPENROUTER_KEY': 'fake-key',
                  '_pick_stream': lambda p, c, **kw: iter(['native']),
                  '_openai_messages': lambda p, **kw: [{'role': 'system', 'content': p.get('system', '')},
                                                       {'role': 'user', 'content': p['user_content']}],
                  '_extract_surface_fields': lambda raw: {'mode': 'think' if 'mode: think' in raw else 'say',
                                                          'status': 'ok'},
                  'parse_surface_response': lambda raw: [{'speech': raw.split('reply: ')[-1]}],
                  'parse_response': lambda raw: [{'speech': 'hello' if 'hello' in raw else ''}]}
        self.manager = install_roles(self.g, self.root, self.memory, self.meter, scope, self.local, {})

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def _save_document(self):
        (self.root / 'config/llm_roles.json').write_text(json.dumps(self.document), encoding='utf-8')

    def _catalog(self, model):
        return {'base': 'https://api.example.test/v1',
                'models': {model: {'model_id': model, 'api_model': model.split('/')[1], 'context': None,
                                   'prices': {'input': 1.0, 'output': 2.0}, 'json_mode': 'json_schema'}}}

    def _publish(self, model):
        """Write the catalog and a role document naming it, the way a save does, then reload."""
        from f_roles import sync_custom_rows
        catalog = self._catalog(model)
        payload = {'version': 1, 'provider': 'custom',
                   'endpoint': {'base_url': catalog['base']},
                   'models': [dict(row, prices=row['prices']) for row in catalog['models'].values()]}
        (self.root / 'config/custom_models.json').write_text(json.dumps(payload), encoding='utf-8')
        self.document = sync_custom_rows(self.document, catalog)
        self._save_document()
        return self.manager.reload_custom()

    def _custom_keys(self):
        return sorted(key for role in ROLES for key, item in self.manager.options[role].items()
                      if item.get('transport') == 'custom')

    def test_a_save_is_visible_without_a_restart(self):
        self.assertEqual(self._custom_keys(), [])
        self._publish('custom/first')
        self.assertEqual(self._custom_keys(), ['deep:custom/first', 'memory_write:custom/first',
                                               'reflection:custom/first', 'surface:custom/first'])
        self.assertIn('custom/first', self.manager.custom['models'])

    def test_a_second_save_replaces_the_first(self):
        self._publish('custom/first')
        self._publish('custom/second')
        self.assertEqual(self._custom_keys(), ['deep:custom/second', 'memory_write:custom/second',
                                               'reflection:custom/second', 'surface:custom/second'])

    def test_the_endpoint_and_key_are_read_as_one_pair(self):
        self._publish('custom/first')
        with mock.patch.dict(os.environ, {'CUSTOM_OPENAI_API_KEY': 'key-one'}):
            self.assertEqual(self.manager.custom_runtime(),
                             {'base': 'https://api.example.test/v1', 'key': 'key-one'})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CUSTOM_OPENAI_API_KEY', None)
            with self.assertRaises(RuntimeError):
                self.manager.custom_runtime()

    def test_a_refused_reload_leaves_no_meter_rates_behind(self):
        self._publish('custom/first')
        rates_before = dict(self.meter.pricing['models'])
        # Rebuild the existing model at different rates, so the reload really does change the meter's
        # map, then fail afterwards on a row naming a model the catalog does not carry.
        payload = {'version': 1, 'provider': 'custom',
                   'endpoint': {'base_url': 'https://api.example.test/v1'},
                   'models': [{'model_id': 'custom/first', 'api_model': 'first', 'prices': {'input': 5.0, 'output': 6.0},
                               'json_mode': 'json_schema'}]}
        (self.root / 'config/custom_models.json').write_text(json.dumps(payload), encoding='utf-8')
        self.document['options']['surface'].append(
            {'key': 'surface:custom/missing', 'provider': 'custom', 'model_id': 'custom/missing', 'name': 'Missing'})
        self._save_document()
        with self.assertRaises(ValueError):
            self.manager.reload_custom()
        self.assertEqual(dict(self.meter.pricing['models']), rates_before)
        self.assertEqual(self.meter.pricing['models']['custom:custom/first']['input'], 1.0)
        self.assertNotIn('surface:custom/missing', self.manager.options['surface'])

    def test_a_save_refuses_while_memory_work_holds_its_lock(self):
        self._publish('custom/first')
        with self.manager.memory._worker_lock:
            with self.assertRaises(RuntimeError):
                with self.manager.custom_write_guard():
                    pass

    def test_a_client_build_waits_for_a_save_and_sees_the_new_pair(self):
        self._publish('custom/first')
        seen, started = {}, threading.Event()

        def read_runtime():
            started.set()
            seen['runtime'] = self.manager.custom_runtime()

        with mock.patch.dict(os.environ, {'CUSTOM_OPENAI_API_KEY': 'key-b'}):
            with self.manager.custom_write_guard():
                reader = threading.Thread(target=read_runtime)
                reader.start()
                self.assertTrue(started.wait(5))
                time.sleep(0.05)                      # let the reader reach the lock
                self.assertNotIn('runtime', seen)     # it cannot see the half-applied save
                payload = {'version': 1, 'provider': 'custom',
                           'endpoint': {'base_url': 'https://b.example.test/v1'},
                           'models': [{'model_id': 'custom/second', 'api_model': 'second',
                                       'prices': {'input': 1.0, 'output': 2.0}, 'json_mode': 'json_schema'}]}
                (self.root / 'config/custom_models.json').write_text(json.dumps(payload), encoding='utf-8')
                self.manager.custom = None
            reader.join(5)
        self.assertEqual(seen['runtime'], {'base': 'https://b.example.test/v1', 'key': 'key-b'})

    def test_a_catalog_the_manager_refuses_leaves_the_running_state_alone(self):
        self._publish('custom/first')
        before = self._custom_keys()
        before_base = self.manager.custom['base']
        # A row with no prices: the loader refuses it, so the reload must put everything back.
        payload = {'version': 1, 'provider': 'custom', 'endpoint': {'base_url': 'https://other.example.test/v1'},
                   'models': [{'model_id': 'custom/third', 'api_model': 'third'}]}
        (self.root / 'config/custom_models.json').write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.manager.reload_custom()
        self.assertEqual(self._custom_keys(), before)
        self.assertEqual(self.manager.custom['base'], before_base)

