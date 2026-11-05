"""Build F live integration. Only the isolated clone imports this module."""
from collections import OrderedDict
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from f_memory import MemoryRuntime
import methods as memory_methods
from f_usage import UsageMeter
from f_settle import Settler
from f_openrouter import StreamDrainer, drain_scope, write_discarded
from f_extract import SYSTEM, FACT_INSTRUCTION, parse_isolated_facts
import po_contract

APP_NAME='OpenAmika'
# Heading placed above the retrieved evidence in the deep and reflection
# prompts; the surface gets its own smaller rendering (B13) with no heading.
# Same language as the persona prompt by the owner's decision. The evidence is
# rendered as natural speech (f_method, CONTRACT 3.7), so the caveats that used
# to be repeated per record are stated once here: what I said back then proves
# nothing about him, later corrections win, and anything not recorded is not
# remembered. An empty cfg['memory_block_header'] switches it off.
DEFAULT_MEMORY_HEADER=('【记忆记录】下面是我记得的、和他以前的对话原话，以及从原话里记下的要点，前面写着大概是多久以前。'
    '我以前说过的话只代表我当时怎么说，不能证明他的事；说“后来被更正了”的，以更正后的说法为准。'
    '他问我是否还记得某件事、他有没有说过某件事、或者当初为什么那样说时，先查这些记录再回答：'
    '记录里有的就照记录说；记录和对话里都没有的，就老实说不记得，不要猜、不要编。')
# Recall-style follow-ups that should reach back to the last substantive user turn
# (word-bounded English; Chinese and Japanese forms; 听说过 "have heard of" excluded).
def history_window(all_turns,endpoint,keep=15,step=5,max_chars=20000):
    """Turns shown as recent history, ending at `endpoint` (exclusive index of the current turn).

    Step window (reports/cache_proposals.md P1, approved 2026-10-01): the window holds between
    `keep` and `keep+step` turns and its start moves only every `step` turns, so consecutive
    prompts share a byte-identical history prefix that the provider prefix cache can reuse
    (a sliding window moved the start on every turn and re-sent the whole history uncached).
    The character budget drops whole steps from the oldest end for the same reason; the
    newest turn is always kept."""
    keep=max(1,int(keep));step=max(1,int(step));n=min(len(all_turns),max(0,int(endpoint)))
    start=0 if n<=keep+step else step*(-(-(n-keep-step)//step))
    chosen=list(all_turns[start:n])
    def size(t):return len(t.get('user',''))+len(t.get('surface_reply',''))+len(t.get('deep_raw',''))+len(t.get('think_hint',''))
    while len(chosen)>1 and sum(size(t) for t in chosen)>max_chars:
        chosen=chosen[min(step,len(chosen)-1):]
    return chosen

_FOLLOWUP_CUE=re.compile(r"(?:\b(?:remember|recall|forg[eo]t(?:ten)?|told you|i (?:said|told|mentioned)|did i (?:say|tell|mention)|what did i (?:say|tell))\b"
    r"|记得|记不记得|(?<!听)说过|提过|讲过|聊过|忘了|忘记|覚えて|言った|話した|忘れ)",re.I)


# ------------------------------------------------------------------ telemetry
# TELEMETRY_SPEC §1.1: one `call_receipt` per metered model or embedding call, sent to
# the speech server on the brain's Socket.IO channel. The metering hooks run on the turn,
# drain, reflection and memory-writer threads right after a paid call settles, so they
# only build a small dict and put it on a bounded queue; one daemon thread emits. Nothing
# here may raise into the metering path, wait on the socket, or ship model text / keys.
_DaemonThread=threading.Thread  # bound at import: tests patch threading.Thread with an inline runner
TELEMETRY_QUEUE_MAX=500


def _epoch_ms(value):
    """UTC ISO timestamp (receipt/ledger started_at) -> epoch milliseconds; None if unusable."""
    if not value:return None
    try:
        moment=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (TypeError,ValueError):return None
    if moment.tzinfo is None:moment=moment.replace(tzinfo=timezone.utc)
    return int(round(moment.timestamp()*1000))


def _count(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


def _money(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


_EMBEDDING_PHASES=('query_embedding','memory_embedding')
# Ledger providers whose calls go through f_roles' transports (OpenRouter, Alibaba Model Studio):
# their call_receipt comes from F_OR_AFTER_CALL with stream timing, never from the meter listener.
_ROLE_TRANSPORTS=('openrouter','alibaba','ollama','custom')
_NULL_TOKENS={'input_tokens':None,'cached_tokens':None,'output_tokens':None}


def _token_fields(tokens,embedding=False,cache_reported=None):
    """Ledger-normalized counts (fallback when the raw usage is not at hand). Missing usage
    stays null (never 0): a cancelled stream has no usage record; an embedding has no output.
    cache_reported False (the ledger row's flag: the provider gave no cache figure, e.g. an
    embedding) leaves cached_tokens null, so the cache share reads "n/a", not 0 %."""
    if not isinstance(tokens,dict):return dict(_NULL_TOKENS)
    cached=None if cache_reported is False else _count(tokens.get('cache_read_tokens'))
    return {'input_tokens':_count(tokens.get('total_input_tokens')),'cached_tokens':cached,
            'output_tokens':None if embedding else _count(tokens.get('output_tokens'))}


def raw_token_fields(provider,raw,embedding=False):
    """input/cached/output from the provider's own usage fields (TELEMETRY_SPEC §1.2: missing
    values are null, never 0). The billing normalizers (f_openrouter/f_usage) count an absent
    key as 0, which would show a 0 % cache hit for a provider that does not report caching;
    here a field the provider did not report stays null. An embedding has no output (null)."""
    raw=raw if isinstance(raw,dict) else {}
    if isinstance(raw.get('usageMetadata'),dict):raw=raw['usageMetadata']
    elif isinstance(raw.get('usage'),dict):raw=raw['usage']
    if not raw:return dict(_NULL_TOKENS)
    provider=str(provider or '').lower()
    if provider=='anthropic':
        base=_count(raw.get('input_tokens'))
        cached=_count(raw.get('cache_read_input_tokens'))
        written=_count(raw.get('cache_creation_input_tokens'))
        if written is None and isinstance(raw.get('cache_creation'),dict):
            parts=[_count(v) for v in raw['cache_creation'].values()]
            written=sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
        inp=None if base is None else base+(cached or 0)+(written or 0)
        out=_count(raw.get('output_tokens'))
    elif provider in ('gemini','gemini_embedding'):
        inp=_count(raw.get('promptTokenCount'))
        cached=_count(raw.get('cachedContentTokenCount'))
        if cached is None and inp is not None and not embedding and provider=='gemini':
            cached=0        # a chat call: Gemini leaves cachedContentTokenCount out when nothing was cached
        candidates=_count(raw.get('candidatesTokenCount'))
        out=None if candidates is None else candidates+(_count(raw.get('thoughtsTokenCount')) or 0)
    else:  # OpenAI-compatible usage: OpenRouter, xAI, OpenAI, Groq, DeepSeek
        inp=_count(raw.get('prompt_tokens'))
        details=raw.get('prompt_tokens_details')
        cached=_count(details.get('cached_tokens')) if isinstance(details,dict) else None
        out=_count(raw.get('completion_tokens'))
    return {'input_tokens':inp,'cached_tokens':cached,'output_tokens':None if embedding else out}


def openrouter_call_receipt(handle,receipt,last_content_ms=None):
    """call_receipt from an OpenRouter client receipt (f_roles after_call). Timing values are
    perf-counter seconds from the request start, anchored on the receipt's UTC started_at.
    last_token_ms: timing.last_content_s when the client records it, else last_content_ms
    (epoch ms of the last content delta, noted by CallReceiptEmitter.on_event), else the
    settle time (elapsed_s: after the usage chunk / [DONE], the cancel, or the end of a drain).
    Token counts come from the raw usage: a field the provider did not report is null."""
    metadata=receipt.get('metadata') or {}
    timing=receipt.get('timing') or {}
    started=_epoch_ms(receipt.get('started_at'))
    def at(key):
        value=timing.get(key)
        if started is None or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return None
        return started+int(round(value*1000))
    first=at('first_content_s')
    settled=at('elapsed_s')
    last=at('last_content_s')
    if last is None and first is not None and _count(last_content_ms) is not None:
        # Kept inside [first token, settle time]: the two clocks differ by rounding only.
        last=max(last_content_ms,first)
        if settled is not None:last=min(last,settled)
    if last is None:last=settled
    phase=metadata.get('phase') or ''
    embedding=phase in _EMBEDDING_PHASES or 'embed' in str(receipt.get('requested_model') or '').lower()
    return {'turn_id':metadata.get('turn_id') or '','phase':phase,
            'status':receipt.get('status') or 'unknown',
            'model':receipt.get('actual_model') or receipt.get('requested_model'),
            'provider':receipt.get('actual_provider') or receipt.get('requested_provider_tag'),
            'started_ms':started,'first_token_ms':first,'last_token_ms':last,
            **raw_token_fields('openrouter',receipt.get('raw_usage'),embedding),'cost_usd':_money(receipt.get('cost_usd')),
            'request_id':receipt.get('request_id'),'open_turn_id':None,
            'ledger_id':handle,'route':receipt.get('transport') or 'openrouter'}


def ledger_call_receipt(row,marks=None):
    """call_receipt from a UsageMeter ledger receipt (native Anthropic/xAI/Gemini calls).
    marks = (first_ms, last_ms[, raw_usage]) recorded by the bridge just before meter.finish:
    wall-clock ms of the first/last streamed text, and the provider's raw usage (a token field
    the provider did not report stays null). Calls without streamed text (embeddings, native
    memory writes) have first_token_ms null and end at the ledger's finished_at."""
    marks=tuple(marks or ())
    first,last=(marks+(None,None))[:2]
    phase=row.get('phase') or ''
    embedding=phase in _EMBEDDING_PHASES
    if row.get('pricing_status')=='missing_usage':token_fields=dict(_NULL_TOKENS)
    elif len(marks)>=3 and isinstance(marks[2],dict):token_fields=raw_token_fields(row.get('provider'),marks[2],embedding)
    else:token_fields=_token_fields(row.get('tokens'),embedding,row.get('cache_reported'))
    return {'turn_id':row.get('turn_id') or '','phase':phase,'status':row.get('status') or 'unknown',
            'model':row.get('actual_model') or row.get('model'),'provider':row.get('provider'),
            'started_ms':_epoch_ms(row.get('started_at')),'first_token_ms':first,
            'last_token_ms':last if last is not None else _epoch_ms(row.get('finished_at')),
            **token_fields,'cost_usd':_money(row.get('cost_usd')),
            'request_id':row.get('provider_request_id'),'open_turn_id':None,
            'ledger_id':row.get('id'),'route':'native'}


class CallReceiptEmitter:
    """Bounded, non-blocking call_receipt delivery (TELEMETRY_SPEC §1.1).

    Hooks (on_openrouter, on_ledger) only build the payload and put_nowait it; a full
    queue drops the receipt (counted, logged at most every 30 s). Each ledger id is sent
    once (UsageMeter listeners also fire on a repeated finish()). The target is the sid
    that ran the receipt's turn (or the open turn it started), else webui's _LAST_SID;
    without a client the receipt is skipped. While a reflection runs, its receipts are
    held on a thread-local capture so the proactive turn it opens can be linked
    (open_turn_id on the receipt, origin on that turn's begin)."""

    def __init__(self,g,maxsize=TELEMETRY_QUEUE_MAX,enabled=True,log=None,broadcast=False):
        self.g,self.enabled=g,bool(enabled)
        # build_f.json call_receipts_broadcast (MekaHime_Live_2, 2026-10-09): deliver every receipt to
        # every connected client instead of the turn's own socket, so the speech server (and the
        # engine's Cost tab behind it) also sees Discord / DM / injected-cue turns run by the bot.
        self.broadcast=bool(broadcast)
        self.queue=queue.Queue(maxsize=max(1,int(maxsize)))
        self.log=log or (lambda message:print(message,flush=True))
        # Not f_bridge's `local`: scope() clears and restores that one on exit.
        self.local=threading.local()
        self._lock=threading.Lock()
        self._sids=OrderedDict()
        self._seen=OrderedDict()
        self._marks=OrderedDict()
        self._content=OrderedDict()   # OpenRouter generation id -> epoch ms of the last content delta
        self._gone=OrderedDict()      # disconnected Socket.IO sids
        self._thread=None
        self._last_drop_log=0.0
        self.counts={'queued':0,'emitted':0,'dropped':0,'no_client':0,'errors':0,'broadcast':0}

    # -------------------------------------------------------------- bookkeeping
    def _error(self,where,exc):
        self.counts['errors']+=1
        with contextlib.suppress(Exception):self.log(f'[telemetry] {where} failed: {type(exc).__name__}')

    def remember_sid(self,turn_id,sid):
        if not turn_id or not sid:return
        with self._lock:
            if sid in self._gone:return   # the client already left; late receipts go to _LAST_SID
            self._sids[turn_id]=sid
            self._sids.move_to_end(turn_id)
            while len(self._sids)>512:self._sids.popitem(last=False)

    def forget_sid(self,sid):
        """webui ws_disconnect: that Socket.IO client is gone (every browser reconnect gives the
        brain a new speech-server connection and sid). Turns it ran fall back to _LAST_SID, so a
        late receipt (memory writer, drain) reaches the current speech-server connection, which
        re-sends its stored rounds and ignores unknown turn ids."""
        if not sid:return
        try:
            with self._lock:
                self._gone[sid]=True
                while len(self._gone)>256:self._gone.popitem(last=False)
                for turn_id in [t for t,s in self._sids.items() if s==sid]:del self._sids[turn_id]
        except Exception as exc:self._error('forget sid',exc)

    def sid_for(self,record):
        with self._lock:
            for key in (record.get('open_turn_id'),record.get('turn_id')):
                if key and key in self._sids and self._sids[key] not in self._gone:return self._sids[key]
        sid=self.g.get('_LAST_SID')
        return None if sid in self._gone else sid

    def mark_native(self,ident,first_ms,last_ms,usage=None):
        """A native call's stream timing and raw usage, recorded just before its meter.finish()."""
        try:
            with self._lock:
                self._marks[ident]=(first_ms,last_ms,copy.deepcopy(usage) if isinstance(usage,dict) else None)
                while len(self._marks)>256:self._marks.popitem(last=False)
        except Exception as exc:self._error('timing mark',exc)

    def on_event(self,data):
        """F_OR_EVENT observer (runs on the stream/drain thread for every OpenRouter event):
        notes the wall-clock ms of the latest non-empty content delta per generation id, the
        stripe end of surface/deep/reflection (f_openrouter records only the first content
        time). A dict lookup and one clock read per chunk; never raises into the stream."""
        try:
            if not self.enabled or not isinstance(data,dict):return
            gen=data.get('id')
            if gen:self.local.generation=gen
            else:gen=getattr(self.local,'generation',None)
            choices=data.get('choices')
            if not gen or not choices or not isinstance(choices,list) or not isinstance(choices[0],dict):return
            delta=choices[0].get('delta')
            if not isinstance(delta,dict) or not delta.get('content'):return
            now=self.clock_ms()
            with self._lock:
                self._content[gen]=now
                self._content.move_to_end(gen)
                while len(self._content)>256:self._content.popitem(last=False)
        except Exception as exc:self._error('content mark',exc)

    def _first_time(self,ledger_id):
        if not ledger_id:return True
        with self._lock:
            if ledger_id in self._seen:return False
            self._seen[ledger_id]=True
            while len(self._seen)>4096:self._seen.popitem(last=False)
        return True

    # -------------------------------------------------------------------- hooks
    def on_openrouter(self,handle,receipt):
        """F_OR_AFTER_CALL observer: every OpenRouter call, stream, drain and embedding."""
        try:
            if not self.enabled:return
            gen=receipt.get('request_id') if isinstance(receipt,dict) else None
            with self._lock:last_content=self._content.pop(gen,None) if gen else None
            self.submit(openrouter_call_receipt(handle,receipt,last_content))
        except Exception as exc:self._error('openrouter receipt',exc)

    def on_ledger(self,row):
        """UsageMeter finish listener: native calls (OpenRouter/Alibaba rows come from on_openrouter)."""
        try:
            if not self.enabled or not isinstance(row,dict) or row.get('provider') in _ROLE_TRANSPORTS:return
            with self._lock:marks=self._marks.pop(row.get('id'),None)
            self.submit(ledger_call_receipt(row,marks))
        except Exception as exc:self._error('ledger receipt',exc)

    def submit(self,record):
        if not self._first_time(record.get('ledger_id')):return
        capture=getattr(self.local,'capture',None)
        if capture is not None:
            capture.append(record)
            return
        self.enqueue(record)

    def enqueue(self,record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.counts['dropped']+=1
            now=time.monotonic()
            if now-self._last_drop_log>30:
                self._last_drop_log=now
                with contextlib.suppress(Exception):self.log(f"[telemetry] queue full, dropped {self.counts['dropped']} receipts so far")
            return False
        self.counts['queued']+=1
        self._ensure_thread()
        return True

    def _ensure_thread(self):
        thread=self._thread
        if thread is not None and thread.is_alive():return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():return
            self._thread=_DaemonThread(target=self._run,daemon=True,name='F-telemetry')
            self._thread.start()

    def _run(self):
        while True:
            record=self.queue.get()
            try:self.emit(record)
            except Exception as exc:self._error('emit',exc)

    def emit(self,record):
        socketio=self.g.get('socketio')
        if self.broadcast and socketio is not None:
            socketio.emit('call_receipt',record)      # every client (see __init__)
            self.counts['emitted']+=1
            self.counts['broadcast']+=1
            return True
        sid=self.sid_for(record)
        if not sid or socketio is None:
            self.counts['no_client']+=1
            return False
        socketio.emit('call_receipt',record,to=sid)
        self.counts['emitted']+=1
        return True

    # ------------------------------------------------ reflection -> open turn link
    def begin_capture(self):
        previous=(getattr(self.local,'capture',None),getattr(self.local,'open_link',None))
        self.local.capture,self.local.open_link=[],None
        return previous

    def end_capture(self,previous):
        try:
            captured=getattr(self.local,'capture',None) or []
            link=getattr(self.local,'open_link',None)
        finally:
            self.local.capture,self.local.open_link=previous
        for record in captured:
            try:
                if link and record.get('ledger_id')==link[0]:record['open_turn_id']=link[1]
                if previous[0] is not None:previous[0].append(record)
                else:self.enqueue(record)
            except Exception as exc:self._error('reflection receipt',exc)

    def open_origin(self):
        """begin.origin for a proactive turn opened by the running reflection (or None)."""
        try:
            captured=getattr(self.local,'capture',None)
            record=next((r for r in reversed(captured or []) if r.get('phase')=='reflection'),None)
            if record is None:return None
            return {'kind':'reflection','receipt_turn_id':record.get('turn_id'),
                    'receipt_started_ms':record.get('started_ms'),'receipt_ledger_id':record.get('ledger_id')}
        except Exception as exc:
            self._error('open origin',exc)
            return None

    def note_open_turn(self,turn_id,sid=None,origin=None):
        try:
            self.remember_sid(turn_id,sid)
            if origin and getattr(self.local,'capture',None) is not None:
                self.local.open_link=(origin.get('receipt_ledger_id'),turn_id)
        except Exception as exc:self._error('open link',exc)

    @staticmethod
    def clock_ms():
        return int(time.time()*1000)

    def status(self):
        return {**self.counts,'enabled':self.enabled,'broadcast_mode':self.broadcast,'queue':self.queue.qsize(),'queue_max':self.queue.maxsize}


def install(g):
    root=Path(g['__file__']).resolve().parents[1]
    cfg=json.loads(po_contract.read_text_bom(root/'config/build_f.json'))
    pricing=json.loads(po_contract.read_text_bom(root/'config/pricing.json'))
    app_name=cfg.get('app',APP_NAME)
    g['APP_NAME']=app_name
    # MekaHime_Live_2: the writer prompt, the embedded-document label and speaker_name follow the character.
    persona=str(cfg.get('persona_name','Amika') or 'Amika')
    import f_extract as _f_extract, f_memory as _f_memory, f_method as _f_method
    for _mod in (_f_extract,_f_memory,_f_method,po_contract):_mod.PERSONA_NAME=persona
    g['F_PERSONA_NAME']=persona
    # R19: 我心想 lines stay only on the last few turns of the history window.
    g['HISTORY_THOUGHT_TURNS']=int(cfg.get('history_thought_turns',3))
    # TELEMETRY_SPEC §3: the history names the user and marks long gaps between turns.
    g['USER_NAME']=cfg.get('user_name','User') or 'User'
    g['CITY']=str(cfg.get('city','') or '').strip()   # {city} in her prompts and scenes (Setup → You & Amika)
    g['HISTORY_GAP_MINUTES']=int(cfg.get('history_gap_minutes',20))
    # P2: the deep of a speculative turn waits for the speech server's commit (webui._await_commit).
    g['DEEP_WAITS_FOR_COMMIT']=bool(cfg.get('deep_waits_for_commit',True))
    g['SPECULATIVE_DEEP_WAIT_S']=float(cfg.get('speculative_deep_wait_s',2.0))
    folder=root/'memory_f';folder.mkdir(exist_ok=True)
    meter=UsageMeter(folder/'usage.sqlite',pricing,budget_usd=cfg.get('test_budget_usd'),budget_scope=cfg.get('budget_scope','live'))
    try:
        # requests a previous (dead) process began but never finished must not count as "active" forever:
        # they blocked the memory wipe with "a model update is still active" (2026-11-01)
        stale=meter.sweep_stale()
        if stale:print(f"[usage] retired {len(stale)} stale request(s) that never finished")
    except Exception as error:
        print(f"[usage] stale sweep skipped: {error}")
    # TELEMETRY_SPEC §1.1: call_receipt per metered call (native calls via the meter's
    # finish listener here, OpenRouter calls via F_OR_AFTER_CALL after install_roles).
    telemetry=CallReceiptEmitter(g,enabled=bool(cfg.get('call_receipts',True)),
                                 broadcast=bool(cfg.get('call_receipts_broadcast',False)))
    g['F_TELEMETRY']=telemetry
    meter.add_finish_listener(telemetry.on_ledger)
    local=threading.local()
    http=g['http_requests']
    @contextlib.contextmanager
    def scope(phase,turn_id='',memory=None,cancel=None,memory_surface=None):
        old=local.__dict__.copy()
        local.phase,local.turn_id=phase,turn_id
        if memory is not None:local.memory=memory
        if memory_surface is not None:local.memory_surface=memory_surface
        if cancel is not None:local.cancel=cancel
        try:yield
        finally:local.__dict__.clear();local.__dict__.update(old)

    def start(provider,model,body):
        provider='gemini' if provider=='gemini_embedding' else provider
        # Owner decision 2026-10-09: Anthropic and xAI are disabled. f_roles publishes the
        # disabled providers (llm_roles.json); refuse before any reservation or HTTP.
        disabled=g.get('F_DISABLED_PROVIDERS') or {}
        if provider in disabled:
            reason=disabled.get(provider) if isinstance(disabled,dict) else None
            raise ValueError(f'{provider} calls are disabled: '+(reason or 'disabled by the owner'))
        # A byte is a conservative upper bound on text tokens. Keep requests
        # well below either provider's long-context pricing threshold.
        size=len(json.dumps(body,ensure_ascii=False).encode('utf-8'))+2048
        if size>120000:raise ValueError('Build F request exceeds 120 KB; narrow the input.')
        if provider not in ('anthropic','xai','gemini','local'):
            raise ValueError('Select the Build F Anthropic/xAI response models.')
        # Refuse an unpriced selection before contacting a provider, including
        # after the deployment-test cap has been removed.
        output_limit=body.get('max_tokens',0)
        if provider not in ('gemini','local') and (not isinstance(output_limit,int) or isinstance(output_limit,bool) or output_limit<=0):
            raise ValueError('Build F generation requires an explicit positive output limit.')
        upper=meter.estimate_reservation(provider,model,size,output_limit)
        return meter.begin(provider,model,getattr(local,'phase','other'),turn_id=getattr(local,'turn_id',''),estimated_max_usd=max(upper,0.0001))

    def json_call(provider,model,url,headers,body,phase):
        provider='gemini' if provider=='gemini_embedding' else provider
        with scope(phase,getattr(local,'turn_id','')):
            ident=start(provider,model,body)
            response=None
            try:
                response=http.post(url,headers=headers,json=body,timeout=90)
                response.raise_for_status()
                data=response.json()
                usage=data.get('usage') or data.get('usageMetadata')
                # Account before saving a response: a disk error must not erase
                # an already completed and paid provider request.
                # call_receipt: token fields the provider did not report stay null. Never
                # allowed to fail the paid call (mark_native swallows its own errors).
                tele=g.get('F_TELEMETRY')
                if tele is not None:tele.mark_native(ident,None,None,usage)
                meter.finish(ident,usage=usage,status='complete')
                path=folder/'responses';path.mkdir(exist_ok=True)
                (path/f'{ident}.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
                return data
            except Exception as exc:
                meter.finish(ident,status='unknown',error=exc)
                # Only the cause's type and HTTP status, never its text (URLs, bodies): memory jobs tell a
                # transient failure (f_memory.TRANSIENT_ERROR) from a rejected request by it.
                code=getattr(getattr(exc,'response',None),'status_code',None)
                cause=type(exc).__name__+(f' {code}' if isinstance(code,int) and not isinstance(code,bool) else '')
                raise RuntimeError(f'Build F provider request failed ({cause}); inspect usage status before retrying.') from None
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):response.close()

    def extract(events,chunk_id):
        rows=[{'source_id':e['id'],'date':e['time'],'source_kind':e.get('source_kind','mixed'),
               'speakers':{'user':e.get('user',''),'assistant':e.get('assistant','')}} for e in events]
        chunk={'id':'chunk-'+chunk_id,'events':rows}
        body={'model':'claude-sonnet-5','max_tokens':4096,'thinking':{'type':'disabled'},
              'system':_f_extract.writer_prompt(persona),'messages':[{'role':'user','content':json.dumps(rows,ensure_ascii=False,separators=(',',':'))}]}
        data=json_call('anthropic','claude-sonnet-5',g['ANTHROPIC_BASE'],g['_anthropic_headers'](),body,'memory_write')
        raw=''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')
        facts,rejected=parse_isolated_facts(raw,chunk)
        if rejected:
            quarantine=folder/profile/'quarantine';quarantine.mkdir(exist_ok=True)
            name=hashlib.sha256(chunk_id.encode()).hexdigest()
            (quarantine/f'{name}.json').write_text(json.dumps({'chunk_id':chunk_id,'source_ids':[e['id'] for e in events],'rejected':rejected},ensure_ascii=False),encoding='utf-8')
        times={e['id']:e['time'] for e in events}
        for fact in facts:
            fact['conversation_date']=fact['date']
            fact['date']=max(times[i] for i in fact['source_ids'])
        return {'facts':facts,'rejected':[{k:r.get(k) for k in ('index','id','reason')} for r in rejected]}

    # Embedding provider (2026-10-05): 'local' = embed_server.py on this box (BAAI/bge-m3 cut to 768 dims,
    # chosen by Amika_Embedding_Experiment), metered as provider "local" at zero price; 'gemini' = the
    # original Google call. The memory profile's embedding.json marker refuses a model switch without a
    # migration (migrate_embeddings.py), because query and document vectors must come from one model.
    embedding_provider=str(cfg.get('embedding_provider','gemini') or 'gemini').lower()
    embedding_url=(os.getenv('AMIKA_EMBEDDING_URL') or cfg.get('embedding_url') or 'http://127.0.0.1:8011').rstrip('/')
    embedding_model=cfg.get('embedding_model') or ('bge-m3-d768' if embedding_provider=='local' else 'gemini-embedding-2')
    g['F_EMBEDDING']={'provider':embedding_provider,'model':embedding_model,'url':embedding_url if embedding_provider=='local' else None}
    def _unit_vectors(raw,batch):
        if len(raw)!=len(batch):raise ValueError('Embedding count mismatch')
        output=[]
        for item in raw:
            v=item.get('values',[]) if isinstance(item,dict) else item
            if not isinstance(v,list) or len(v)!=768 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in v):raise ValueError('Invalid vector')
            norm=math.sqrt(sum(x*x for x in v))
            if not norm:raise ValueError('Zero vector')
            output.append([x/norm for x in v])
        return output
    def embed(texts,task_type):
        if not texts:return []
        phase='query_embedding' if task_type=='query' else 'memory_embedding'
        output=[]
        if embedding_provider=='local':
            for offset in range(0,len(texts),32):
                batch=texts[offset:offset+32]
                data=json_call('local',embedding_model,embedding_url+'/embed',{'Content-Type':'application/json'},
                               {'texts':batch,'task_type':task_type},phase)
                output.extend(_unit_vectors(data.get('embeddings',[]),batch))
            return output
        if not g.get('GEMINI_KEY'):raise RuntimeError('Embedding key unavailable')
        for offset in range(0,len(texts),16):
            batch=texts[offset:offset+16]
            body={'requests':[{'model':'models/gemini-embedding-2','outputDimensionality':768,
                    'content':{'parts':[{'text':('task: search result | query: '+t if task_type=='query' else 'title: Amika historical conversation | text: '+t)}]}} for t in batch]}
            data=json_call('gemini','gemini-embedding-2',
                 'https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents',
                 {'Content-Type':'application/json','x-goog-api-key':g['GEMINI_KEY']},body,phase)
            output.extend(_unit_vectors(data.get('embeddings',[]),batch))
        return output

    profile=cfg.get('memory_profile','live')
    if profile not in ('live','validation'):raise ValueError('Invalid memory profile')
    # TELEMETRY_SPEC §4 (smaller memory block): deep/reflection budget, and how many fact
    # groups / original-event candidates one retrieval considers.
    # memory_auto_retry (2026-10-10; absent = on with f_memory.AUTO_RETRY_DEFAULTS): transiently failed memory
    # jobs wait in "retry_wait" and the retry timer below resumes them through flush(); false = block as before.
    memory=MemoryRuntime(folder/profile,extract,embed,seed_path=root/'config/seed.json',budget_chars=cfg.get('memory_budget_chars',3600),
                         fact_packets=int(cfg.get('memory_fact_packets',4)),event_candidates=int(cfg.get('memory_event_candidates',5)),
                         embedding_model=embedding_model,auto_retry=cfg.get('memory_auto_retry',True),
                         persist_delay_s=float(cfg.get('memory_persist_delay_s',120)))
    for note in memory.startup_notes:print(f'[{app_name}] memory: {note}',flush=True)
    # E: numpy recall (build_f.json memory_numpy_recall, default on; false is the one-word revert).
    memory_methods.NUMPY_RECALL=bool(cfg.get('memory_numpy_recall',True)) and memory_methods._np is not None
    print(f'[{app_name}] memory recall: {"numpy" if memory_methods.NUMPY_RECALL else "per-item Python cosine"}',flush=True)
    g['F_MEMORY'],g['F_METER']=memory,meter
    # The memory writer runs on a flush thread with no turn; attribute its metered
    # memory_write / memory_embedding calls to the archived turn ('live-<turn_id>').
    def job_context(event_id):
        tid=event_id[5:] if isinstance(event_id,str) and event_id.startswith('live-') else ''
        return scope('memory_write',tid)
    memory.job_context=job_context
    # Cost settlement (f_settle, reports/settle.md): settles cancelled OpenRouter rows
    # by generation id, estimates the ones without an id, snapshots this key's usage.
    # Started with the boot-time resume below, never at import (tests stay offline).
    def openrouter_key():
        return g.get('OPENROUTER_KEY') or os.getenv('OPENROUTER_API_KEY')
    settler=Settler(meter,http,openrouter_key,enabled=bool(cfg.get('settlement_enabled',True)))
    g['F_SETTLER']=settler
    # Drain instead of abort (owner decision 2026-09-27): a cancelled turn's open
    # OpenRouter stream is read to its end by a daemon thread, content discarded
    # (receipts + memory_f/discarded/<turn_id>.json), usage recorded as "drained".
    def discarded_speech(phase,raw):
        try:
            if phase=='surface' and callable(g.get('parse_surface_response')):return g['parse_surface_response'](raw)[0].get('speech','')
            if phase=='deep' and callable(g.get('parse_response')):return g['parse_response'](raw)[0].get('speech','')
        except Exception:return None
        return None
    drainer=None
    if cfg.get('drain_cancelled_streams',True):
        drainer=StreamDrainer(deadlines=cfg.get('drain_deadlines_s'),max_per_phase=int(cfg.get('drain_max_per_phase',4)),
            on_finished=lambda receipt:write_discarded(folder/'discarded',receipt,
                speech=discarded_speech((receipt.get('drain') or {}).get('phase'),receipt.get('content',''))))
        meter.drain_worker=drainer.status
    g['F_OR_DRAINER']=drainer
    # One retrieval per turn, rendered twice (B13): the deep/reflection budget above
    # and a smaller surface budget. Perceived times use the brain's clock, i.e.
    # webui's datetime (a benchmark may freeze it) in its time zone.
    surface_budget=int(cfg.get('surface_memory_chars',2000))
    user_name=cfg.get('user_name','User')
    memory_language=cfg.get('memory_language','zh')
    def brain_now():
        clock=g.get('datetime') or datetime
        zone=g.get('LOCAL_TZ')
        return clock.now(zone) if zone is not None else clock.now()

    def stream(provider,parts,model,api_key=None,thinking_disabled=False,no_think=False,reasoning_effort=None):
        from f_usage import merge_usage
        if provider=='anthropic':
            body=g['_anthropic_body'](parts,model,thinking_disabled=thinking_disabled)
            body['stream']=True
            url,headers=g['ANTHROPIC_BASE'],g['_anthropic_headers'](api_key)
        elif provider=='xai':
            url,headers=g['_openai_headers'](provider,conv_id=parts.get('conv_id'))
            body={'model':model,'messages':g['_openai_messages'](parts,no_think=no_think,split_system=provider=='xai'),
                  'stream':True,'max_tokens':1024,'stream_options':{'include_usage':True}}
            if reasoning_effort:body['reasoning_effort']=reasoning_effort
        else:raise ValueError('Build F supports its configured Anthropic/xAI response models.')
        with scope(parts.get('_f_phase',getattr(local,'phase','other')),parts.get('_f_turn_id',getattr(local,'turn_id',''))):
            if getattr(local,'cancel',None) is not None and local.cancel.is_set():return
            ident=start(provider,model,body)
            usage={};complete=False;response=None;error=None
            # Wall-clock ms of the first and last streamed text (call_receipt timing).
            tele=g.get('F_TELEMETRY')
            clock=tele.clock_ms if tele is not None else (lambda:None)
            first_ms=last_ms=None
            try:
                response=http.post(url,headers=headers,json=body,timeout=90,stream=True)
                response.raise_for_status();response.encoding='utf-8'
                for line in response.iter_lines(decode_unicode=True):
                    if getattr(local,'cancel',None) is not None and local.cancel.is_set():break
                    if not line or not line.startswith('data:'):continue
                    raw=line[5:].strip()
                    if raw=='[DONE]':
                        if provider!='anthropic':complete=True
                        break
                    try:event=json.loads(raw)
                    except json.JSONDecodeError:continue
                    if provider=='anthropic':
                        kind=event.get('type')
                        if kind=='message_start':usage=merge_usage(usage,event.get('message',{}).get('usage',{}))
                        elif kind=='message_delta':usage=merge_usage(usage,event.get('usage',{}))
                        elif kind=='message_stop':complete=True;break
                        elif kind=='error':raise RuntimeError('Provider stream error')
                        elif kind=='content_block_delta':
                            text=event.get('delta',{}).get('text','')
                            if text:
                                last_ms=clock()
                                if first_ms is None:first_ms=last_ms
                                yield text
                    else:
                        if event.get('error'):raise RuntimeError('Provider stream error')
                        if event.get('usage'):usage=merge_usage(usage,event['usage'])
                        choices=event.get('choices') or []
                        if choices:
                            text=choices[0].get('delta',{}).get('content','')
                            if text:
                                last_ms=clock()
                                if first_ms is None:first_ms=last_ms
                                yield text
                if not complete and not (getattr(local,'cancel',None) is not None and local.cancel.is_set()):
                    raise RuntimeError('Provider stream ended without completion marker')
            except GeneratorExit:
                raise
            except Exception as exc:
                error=exc
                raise RuntimeError('Provider request failed; the usage panel records incomplete calls.') from None
            finally:
                try:
                    if tele is not None:tele.mark_native(ident,first_ms,last_ms,usage or None)
                    meter.finish(ident,usage=usage or None,status='complete' if complete else 'cancelled' if not error else 'unknown',error=error)
                finally:
                    if response is not None:
                        with contextlib.suppress(Exception):response.close()

    g['_stream_anthropic']=lambda parts,model,api_key=None,thinking_disabled=False:stream('anthropic',parts,model,api_key=api_key,thinking_disabled=thinking_disabled)
    g['_stream_openai']=lambda parts,model,provider,no_think=False,reasoning_effort=None:stream(provider,parts,model,no_think=no_think,reasoning_effort=reasoning_effort)
    def call_anthropic(parts,model,api_key=None,thinking_disabled=False):
        body=g['_anthropic_body'](parts,model,thinking_disabled=thinking_disabled)
        with scope(parts.get('_f_phase',getattr(local,'phase','other')),parts.get('_f_turn_id',getattr(local,'turn_id',''))):
            data=json_call('anthropic',model,g['ANTHROPIC_BASE'],g['_anthropic_headers'](api_key),body,getattr(local,'phase','other'))
        return ''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')
    g['_call_anthropic']=call_anthropic
    # All selected provider calls are metered. Prevent hidden unmetered routes.
    g['_stream_gemini']=lambda *a,**k:(_ for _ in ()).throw(ValueError('Select the Build F Anthropic/xAI response models.'))
    g['_call_gemini']=g['_stream_gemini']
    # The original non-streaming xAI call_llm path made its own POST, bypassing
    # both provider overrides above. Replace it too so fallback/API routes meter.
    def call_llm(user_message,model,provider,no_think=False):
        parts=g['build_messages'](user_message)
        if provider=='anthropic':return call_anthropic(parts,model)
        if provider!='xai':raise ValueError('Select the Build F Anthropic/xAI response models.')
        url,headers=g['_openai_headers'](provider,conv_id=parts.get('conv_id'))
        body={'model':model,'messages':g['_openai_messages'](parts,no_think=no_think,split_system=True),'max_tokens':1024}
        with scope(parts.get('_f_phase',getattr(local,'phase','other')),parts.get('_f_turn_id',getattr(local,'turn_id',''))):
            data=json_call(provider,model,url,headers,body,getattr(local,'phase','other'))
        choices=data.get('choices') or []
        if not choices:raise ValueError('Provider returned no answer')
        return choices[0].get('message',{}).get('content','')
    g['call_llm']=call_llm

    fill=g['_fill_deep_defaults']
    # A heading over the retrieved evidence (added 2026-09-26) tells the model
    # what the block is and to answer recall questions from it, or admit not
    # remembering, rather than guess. Kept in the persona's prompt language.
    memory_header=cfg.get('memory_block_header',DEFAULT_MEMORY_HEADER) or ''
    def framed(text):
        text=(text or '').strip()
        if not text or not memory_header:return text
        return memory_header+'\n'+text
    def memory_block():
        # The surface gets its own (smaller) rendering and no heading; deep and
        # reflection get the deep-budget rendering under the heading.
        if getattr(local,'role','')=='surface':
            surface=getattr(local,'memory_surface',None)
            return (getattr(local,'memory','') if surface is None else surface or '').strip()
        return framed(getattr(local,'memory',''))
    def fill_memory(template,**kwargs):
        if kwargs.get('memories_override') is None:kwargs['memories_override']=memory_block()
        return fill(template,**kwargs)
    g['_fill_deep_defaults']=fill_memory
    g['build_memories_block']=memory_block
    # Preserve nearby turns but prevent prompt growth after disabling compaction.
    # Step window (history_window): the start moves every history_window_step turns, not every turn.
    def recent_for(current):
        with g['_hist_lock']:
            all_turns=g['CONVERSATION_HISTORY']
            endpoint=next((i+1 for i,t in enumerate(all_turns) if t['turn_id']==current),len(all_turns))
            return history_window(all_turns,endpoint,keep=cfg.get('recent_turns',15),step=cfg.get('history_window_step',5),
                                  max_chars=cfg.get('recent_chars',20000))
    def recent():
        return recent_for(getattr(local,'turn_id',''))
    g['_unconsolidated_turns']=recent
    for name,phase in [('build_surface_messages','surface'),('build_messages','deep'),('build_thinking_messages','reflection')]:
        original=g[name]
        def builder(*args,_original=original,_phase=phase,**kwargs):
            # The role is visible to the memory framing while the prompt is built.
            had=hasattr(local,'role');previous=getattr(local,'role',None);local.role=_phase
            try:parts=_original(*args,**kwargs)
            finally:
                if had:local.role=previous
                else:
                    with contextlib.suppress(AttributeError):del local.role
            parts['_f_phase'],parts['_f_turn_id']=_phase,getattr(local,'turn_id','')
            return parts
        g[name]=builder

    run=g['_ws_run_generation']
    def generation(sid,turn_id,text,model_key,deep_model_key,entry):
        # call_receipts of this turn (and of a reflection on it) go to this client.
        telemetry.remember_sid(turn_id,sid)
        try:
            if entry['cancel'].is_set():return
            with g['_hist_lock']:
                turn=g['_find_turn'](turn_id)
                if turn is None:return
                original_user=(turn or {}).get('f_user_text',text)
                prev=[t for t in g['CONVERSATION_HISTORY'] if t['turn_id']!=turn_id and t.get('protected') and t.get('f_user_text')]
                # A reflection-initiated (open) turn searches with its own cue.
                query=original_user or entry.get('inject_cue','') or entry.get('open_cue','')
                # Follow-up references retain preceding user turns, not prior
                # assistant claims. This is a disclosed live query adaptation,
                # revised 2026-09-26: a short follow-up ("I told you", "do you not
                # remember?") anchors on the most recent substantive user turn
                # within a bounded lookback, plus the immediately preceding short
                # turn, so the topic survives a run of brief exchanges.
                if len(query)<cfg.get('followup_query_chars',160) and prev:
                    recent=prev[-max(1,cfg.get('followup_anchor_lookback',6)):]
                    anchor=recent[-1]
                    # Only a recall-style follow-up from the user reaches back to the
                    # last substantive user turn; other short inputs (and operator
                    # cues) keep the original one-turn rule so a topic change is not
                    # pulled toward an older subject.
                    if original_user and len(query.strip())<cfg.get('followup_reachback_max_chars',100) and _FOLLOWUP_CUE.search(query):
                        min_chars=cfg.get('followup_anchor_min_chars',40)
                        anchor=next((t for t in reversed(recent) if len(t['f_user_text'].strip())>=min_chars),recent[-1])
                    parts=[anchor['f_user_text']]
                    if recent[-1] is not anchor:parts.append(recent[-1]['f_user_text'])
                    query='\n'.join(parts+[query])
            # R16: events still visible in the recent history window are not
            # retrieved again (no backfill from lower ranks).
            window={'live-'+t['turn_id'] for t in recent_for(turn_id)}
            with scope('query_embedding',turn_id,cancel=entry['cancel']):
                result=memory.retrieve(query,exclude_ids=window,now=brain_now(),user_name=user_name,
                                       lang=memory_language) if query else {'text':'','source_ids':[],'diagnostics':{}}
            if entry['cancel'].is_set():return
            surface=memory.render(result['packets'],surface_budget) if result.get('packets') else {'text':''}
            diagnostics=dict(result.get('diagnostics',{}))
            # TELEMETRY_SPEC §4: the effective memory limits of this turn.
            diagnostics['memory_limits']={'memory_budget_chars':memory.budget_chars,'surface_memory_chars':surface_budget,
                                          'memory_fact_packets':memory.fact_packets,'memory_event_candidates':memory.event_candidates}
            if result.get('packets'):
                diagnostics['surface_render']={k:surface.get(k) for k in ('budget_chars','returned_chars','selected_record_ids','skipped_oversize_packet_ids')}
            with g['_hist_lock']:
                turn=g['_find_turn'](turn_id)
                if turn is None:return
                turn['f_memory_text']=result['text'];turn['f_sources']=result.get('source_ids',[])
                turn['f_retrieval']=diagnostics
            with scope('conversation',turn_id,result['text'],entry['cancel'],memory_surface=surface['text']),drain_scope(drainer):
                return run(sid,turn_id,text,model_key,deep_model_key,entry)
        except Exception as exc:
            g['socketio'].emit('error',{'turn_id':turn_id,'message':type(exc).__name__+': Build F could not prepare the turn.'},to=sid)
            g['socketio'].emit('end',{'turn_id':turn_id},to=sid)
            g['discard_turn'](turn_id)
        finally:
            with g['_ws_active_lock']:g['_ws_active'].pop(turn_id,None)
    g['_ws_run_generation']=generation
    thinking=g['run_thinking_once']
    def reflection(deep_request='',on_guidance=None,target_turn_id=None):
        with g['_hist_lock']:
            if target_turn_id is None:
                # Idle rhythm: no triggering turn; look back from the latest turn.
                history=g['CONVERSATION_HISTORY']
                turn=history[-1] if history else None
                scope_turn=turn['turn_id'] if turn else ''
            else:
                turn=g['_find_turn'](target_turn_id)
                if not turn:return {}
                scope_turn=target_turn_id
            text=(turn or {}).get('f_memory_text','')
        # The reflection's call_receipt is held until it returns, so a proactive turn it
        # opens can be linked both ways (open_turn_id / begin.origin). Emitted in finally.
        captured=telemetry.begin_capture()
        try:
            with scope('reflection',scope_turn,text),drain_scope(drainer):return thinking(deep_request,on_guidance,target_turn_id)
        finally:
            telemetry.end_capture(captured)
    g['run_thinking_once']=reflection
    # B12: the reflection's 开口 (a proactive spoken turn) and the idle rhythm are
    # restored as in MekaHime's earlier companion builds when build_f.json enables them. Open turns
    # are metered as deep calls and archived as evidence. Self-continuation,
    # startup catch-up and legacy consolidation stay off.
    auto_open=bool(cfg.get('auto_open_from_reflection',False))
    idle_reflection=bool(cfg.get('idle_reflection',False))
    g['F_AUTO_OPEN'],g['F_IDLE_REFLECTION']=auto_open,idle_reflection
    if not auto_open:g['_start_open_gen']=lambda *args,**kwargs:None
    elif callable(g.get('_start_open_gen')):
        start_open=g['_start_open_gen']
        def start_open_linked(sid,cue,*args,**kwargs):
            # Opened from a running reflection: its begin carries origin (TELEMETRY_SPEC §1.1).
            origin=telemetry.open_origin()
            if origin is not None and 'origin' not in kwargs:kwargs['origin']=origin
            tid=start_open(sid,cue,*args,**kwargs)
            if tid:telemetry.note_open_turn(tid,sid,origin)
            return tid
        g['_start_open_gen']=start_open_linked
    if not idle_reflection:g['_idle_watch_loop']=lambda:None
    g['_start_continue_gen']=lambda *args,**kwargs:None
    g['_startup_catchup']=lambda:None
    g['_auto_consolidation_loop']=lambda:None
    g['CONTINUE_ENABLED']=False
    idle_started=threading.Event()
    def start_idle_reflection():
        if not idle_reflection or idle_started.is_set():return False
        idle_started.set()
        threading.Thread(target=lambda:g['_idle_watch_loop'](),daemon=True,name='F-idle-reflection').start()
        return True
    g['f_start_idle_reflection']=start_idle_reflection

    worker_lock=threading.Lock()
    work_requested=threading.Event()
    def flush():
        work_requested.set()
        if not worker_lock.acquire(blocking=False):return
        try:
            while True:
                work_requested.clear()
                memory.flush_pending()
                if memory.status()['pending']:continue
                # B (f_memory.settle): the batch's one snapshot write waits for the turn in flight to end
                # (f_memory forces it after memory_persist_delay_s); new archives are picked up meanwhile.
                if not memory.settle():
                    work_requested.wait(2.0)
                    continue
                if not work_requested.is_set():break
        finally:
            worker_lock.release()
            # Close the small race where an archive arrived at worker shutdown.
            if work_requested.is_set():threading.Thread(target=flush,daemon=True).start()
    def archive_turn(tid):
        with g['_hist_lock']:
            t=g['_find_turn'](tid)
            if not t or not t.get('protected') or not t.get('f_generation_complete') or not t.get('f_speech_done') or t.get('interrupted') or t.get('inject_cue'):return
            # A reflection-initiated (open) turn has no user speech; it is archived
            # with user="" and its cue. Every other turn needs the user's words.
            open_cue=t.get('open_cue') or ''
            if (not open_cue and not t.get('f_user_text')) or t.get('f_archived'):return
            # C14: the surface line was spoken in every mode (say, say+think, and a
            # think-mode filler); the deep's repeat of it is not spoken twice (C19).
            surface=t.get('surface_reply','')
            deep=g['_parsed_deep'](t.get('deep_raw',''))[0]
            if surface and deep:deep=po_contract.dedupe_blurt(deep,surface)
            speech='\n'.join(x for x in [surface,deep] if x)
            if not speech.strip():return
            event={'id':'live-'+tid,'time':t['ts'],'user':'' if open_cue else t['f_user_text'],'assistant':speech,'source_kind':'conversation','cue':open_cue}
        # R21: the evidence snapshot is persisted outside the history lock, so a
        # multi-megabyte write never stalls the conversation. archive() is
        # idempotent for an identical event id.
        memory.archive(event)
        with g['_hist_lock']:
            t=g['_find_turn'](tid)
            if t is not None:t['f_archived']=True
        g['_save_history']()
        threading.Thread(target=flush,daemon=True,name='F-memory-write').start()
    g['f_archive_turn']=archive_turn
    # Operator re-dispatch (f_controls requeue) reuses the same bounded worker.
    g['f_memory_flush']=lambda:threading.Thread(target=flush,daemon=True,name='F-memory-requeue').start()
    # Automatic bounded retry (build_f.json memory_auto_retry; reports/memory_auto_retry.md). A job whose
    # provider call failed transiently waits in "retry_wait"; flush() resumes it once its back-off has passed.
    # One timer thread (started by the boot-time resume, never at import) sleeps until the earliest due retry
    # and then runs flush() itself, only while no conversation turn or reflection is active.
    def turn_active():
        # Plain reads of webui's turn bookkeeping (atomic under the GIL). No lock is taken, so the existing
        # lock order (_ws_active_lock before the memory locks) is unchanged.
        return bool(g.get('_ws_active')) or bool(g.get('_thinking_inflight',0))
    memory.retry_gate=lambda:not turn_active()
    # B: the memory worker's snapshot write (f_memory.settle) waits for the same quiet moment.
    memory.persist_gate=memory.retry_gate
    retry_wake=threading.Event()
    memory.on_auto_retry=retry_wake.set
    retry_timer={'thread':None}
    retry_timer_lock=threading.Lock()
    def retry_tick():
        """One timer step; returns the seconds to sleep (None = until a retry is scheduled)."""
        due=memory.next_auto_retry_at()
        if due is None:return None
        wait=due-memory.clock()
        if wait>0:return min(wait,60.0)
        if turn_active():return 5.0
        flush()
        return 5.0
    def retry_loop():
        while not memory._closed:
            retry_wake.clear()
            try:wait=retry_tick()
            except Exception as exc:
                print(f'[{app_name}] memory auto-retry: {type(exc).__name__}; next check in 60 s',flush=True)
                wait=60.0
            retry_wake.wait(wait)
    def start_retry_timer():
        with retry_timer_lock:
            if not memory.auto_retry['enabled'] or retry_timer['thread'] is not None:return False
            retry_timer['thread']=_DaemonThread(target=retry_loop,daemon=True,name='F-memory-retry')
        retry_timer['thread'].start()
        return True
    def retry_timer_running():
        thread=retry_timer['thread']
        return bool(thread is not None and thread.is_alive())
    g['f_memory_retry_tick']=retry_tick
    g['f_start_memory_retry']=start_retry_timer
    g['f_mark_complete']=lambda tid:mark_complete(g,tid,archive_turn)
    def speech_done(data=None):
        tid=(data or {}).get('turn_id')
        with g['_hist_lock']:
            t=g['_find_turn'](tid)
            if t:t['f_speech_done']=True;g['_save_history']()
        archive_turn(tid)
        return {'ok':True}
    g['socketio'].on_event('speech_done',speech_done)
    protect=g['_protect_latest']
    def protected():
        result=protect()
        with g['_hist_lock']:tid=g['CONVERSATION_HISTORY'][-1]['turn_id'] if g['CONVERSATION_HISTORY'] else None
        if tid:archive_turn(tid)
        return result
    g['_protect_latest']=protected
    status_cache_lock=threading.Lock()
    status_cache={'memory':None,'at':None}
    def memory_status_snapshot():
        # Query-cache writes run under _query_lock rather than _lock. A status
        # scan must not open their destination files during an atomic replace
        # (especially on Windows). Never wait behind a provider request merely
        # to paint the website: return a labelled previous snapshot while busy.
        acquired=[]
        busy=False
        try:
            for lock in (memory._query_lock,memory._worker_lock,memory._lock):
                if not lock.acquire(blocking=False):
                    busy=True
                    break
                acquired.append(lock)
            if not busy and memory._active_retrievals:
                busy=True
            if not busy:
                snapshot=memory.status()
                at=datetime.now(timezone.utc).isoformat()
                with status_cache_lock:
                    status_cache.update(memory=copy.deepcopy(snapshot),at=at)
                return {**snapshot,'status_busy':False,'status_stale':False,'status_snapshot_at':at}
        finally:
            for lock in reversed(acquired):lock.release()
        with status_cache_lock:
            snapshot=copy.deepcopy(status_cache['memory'])
            at=status_cache['at']
        return {**(snapshot or {'method':'F'}),'status_busy':True,
                'status_stale':snapshot is not None,'status_snapshot_at':at}
    g['F_MEMORY_STATUS_SNAPSHOT']=memory_status_snapshot
    @g['app'].route('/api/build_f/status')
    def status():
        # template_check: the boot-time self-check (R2), {file: [problems]}.
        check=g.get('TEMPLATE_CHECK')
        if check is None and callable(g.get('run_template_check')):check=g['run_template_check']()
        # memory carries the effective §4 limits (budget_chars, fact_packets, event_candidates)
        # plus the surface budget; telemetry: call_receipt delivery counters.
        return g['jsonify']({'app':app_name,'usage':meter.summary(),
                             'memory':{**memory_status_snapshot(),'surface_budget_chars':surface_budget,
                                       'auto_retry_timer':retry_timer_running()},'pricing':pricing,
                             'template_check':check,'telemetry':telemetry.status()})
    # Scheduled legacy summarization is intentionally replaced by F writes.
    if 'consolidate_memory' in g['app'].view_functions:
        g['app'].view_functions['consolidate_memory']=lambda:(g['jsonify']({'error':'Build F uses evidence-backed fact updates; legacy consolidation is disabled.'}),409)
    # Pending complete evidence from a prior graceful shutdown may finish once; blocked
    # jobs are not retried, transiently failed ones wait for the automatic-retry timer.
    g['f_resume_pending']=lambda:threading.Thread(target=flush,daemon=True,name='F-memory-resume').start()
    # webui's boot sequence calls f_resume_pending() once in __main__; the settlement
    # worker (boot key snapshot, then settling) and the memory retry timer start there
    # too, never on import.
    resume_memory=g['f_resume_pending']
    def resume_pending():
        resume_memory()
        start_retry_timer()
        settler.start()
    g['f_resume_pending']=resume_pending
    g['f_start_settlement']=settler.start
    from f_reset import install_reset
    install_reset(g,memory,meter,worker_lock,work_requested)
    if (root/'config/llm_roles.json').is_file():
        from f_roles import install_roles
        install_roles(g,root,memory,meter,scope,local,pricing)
    try:
        # the fully-local option: detect / pick Ollama models (Setup page, first-run wizard)
        from f_ollama import install_ollama
        install_ollama(g)
    except Exception as error:
        print(f"[ollama] endpoints not installed: {error}")
    # OpenRouter calls (streams, drains, complete() memory writes, embeddings) reach
    # f_roles.after() exactly once, after metering; its observer builds their
    # call_receipt with first-token timing. An observer set earlier (e.g. a benchmark
    # harness) is chained and keeps its own behaviour.
    previous_observer=g.get('F_OR_AFTER_CALL')
    def after_call_observer(handle,receipt):
        telemetry.on_openrouter(handle,receipt)
        if previous_observer is not None:previous_observer(handle,receipt)
    g['F_OR_AFTER_CALL']=after_call_observer
    # Every OpenRouter stream event (f_roles event_sink, on the stream or drain thread):
    # the emitter notes the time of the last content delta for the call_receipt's
    # last_token_ms (f_openrouter records only the first). Chained like the observer above.
    previous_event=g.get('F_OR_EVENT')
    def event_observer(event):
        telemetry.on_event(event)
        if previous_event is not None:previous_event(event)
    g['F_OR_EVENT']=event_observer
    from f_controls import install_controls
    install_controls(g,meter)
    try:
        from f_tools import install_tools
        install_tools(g)
    except Exception as tools_error:
        # The tool layer is optional (needs the `mcp` package only when actually used);
        # a failure here must never block the brain from starting.
        print(f"[tools] not installed: {tools_error}")
    try:
        from f_persona import install_persona
        install_persona(g)
    except Exception as persona_error:
        print(f"[persona] not installed: {persona_error}")


def mark_complete(g,tid,archive):
    with g['_hist_lock']:
        t=g['_find_turn'](tid)
        if t:t['f_generation_complete']=True;g['_save_history']()
    archive(tid)
