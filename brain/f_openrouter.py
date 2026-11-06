"""Pinned, metered OpenRouter and Alibaba Model Studio transports for Build F and isolated evaluations.

This module has no environment reads, global monkeypatches, network side effects,
or automatic retries. Callers supply an HTTP transport and *both* meter hooks.
``before_call(request)`` must reserve the request before returning a handle;
``after_call(handle, receipt)`` settles it exactly once. Unknown usage must keep
that reservation held. Stream callers must exhaust or explicitly close generators.
The optional event sink receives raw provider events for a private audit archive;
only ``delta.content`` is yielded to the native speech parser, never reasoning.

``fetch_generation``/``fetch_key_usage``/``fetch_credits`` at the end are single
read-only billing GETs for the settlement worker (f_settle.py); they never retry.

``AlibabaClient`` (end of file, added 2026-10-09) is the same client for Alibaba Cloud
Model Studio (Singapore workspace, OpenAI-compatible endpoint): identical receipts,
metering hooks and drain, the cost computed from the reviewed price table because
Model Studio reports tokens but no charge.

Drain instead of abort (owner decision 2026-09-27): a cancelled stream is billed at
the full uncached prompt price with no usage record, while letting the same request
finish costs about half and yields an exact receipt. When a ``StreamDrainer`` is in
scope (``drain_scope`` or the ``drainer=`` argument), a cancelled stream's open
response is handed to a daemon thread that reads it to ``[DONE]``, discards the
content (never yielded) and settles the receipt with status ``drained``. The caller
returns immediately as before. On the per-phase deadline, a transport error, or when
the per-phase cap of concurrent drains is reached, the response is closed and the
receipt settles through the ordinary ``cancelled`` path.
"""
from __future__ import annotations

import collections
import copy
import contextlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timedelta, timezone

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
EMBED_URL = "https://openrouter.ai/api/v1/embeddings"
ROLES = {"surface", "deep", "reflection", "memory_write"}
MAX_BODY_BYTES = 120000
MAX_OUTPUT_TOKENS = 8192


class OpenRouterError(RuntimeError):
    """A single attempted request failed; its receipt has already been settled."""


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} is outside its permitted range")
    return float(value)


def load_catalog(path):
    """Read the reviewed local endpoint snapshot; never discover models silently."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = data.get("candidates", [])
    result = {}
    for row in rows:
        model = row.get("model_id")
        if not isinstance(model, str) or not model or model in result:
            raise ValueError("Catalog needs unique model IDs")
        result[model] = copy.deepcopy(row)
    if not result:
        raise ValueError("Empty model catalog")
    return result


def _endpoint_config(row):
    model, tag = row.get("model_id"), row.get("provider_tag")
    if not isinstance(model, str) or "/" not in model or not isinstance(tag, str) or not tag:
        raise ValueError("An exact catalog model and provider tag are required")
    base = row.get("usd_per_million_tokens") or {}
    # A request may straddle a clock tier. Reserve the largest published tier,
    # while the receipt uses OpenRouter's actual charge, never this upper bound.
    prices = [base] + [t.get("usd_per_million_tokens", {}) for t in row.get("pricing_tiers", [])]
    prompt = max(_number(p.get("prompt"), "prompt price", positive=True) for p in prices)
    completion = max(_number(p.get("completion", 0), "completion price") for p in prices)
    cache_write = max(_number(p.get("input_cache_write", 0), "cache write price") for p in prices)
    request = max(_number(p.get("request", 0), "request price") for p in prices)
    return {"model_id": model, "provider_tag": tag, "provider_name": row.get("provider_name", tag),
            "canonical_slug": row.get("canonical_slug", model), "quantization": row.get("quantization"),
            "max_price": {"prompt": prompt, "completion": completion, "request": request},
            "reservation_input_rate": max(prompt, cache_write),
            "endpoint_context_tokens": row.get("endpoint_context_tokens"),
            "endpoint_max_completion_tokens": row.get("endpoint_max_completion_tokens"),
            "supported_parameters": copy.deepcopy(row.get("supported_parameters", [])),
            "structured_outputs_advertised": bool(row.get("structured_outputs_advertised")),
            "snapshot_time": row.get("endpoint_snapshot_time_utc")}


def role_config(catalog, model_id, role, max_tokens=None, *, reasoning_enabled=None):
    """Choose conservative per-role parameters from a reviewed endpoint row.

    Surface and optional-thinking extraction default to thinking off. Deep and
    reflection default to the lowest advertised effort; mandatory thinkers can
    never be switched off. Non-thinking models receive no reasoning parameter.
    ``memory_extraction`` is accepted as an alias for the native ``memory_write``.
    """
    role = "memory_write" if role == "memory_extraction" else role
    if role not in ROLES or model_id not in catalog:
        raise ValueError("Unknown role or unreviewed model")
    row = catalog[model_id]
    config = _endpoint_config(row)
    if config["max_price"]["completion"] <= 0:
        raise ValueError("Chat completion price must be explicitly positive")
    meta = row.get("reasoning_catalog_metadata")
    parameters = set(config["supported_parameters"])
    mandatory = bool((meta or {}).get("mandatory"))
    enabled = mandatory or role in {"deep", "reflection"}
    if reasoning_enabled is not None:
        if not isinstance(reasoning_enabled, bool):
            raise ValueError("reasoning_enabled must be boolean")
        if mandatory and not reasoning_enabled:
            raise ValueError("This model cannot disable thinking")
        enabled = reasoning_enabled
    reasoning = None
    if meta is not None and "reasoning" in parameters:
        if not enabled:
            reasoning = {"enabled": False, "exclude": True}
        else:
            efforts = (meta or {}).get("supported_efforts") or []
            ordered = [x for x in ("minimal", "low", "medium", "high", "xhigh", "max") if x in efforts]
            if ordered:
                reasoning = {"effort": ordered[0], "exclude": True}
            elif (meta or {}).get("supports_max_tokens"):
                reasoning = {"max_tokens": 1024, "exclude": True}
            else:
                reasoning = {"enabled": True, "exclude": True}
    elif mandatory:
        raise ValueError("Mandatory reasoning model lacks a supported reasoning control")
    else:
        enabled = False
    default_limit = 8192 if enabled else 1024 if role == "surface" else 4096
    limit = default_limit if max_tokens is None else max_tokens
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_OUTPUT_TOKENS:
        raise ValueError("max_tokens must be an integer between 1 and 8192")
    endpoint_limit = config.get("endpoint_max_completion_tokens")
    if endpoint_limit and limit > endpoint_limit:
        raise ValueError("Output limit exceeds the selected endpoint")
    if role == "memory_write" and not config["structured_outputs_advertised"]:
        raise ValueError("The selected endpoint does not advertise strict structured output")
    config.update(role=role, max_tokens=limit, reasoning=reasoning,
                  mandatory_reasoning=mandatory, reasoning_enabled=enabled)
    return config


def _routing(config):
    route = {"only": [config["provider_tag"]], "order": [config["provider_tag"]],
             "allow_fallbacks": False, "require_parameters": True,
             "max_price": copy.deepcopy(config["max_price"])}
    if config.get("role") == "embedding":
        # Embedding endpoints publish an empty chat supported_parameters list.
        # dimensions/encoding_format are native embedding API fields, verified
        # by output shape below rather than chat-parameter provider filtering.
        route.pop("require_parameters")
    if config.get("quantization") not in (None, "", "unknown"):
        route["quantizations"] = [config["quantization"]]
    return route


def make_chat_body(config, messages, *, stream=True, json_schema=None):
    if config.get("role") not in ROLES or not config.get("model_id") or not config.get("provider_tag"):
        raise ValueError("Use a validated role_config")
    if not isinstance(messages, list) or not messages:
        raise ValueError("A nonempty message list is required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise ValueError("Only system, user, and assistant text messages are supported")
        if not isinstance(message.get("content"), str):
            raise ValueError("This transport accepts text-only messages")
    body = {"model": config["model_id"], "messages": copy.deepcopy(messages),
            "stream": bool(stream), "max_tokens": config["max_tokens"], "provider": _routing(config)}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if config.get("reasoning") is not None:
        body["reasoning"] = copy.deepcopy(config["reasoning"])
    if config["role"] == "memory_write" and json_schema is None:
        json_schema = FACT_SCHEMA
    if json_schema is not None:
        if not config.get("structured_outputs_advertised"):
            raise ValueError("JSON schema is unsupported by this endpoint")
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "amika_memory_facts", "strict": True, "schema": copy.deepcopy(json_schema)}}
    _body_size(body, config)
    return body


def _body_size(body, config):
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise ValueError("Request exceeds the 120 KB input ceiling")
    # UTF-8 bytes plus framing reserve are deliberately conservative text-token
    # bounds; there are no image/audio/tool inputs in this transport.
    tokens_upper = size + 4096
    context = config.get("endpoint_context_tokens")
    if context and body.get("max_tokens", 0) >= context:
        raise ValueError("Output limit alone exhausts the endpoint context")
    # The byte bound protects the budget, not context eligibility. In particular
    # Chinese prompt bytes can far exceed real tokenizer counts. Without the
    # endpoint's exact tokenizer, rejecting bytes > context would falsely label
    # a model incompatible. The provider validates actual input-token limits;
    # its bounded error is archived and never automatically retried. Embedding
    # context is also per item, not the aggregate batch byte count.
    return size, tokens_upper


def estimate_reservation(config, body):
    _, bound = _body_size(body, config)
    price = config["max_price"]
    value = (bound * config["reservation_input_rate"] + body.get("max_tokens", 0) * price["completion"]) / 1e6
    # Extra margin covers rounding/account-level fee behavior; actual usage.cost
    # settles once and never has a second platform fee added to it.
    return (value + price.get("request", 0)) * 1.10 + 0.00001


def normalize_usage(raw, *, embedding=False):
    """No estimates: preserve actual OR charge and avoid double-counting thought."""
    raw = raw if isinstance(raw, dict) else {}
    issues, warnings = [], []
    def count(mapping, key, required=False):
        value = mapping.get(key)
        if value is None and not required:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append("invalid_or_missing_" + key)
            return 0
        return value
    prompt = count(raw, "prompt_tokens", True)
    completion = count(raw, "completion_tokens", not embedding)
    pd = raw.get("prompt_tokens_details")
    pd = pd if isinstance(pd, dict) else {}
    cd = raw.get("completion_tokens_details")
    cd = cd if isinstance(cd, dict) else {}
    cached, written = count(pd, "cached_tokens"), count(pd, "cache_write_tokens")
    reasoning = count(cd, "reasoning_tokens")
    if cached + written > prompt:
        issues.append("cache_tokens_exceed_prompt")
    if reasoning > completion:
        # Some OR providers report a reasoning count using a different count
        # convention from completion_tokens (observed DeepSeek V3.2/Terminus/
        # V4 Flash). Preserve it for analysis, but do not discard a valid paid
        # response or invent an extra charge by adding it to completion_tokens.
        warnings.append("reasoning_count_exceeds_completion_provider_convention")
    cost = raw.get("cost")
    try:
        cost = _number(cost, "cost")
    except ValueError:
        cost = None
        issues.append("invalid_or_missing_cost")
    return {"cost_usd": cost, "complete": not issues, "issues": issues, "warnings": warnings,
            "tokens": {"total_input_tokens": prompt, "input_uncached_tokens": max(0, prompt-cached-written),
                       "cache_read_tokens": cached, "cache_write_tokens": written,
                       "output_tokens": completion, "reasoning_tokens": reasoning,
                       "total_tokens": prompt+completion}}


def _merge(target, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _identity(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


class OpenRouterClient:
    # Endpoint and receipt label; AlibabaClient overrides both and the three hooks
    # below (_chat_body, _reservation, _usage). Everything else is shared.
    chat_url = CHAT_URL
    transport = "openrouter"

    def __init__(self, http, api_key, before_call, after_call, *, timeout=(10, 120), event_sink=None,
                 drainer=None):
        if not api_key or not callable(before_call) or not callable(after_call):
            raise ValueError("A key and both mandatory metering callbacks are required")
        self.http, self._key = http, api_key
        self.before_call, self.after_call = before_call, after_call
        self.timeout, self.event_sink = timeout, event_sink
        # Explicit drainer, else the one in the caller's drain_scope (thread-local).
        self.drainer = drainer

    def _chat_body(self, config, messages, *, stream, json_schema=None):
        return make_chat_body(config, messages, stream=stream, json_schema=json_schema)

    def _reservation(self, config, body):
        return estimate_reservation(config, body)

    def _usage(self, receipt, embedding):
        return normalize_usage(receipt["raw_usage"], embedding=embedding)

    def _prepare(self, config, body, metadata, url):
        request = {"url": url, "config": copy.deepcopy(config), "body": copy.deepcopy(body),
                   "metadata": copy.deepcopy(metadata or {}),
                   "estimated_max_usd": self._reservation(config, body)}
        # This is deliberately before HTTP, including DNS/connect. A hook may
        # refuse budget or save request+state provenance without leaking headers.
        handle = self.before_call(request)
        receipt = {"requested_model": config["model_id"], "requested_provider_tag": config["provider_tag"],
                   "option_key": config.get("option_key"), "transport": self.transport,
                   "actual_model": None, "actual_provider": None, "request_id": None,
                   "raw_usage": {}, "content": "", "finish_reason": None, "truncated": False,
                   "estimated_max_usd": request["estimated_max_usd"], "metadata": request["metadata"],
                   "started_at": datetime.now(timezone.utc).isoformat(),
                   "timing": {"elapsed_s": None, "first_event_s": None,
                              "first_content_s": None, "first_speech_s": None}, "issues": []}
        return handle, receipt

    def _post(self, url, body, stream):
        return self.http.post(url, headers={"Authorization": "Bearer " + self._key,
                 "Content-Type": "application/json", "X-OpenRouter-Title": "Amika Evo Build F"},
                 json=body, stream=stream, timeout=self.timeout)

    def _event(self, config, receipt, data):
        if not isinstance(data, dict):
            raise OpenRouterError("Provider returned a non-object event")
        if data.get("id"):
            receipt["request_id"] = str(data["id"])
        if data.get("model"):
            receipt["actual_model"] = str(data["model"])
        if data.get("provider"):
            receipt["actual_provider"] = str(data["provider"])
        if isinstance(data.get("usage"), dict):
            _merge(receipt["raw_usage"], data["usage"])
        if data.get("error"):
            raise OpenRouterError("OpenRouter returned an error event")
        if self.event_sink:
            self.event_sink(copy.deepcopy(data))
        actual = receipt["actual_model"]
        if actual and actual not in {config["model_id"], config.get("canonical_slug")}:
            if "model_mismatch" not in receipt["issues"]:
                receipt["issues"].append("model_mismatch")
        provider = receipt["actual_provider"]
        known = {_identity(config["provider_name"]), _identity(config["provider_tag"]),
                 _identity(config["provider_tag"].split("/")[0])}
        if provider and _identity(provider) not in known and "provider_mismatch" not in receipt["issues"]:
            receipt["issues"].append("provider_mismatch")

    def _settle(self, handle, receipt, started, done, cancelled, error, *, embedding=False, drained=False):
        receipt["timing"]["elapsed_s"] = time.perf_counter()-started
        receipt["usage"] = self._usage(receipt, embedding)
        receipt["cost_usd"] = receipt["usage"]["cost_usd"]
        receipt["usage_complete"] = receipt["usage"]["complete"]
        receipt["warnings"] = receipt["usage"].get("warnings", [])
        receipt["issues"].extend(receipt["usage"]["issues"])
        if receipt["cost_usd"] is not None and receipt["cost_usd"] > receipt["estimated_max_usd"] + 1e-9:
            receipt["issues"].append("reported_charge_exceeds_reservation")
        receipt["status"] = "cancelled" if cancelled else "complete" if done and not error and not receipt["issues"] else "unknown"
        if drained and receipt["status"] == "complete":
            # A cancelled turn's request read to its end: exact usage, content discarded.
            receipt["status"] = "drained"
        receipt["error_type"] = type(error).__name__ if error else None
        self.after_call(handle, receipt)

    def _stream_line(self, config, receipt, line, started):
        """Apply one SSE line to the receipt: None = skip, True = [DONE], else the content text."""
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line or not line.startswith("data:"):
            return None
        raw = line[5:].strip()
        if raw == "[DONE]":
            return True
        data = json.loads(raw)
        if receipt["timing"]["first_event_s"] is None:
            receipt["timing"]["first_event_s"] = time.perf_counter()-started
        self._event(config, receipt, data)
        choices = data.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            receipt["finish_reason"] = choice["finish_reason"]
            receipt["truncated"] = choice["finish_reason"] in {"length", "max_tokens"}
        text = (choice.get("delta") or {}).get("content")
        if text is not None and not isinstance(text, str):
            raise OpenRouterError("Provider returned non-text content")
        if text:
            if receipt["timing"]["first_content_s"] is None:
                receipt["timing"]["first_content_s"] = time.perf_counter()-started
            receipt["content"] += text
        return text or ""

    def _hand_off(self, drainer, config, handle, receipt, started, response, lines, carry):
        """Give a cancelled stream's open response to the drainer; False = abort as before."""
        phase = (receipt.get("metadata") or {}).get("phase") or config.get("role")
        try:
            deadline = drainer.acquire(phase)
        except Exception:
            return False
        if deadline is None:
            return False
        job = lambda: self._drain(drainer, config, handle, receipt, started, response, lines, carry, phase, deadline)
        try:
            drainer.spawn(job, phase)
        except Exception:
            drainer.release(phase)
            return False
        return True

    def _drain(self, drainer, config, handle, receipt, started, response, lines, carry, phase, deadline):
        """Drainer thread: read the rest of the stream, discard content, settle exactly once."""
        began = time.perf_counter()
        expired = threading.Event()

        def expire():
            expired.set()
            with contextlib.suppress(Exception):
                response.close()  # unblocks a read that is waiting on the socket
        timer = None
        done, failure = False, None
        receipt["discarded"] = True
        try:
            timer = drainer.timer(max(0.0, deadline - (time.perf_counter() - began)), expire)
            for line in itertools.chain(() if carry is None else (carry,), lines):
                if expired.is_set():
                    break
                if self._stream_line(config, receipt, line, started) is True:
                    done = True
                    break
            if not done and not expired.is_set():
                failure = "stream_ended_without_done"
        except BaseException as exc:  # a transport error, or the watchdog closing the socket
            failure = type(exc).__name__
        finally:
            if timer is not None:
                timer.cancel()
            with contextlib.suppress(Exception):
                response.close()
        hit = expired.is_set() and not done
        drained = done
        receipt["drain"] = {"phase": phase, "ledger_id": handle, "deadline_s": deadline,
                            "elapsed_s": round(time.perf_counter() - began, 3), "deadline_hit": hit,
                            "drained": drained, "error": None if drained or hit else failure}
        try:
            # Drained: settled like a completed call (status "drained"). Otherwise the
            # existing cancelled path; f_settle then looks the generation up by id.
            self._settle(handle, receipt, started, drained, not drained, None, drained=drained)
        except BaseException as exc:
            drainer.log(f"[drain] phase={phase} accounting failed: {type(exc).__name__}")
        finally:
            drainer.finish(phase, receipt)

    def stream(self, config, messages, *, metadata=None, cancel=None, speech_probe=None, json_schema=None):
        body = self._chat_body(config, messages, stream=True, json_schema=json_schema)
        if cancel is not None and cancel.is_set():
            return
        drainer = self.drainer if self.drainer is not None else current_drainer()
        handle, receipt = self._prepare(config, body, metadata, self.chat_url)
        started = time.perf_counter()
        response, error, done, cancelled = None, None, False, False
        lines, carry, handed = None, None, False
        try:
            response = self._post(self.chat_url, body, True)
            response.raise_for_status()
            response.encoding = "utf-8"
            lines = response.iter_lines(decode_unicode=True)
            for line in lines:
                if cancel is not None and cancel.is_set():
                    # This line is not processed here; a drainer reads it first.
                    cancelled, carry = True, line
                    break
                text = self._stream_line(config, receipt, line, started)
                if text is True:
                    done = True
                    break
                if text:
                    if speech_probe and receipt["timing"]["first_speech_s"] is None and speech_probe(receipt["content"]):
                        receipt["timing"]["first_speech_s"] = time.perf_counter()-started
                    yield text
                # Never break on finish_reason: usage normally follows it in a
                # final choices=[] event and must be read before [DONE].
            if not done and not cancelled:
                raise OpenRouterError("Stream ended without its completion marker")
        except GeneratorExit:
            cancelled = True
            raise
        except BaseException as exc:
            error = exc
            raise
        finally:
            if cancelled and error is None and lines is not None and drainer is not None:
                # The request is in flight and billed either way; finishing it is
                # cheaper (cached prompt) and records exact usage. Never blocks here.
                handed = self._hand_off(drainer, config, handle, receipt, started, response, lines, carry)
            if not handed:
                # Close the line iterator explicitly: a wrapping recorder (the benchmark
                # guard's stream.jsonl writer) may hold a file open until it is closed.
                if lines is not None and hasattr(lines, "close"):
                    with contextlib.suppress(Exception):
                        lines.close()
                if response is not None:
                    with contextlib.suppress(Exception):
                        response.close()
                self._settle(handle, receipt, started, done, cancelled, error)
        if handed:
            return  # the drainer thread owns the receipt from here on
        if receipt["status"] == "unknown":
            raise OpenRouterError("Incomplete provider accounting; inspect the request receipt before retrying")

    def complete(self, config, messages, *, metadata=None, json_schema=None):
        body = self._chat_body(config, messages, stream=False, json_schema=json_schema)
        handle, receipt = self._prepare(config, body, metadata, self.chat_url)
        started = time.perf_counter()
        response, error, done = None, None, False
        data = None
        try:
            response = self._post(self.chat_url, body, False)
            response.raise_for_status()
            data = response.json()
            self._event(config, receipt, data)
            choices = data.get("choices") or []
            if not choices:
                raise OpenRouterError("Provider returned no completion choice")
            receipt["finish_reason"] = choices[0].get("finish_reason")
            receipt["truncated"] = receipt["finish_reason"] in {"length", "max_tokens"}
            text = (choices[0].get("message") or {}).get("content")
            if not isinstance(text, str):
                raise OpenRouterError("Provider returned no text content")
            receipt["content"] = text
            done = True
        except BaseException as exc:
            error = exc
            raise
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response.close()
            self._settle(handle, receipt, started, done, False, error)
        if receipt["status"] != "complete":
            raise OpenRouterError("Incomplete provider accounting; inspect the request receipt before retrying")
        return {"text": receipt["content"], "raw": data, "receipt": receipt}

    def embed(self, config, texts, *, dimensions=768, metadata=None):
        """Embedding config comes from embedding_config(reviewed_endpoint_row).

        Inputs are already prefixed by the caller exactly as the existing F
        pipeline requires. Returns vectors normalized like native F, plus raw
        response and metering receipt. Missing provider cost is never inferred.
        """
        if config.get("role") != "embedding" or config.get("model_id") != "google/gemini-embedding-2":
            raise ValueError("Only the existing Gemini embedding 2 model is permitted")
        if dimensions != 768 or not isinstance(texts, list) or not texts or len(texts) > 16 or any(not isinstance(t, str) for t in texts):
            raise ValueError("Expected one to sixteen text inputs and exactly 768 dimensions")
        body = {"model": config["model_id"], "input": texts, "dimensions": dimensions,
                "encoding_format": "float", "provider": _routing(config)}
        handle, receipt = self._prepare(config, body, metadata, EMBED_URL)
        started = time.perf_counter()
        response, error, done = None, None, False
        data, vectors = None, []
        try:
            response = self._post(EMBED_URL, body, False)
            response.raise_for_status()
            data = response.json()
            self._event(config, receipt, data)
            rows = data.get("data") or []
            if len(rows) != len(texts) or sorted(r.get("index", -1) for r in rows) != list(range(len(texts))):
                raise OpenRouterError("Embedding input/output index mismatch")
            for row in sorted(rows, key=lambda r: r["index"]):
                vector = row.get("embedding")
                if not isinstance(vector, list) or len(vector) != dimensions:
                    raise OpenRouterError("Embedding dimension mismatch")
                if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in vector):
                    raise OpenRouterError("Invalid embedding component")
                norm = math.sqrt(sum(v*v for v in vector))
                if not norm:
                    raise OpenRouterError("Zero embedding vector")
                vectors.append([v/norm for v in vector])
            done = True
        except BaseException as exc:
            error = exc
            raise
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response.close()
            self._settle(handle, receipt, started, done, False, error, embedding=True)
        if receipt["status"] != "complete":
            raise OpenRouterError("Incomplete embedding accounting; inspect the request receipt before retrying")
        return {"vectors": vectors, "raw": data, "receipt": receipt}


# ---------------------------------------------------------------------------
# Drain instead of abort.
DRAIN_DEADLINES = {"surface": 30.0, "deep": 90.0, "reflection": 120.0}
DRAIN_MAX_PER_PHASE = 4
DISCARDED_KEEP = 300
_DRAIN_SCOPE = threading.local()
_DISCARD_LOCK = threading.Lock()


@contextlib.contextmanager
def drain_scope(drainer):
    """Streams started on this thread inside the block drain on cancel (None = abort)."""
    previous = getattr(_DRAIN_SCOPE, "drainer", None)
    _DRAIN_SCOPE.drainer = drainer
    try:
        yield drainer
    finally:
        _DRAIN_SCOPE.drainer = previous


def current_drainer():
    return getattr(_DRAIN_SCOPE, "drainer", None)


class StreamDrainer:
    """Per-phase deadlines and a concurrency cap for draining cancelled streams.

    ``deadlines`` maps a phase to seconds; phases without one (memory_write, which is
    never cancelled) are never drained. ``on_finished(receipt)`` runs in the drain
    thread after the receipt settled (the live wiring writes the discarded text).
    One ``[drain]`` line is logged per drain; it never contains a key.
    """

    def __init__(self, *, deadlines=None, max_per_phase=DRAIN_MAX_PER_PHASE, log=None, on_finished=None):
        self.deadlines = {str(k): float(v) for k, v in (DRAIN_DEADLINES if deadlines is None else deadlines).items()
                          if _optional_number(v)}
        self.max_per_phase = int(max_per_phase)
        self.on_finished = on_finished
        self._log = log or (lambda message: print(message, flush=True))
        self._lock = threading.Lock()
        self._inflight = collections.Counter()
        self._threads = set()
        self.stats = {"drained": 0, "deadline_fallbacks": 0, "error_fallbacks": 0, "cap_fallbacks": 0}

    def log(self, message):
        with contextlib.suppress(Exception):
            self._log(message)

    def acquire(self, phase):
        """Deadline in seconds for a new drain of ``phase``, or None to abort instead."""
        deadline = self.deadlines.get(str(phase))
        if not deadline:
            return None
        with self._lock:
            if self._inflight[phase] >= self.max_per_phase:
                self.stats["cap_fallbacks"] += 1
                capped = True
            else:
                self._inflight[phase] += 1
                capped = False
        if capped:
            self.log(f"[drain] phase={phase} cap of {self.max_per_phase} drains reached; aborting this stream")
            return None
        return deadline

    def release(self, phase):
        with self._lock:
            self._inflight[phase] = max(0, self._inflight[phase] - 1)

    def spawn(self, job, phase):
        def run():
            try:
                job()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True, name="F-drain-" + str(phase))
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def timer(self, seconds, callback):
        timer = threading.Timer(seconds, callback)
        timer.daemon = True
        timer.start()
        return timer

    def finish(self, phase, receipt):
        self.release(phase)
        drain = receipt.get("drain") or {}
        with self._lock:
            key = "drained" if drain.get("drained") else "deadline_fallbacks" if drain.get("deadline_hit") else "error_fallbacks"
            self.stats[key] += 1
        cost = receipt.get("cost_usd")
        self.log(f"[drain] phase={phase} elapsed={drain.get('elapsed_s', 0):.2f}s "
                 f"cost={'$%.6f' % cost if isinstance(cost, (int, float)) else 'unknown'} "
                 f"deadline_hit={bool(drain.get('deadline_hit'))} status={receipt.get('status')}")
        if self.on_finished is not None:
            try:
                self.on_finished(receipt)
            except Exception as exc:
                self.log(f"[drain] phase={phase} could not archive discarded text: {type(exc).__name__}")

    def status(self):
        with self._lock:
            return {"enabled": True, "in_flight": {k: v for k, v in self._inflight.items() if v},
                    "deadlines_s": dict(self.deadlines), "max_per_phase": self.max_per_phase, **self.stats}

    def wait_idle(self, timeout=10.0):
        """Join running drain threads (tests and shutdown)."""
        end = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._threads)
            if not threads:
                return True
            for thread in threads:
                thread.join(max(0.0, end - time.monotonic()))
            if time.monotonic() >= end:
                with self._lock:
                    return not self._threads


def write_discarded(folder, receipt, *, speech=None, keep=DISCARDED_KEEP):
    """Append a drained/cancelled stream's discarded text to ``<folder>/<turn_id>.json``.

    Keeps only the newest ``keep`` files. Never raises into the drain thread's caller
    beyond what the drainer already catches.
    """
    folder = Path(folder)
    metadata = receipt.get("metadata") or {}
    turn = str(metadata.get("turn_id") or "")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", turn)[:120] or "no-turn"
    drain = receipt.get("drain") or {}
    entry = {"phase": drain.get("phase") or metadata.get("phase"), "ledger_id": drain.get("ledger_id"),
             "generation_id": receipt.get("request_id"), "status": receipt.get("status"),
             "drained": bool(drain.get("drained")), "deadline_hit": bool(drain.get("deadline_hit")),
             "elapsed_s": drain.get("elapsed_s"), "cost_usd": receipt.get("cost_usd"),
             "finish_reason": receipt.get("finish_reason"), "started_at": receipt.get("started_at"),
             "discarded_at": datetime.now(timezone.utc).isoformat(), "text": receipt.get("content", ""),
             "speech": speech}
    with _DISCARD_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + ".json")
        document = {"turn_id": turn, "discarded": True, "entries": []}
        if path.exists():
            with contextlib.suppress(Exception):
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                    document = loaded
        document["entries"].append(entry)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
        files = sorted(folder.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name))
        for old in files[:-keep] if keep > 0 else []:
            with contextlib.suppress(OSError):
                old.unlink()
    return path


def embedding_config(reviewed_endpoint_row):
    config = _endpoint_config(reviewed_endpoint_row)
    if config["model_id"] != "google/gemini-embedding-2":
        raise ValueError("An embedding-model migration is not authorized by this adapter")
    config.update(role="embedding", max_tokens=0)
    return config


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_FACT_PROPERTIES = {
    "id": _STRING, "text": _STRING, "source_ids": {"type": "array", "minItems": 1, "items": _STRING},
    "quotes": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False,
        "properties": {"source_id": _STRING, "speaker": {"type": "string", "enum": ["user", "assistant", "operator"]}, "quote": _STRING},
        "required": ["source_id", "speaker", "quote"]}},
    "date": _STRING, "source_kind": {"type": "string", "enum": ["user", "assistant", "mixed", "operator_cue"]},
    "entities": _STRINGS, "subject": _STRING, "predicate": _STRING, "value": _STRING,
    "status": {"type": "string", "enum": ["active", "superseded", "uncertain", "retracted"]},
    "valid_from": {"type": ["string", "null"]}, "valid_to": {"type": ["string", "null"]},
    "supersedes": _STRINGS, "replaces_prior": {"type": "boolean"},
    "fact_type": {"type": "string", "enum": ["user_fact", "assistant_commitment", "shared_event", "speculation", "operator_cue", "persona"]}}
FACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["facts"],
               "properties": {"facts": {"type": "array", "maxItems": 20, "items": {
                   "type": "object", "additionalProperties": False, "properties": _FACT_PROPERTIES,
                   "required": list(_FACT_PROPERTIES)}}}}


# ---------------------------------------------------------------------------
# Read-only billing lookups used by the cost-settlement worker (f_settle.py).
# Each helper makes exactly one GET with the caller's HTTP object and key and
# never retries; the worker owns the backoff. Exceptions carry short codes
# only, never response text, URLs with credentials, or the key itself.
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
KEY_USAGE_URL = "https://openrouter.ai/api/v1/auth/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}")


class GenerationNotReady(OpenRouterError):
    """404 / "not found": the generation is not queryable yet; retry later."""


class OpenRouterAuthError(OpenRouterError):
    """401/403: the key cannot read this billing data; not a per-row failure."""


class OpenRouterHTTPError(OpenRouterError):
    """Any other non-200 status (429 and 5xx included)."""

    def __init__(self, status):
        super().__init__("http_" + str(status))
        self.status = status


def _optional_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _optional_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _billing_get(http, key, url, *, params=None, timeout=(5, 15)):
    if not key:
        raise OpenRouterAuthError("no_key")
    response = http.get(url, headers={"Authorization": "Bearer " + key}, params=params,
                        timeout=timeout, allow_redirects=False)
    try:
        status = getattr(response, "status_code", 200)
        if status == 404:
            raise GenerationNotReady("not_found")
        if status in (401, 403):
            raise OpenRouterAuthError("http_" + str(status))
        if status != 200:
            raise OpenRouterHTTPError(status)
        try:
            payload = response.json()
        except ValueError:
            raise OpenRouterError("malformed_json") from None
    finally:
        with contextlib.suppress(Exception):
            response.close()
    if not isinstance(payload, dict):
        raise OpenRouterError("malformed_payload")
    error = payload.get("error")
    if error:
        message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
        code = error.get("code") if isinstance(error, dict) else None
        if code == 404 or "not found" in message:
            raise GenerationNotReady("not_found")
        if code in (401, 403):
            raise OpenRouterAuthError("http_" + str(code))
        raise OpenRouterError("provider_error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OpenRouterError("malformed_payload")
    return data


def fetch_generation(http, key, generation_id, *, timeout=(5, 15)):
    """Authoritative charge of one generation (``GET /api/v1/generation?id=``).

    Token counts prefer the native (billed) counts and fall back to OpenRouter's
    normalized ``tokens_*`` fields; ``cached_tokens`` is ``native_tokens_cached``.
    """
    if not isinstance(generation_id, str) or not _GENERATION_ID.fullmatch(generation_id):
        raise OpenRouterError("invalid_generation_id")
    data = _billing_get(http, key, GENERATION_URL, params={"id": generation_id}, timeout=timeout)
    if data.get("id") not in (None, generation_id):
        raise OpenRouterError("generation_id_mismatch")
    cost = _optional_number(data.get("total_cost"))
    if cost is None:
        cost = _optional_number(data.get("usage"))
    if cost is None:
        raise OpenRouterError("missing_total_cost")

    def tokens(native, normalized):
        value = _optional_count(data.get(native))
        return value if value is not None else _optional_count(data.get(normalized))

    cancelled = data.get("cancelled")
    finish = data.get("finish_reason")
    return {"id": generation_id, "total_cost": cost,
            "prompt_tokens": tokens("native_tokens_prompt", "tokens_prompt"),
            "completion_tokens": tokens("native_tokens_completion", "tokens_completion"),
            "cached_tokens": _optional_count(data.get("native_tokens_cached")),
            "cancelled": cancelled if isinstance(cancelled, bool) else None,
            "finish_reason": finish if isinstance(finish, str) and re.fullmatch(r"[a-z_]{1,40}", finish) else None}


def fetch_key_usage(http, key, *, timeout=(5, 15)):
    """USD usage of THIS key (``GET /api/v1/auth/key``); shared by every app using it."""
    data = _billing_get(http, key, KEY_USAGE_URL, timeout=timeout)
    return {name: _optional_number(data.get(name)) for name in
            ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining")}


def fetch_credits(http, key, *, timeout=(5, 15)):
    """Account credits and total usage (``GET /api/v1/credits``)."""
    data = _billing_get(http, key, CREDITS_URL, timeout=timeout)
    return {name: _optional_number(data.get(name)) for name in ("total_credits", "total_usage")}


# ---------------------------------------------------------------------------
# Alibaba Cloud Model Studio (Bailian / DashScope), international deployment.
#
# Same receipts, metering hooks, speech probe and drain-on-cancel as OpenRouter:
# AlibabaClient is an OpenRouterClient with another endpoint, request body and usage
# normalisation (owner decision 2026-10-09: provider switch OpenRouter <-> Alibaba).
#   * One reviewed workspace endpoint (OpenAI-compatible; allow-listed international
#     host), no provider routing object, no fallbacks, no max_price ceilings.
#   * Thinking: a hybrid model always gets an explicit ``enable_thinking`` (False
#     unless the option enables it); thinking output is bounded by ``thinking_budget``
#     or by a total output cap. ``reasoning_content`` deltas reach the private event
#     sink only and are never yielded as speech.
#   * Model Studio reports tokens (``prompt_tokens``, ``completion_tokens``,
#     ``prompt_tokens_details.cached_tokens`` = implicit context cache) but no cost.
#     The charge is computed from the reviewed price table (pricing.json
#     ``models["alibaba/<model>"]``: tiered by input tokens per request, peak/off-peak
#     where published, cached input at its own rate, thinking output where it differs).
#   * There is no generation lookup: a call settles from its final usage chunk (drained
#     streams included). A call without usage keeps its reservation; f_settle then
#     estimates it like any call without a generation id (``no_id``).
ALIBABA = "alibaba"
ALIBABA_THINKING_MODES = ("hybrid", "always", "never")
ALIBABA_CAP_PARAMS = ("max_tokens", "max_tokens_total", "max_completion_tokens")
ALIBABA_ANSWER_TOKENS = {"surface": 1024, "deep": 4096, "reflection": 4096, "memory_write": 4096}
ALIBABA_THINKING_TOKENS = {"surface": 512, "deep": 1024, "reflection": 2048, "memory_write": 1024}
ALIBABA_MAX_THINKING_BUDGET = 32768
_ALIBABA_HOST = re.compile(r"(?:ws-[a-z0-9]{6,64}\.ap-southeast-1\.maas\.aliyuncs\.com|dashscope-intl\.aliyuncs\.com)")
_ALIBABA_API_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}")
_ALIBABA_PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "output_thinking")
_ALIBABA_SNAPSHOT = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{4})")


def alibaba_base_url(url):
    """The reviewed Model Studio OpenAI-compatible base URL, normalised, or ValueError.

    Only https, an allow-listed international host (the workspace domain
    ``ws-<id>.ap-southeast-1.maas.aliyuncs.com`` or the legacy ``dashscope-intl``),
    no credentials/port/query/fragment and the path ``/compatible-mode/v1``. Website
    requests never supply it; the key is never sent anywhere else.
    """
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A reviewed Alibaba Model Studio endpoint is required")
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid Alibaba endpoint port") from None
    if parts.scheme != "https" or parts.username or parts.password or port is not None or parts.query or parts.fragment:
        raise ValueError("The Alibaba endpoint must be a plain https URL")
    host = (parts.hostname or "").lower()
    if not _ALIBABA_HOST.fullmatch(host):
        raise ValueError("The Alibaba endpoint host is not an allowed Model Studio international host")
    if parts.path.rstrip("/") != "/compatible-mode/v1":
        raise ValueError("Use the OpenAI-compatible Model Studio base ending in /compatible-mode/v1")
    return "https://" + host + "/compatible-mode/v1"


def load_alibaba_catalog(path):
    """Read the reviewed Model Studio catalog (config/alibaba_models.json); never discover models."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("provider") != ALIBABA:
        raise ValueError("Invalid Alibaba model catalog")
    endpoint = data.get("endpoint") if isinstance(data.get("endpoint"), dict) else {}
    base = alibaba_base_url(endpoint.get("base_url"))
    models = {}
    for row in data.get("models") or []:
        if not isinstance(row, dict):
            raise ValueError("Alibaba catalog rows must be objects")
        api = row.get("api_model")
        if not isinstance(api, str) or not _ALIBABA_API_MODEL.fullmatch(api) or row.get("model_id") != ALIBABA + "/" + api:
            raise ValueError("Alibaba catalog rows need model_id = 'alibaba/' + api_model")
        if row["model_id"] in models:
            raise ValueError("Alibaba catalog model IDs must be unique")
        if row.get("thinking") not in ALIBABA_THINKING_MODES:
            raise ValueError("Unknown thinking mode for " + api)
        if row.get("cap_param", "max_tokens") not in ALIBABA_CAP_PARAMS:
            raise ValueError("Unknown output cap parameter for " + api)
        if row.get("json_mode") not in (None, "json_object", "json_schema"):
            raise ValueError("Unknown JSON mode for " + api)
        models[row["model_id"]] = copy.deepcopy(row)
    if not models:
        raise ValueError("Empty Alibaba model catalog")
    return {"endpoint": dict(endpoint, base_url=base), "models": models, "snapshot_date": data.get("snapshot_date")}


def _alibaba_rates(entry, label):
    if not isinstance(entry, dict):
        raise ValueError("Alibaba price entries must be objects: " + label)
    rates = {}
    for key in _ALIBABA_PRICE_KEYS:
        if entry.get(key) is not None:
            rates[key] = _number(entry[key], label + " " + key + " price")
    if rates.get("input", 0) <= 0 or rates.get("output", 0) <= 0:
        raise ValueError("Alibaba input and output prices must be explicitly positive: " + label)
    return rates


def alibaba_price(pricing, model_id):
    """The reviewed Singapore list price of one Model Studio model (pricing.json ``models[model_id]``).

    Top-level ``input``/``output``/``cache_read`` (USD per 1M tokens) are the first tier,
    also shown in the usage card. Optional: ``output_thinking`` (thinking-mode output),
    ``cache_write`` (explicit cache creation; default 125 % of input), ``tiers`` (by
    input tokens per request; all tokens of a request are billed at its tier) and
    ``time_of_day`` (peak hours at a UTC offset, with an ``offpeak`` price).
    """
    models = pricing.get("models") if isinstance(pricing, dict) else None
    entry = (models or {}).get(model_id)
    if not isinstance(entry, dict):
        raise ValueError("No reviewed Alibaba price for " + str(model_id))
    base = _alibaba_rates(entry, model_id)
    tiers, previous = [], 0
    for tier in entry.get("tiers") or []:
        limit = tier.get("max_input_tokens") if isinstance(tier, dict) else None
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= previous:
            raise ValueError("Alibaba price tiers need increasing integer max_input_tokens: " + model_id)
        tiers.append(dict(_alibaba_rates(tier, model_id + " tier"), max_input_tokens=limit))
        previous = limit
    if tiers and any(tiers[0].get(key) != value for key, value in base.items()):
        raise ValueError("The listed price must equal the first tier: " + model_id)
    if not tiers:
        tiers = [dict(base, max_input_tokens=None)]
    window = None
    peak = entry.get("time_of_day")
    if peak is not None:
        if not isinstance(peak, dict) or len(tiers) > 1:
            raise ValueError("Peak/off-peak prices are supported for untiered models only: " + model_id)
        offset, start, end = peak.get("utc_offset_hours"), peak.get("peak_start_hour"), peak.get("peak_end_hour")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, start, end)):
            raise ValueError("Invalid peak hours: " + model_id)
        if not -12 <= offset <= 14 or not 0 <= start < end <= 24:
            raise ValueError("Invalid peak hours: " + model_id)
        window = {"utc_offset_hours": offset, "peak_start_hour": start, "peak_end_hour": end,
                  "offpeak": _alibaba_rates(peak.get("offpeak"), model_id + " off-peak")}
    rows = tiers + ([window["offpeak"]] if window else [])
    ceiling = {"input": max(max(r["input"], r.get("cache_read", 0), r.get("cache_write", r["input"] * 1.25)) for r in rows),
               "output": max(max(r["output"], r.get("output_thinking", 0)) for r in rows)}
    return {"model_id": model_id, "base": base, "tiers": tiers, "time_of_day": window, "ceiling": ceiling,
            "source": entry.get("price_source") or entry.get("source"), "checked_at": entry.get("price_date")}


def _alibaba_offpeak(window, at):
    if not window or not isinstance(at, datetime):
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    hour = (at.astimezone(timezone.utc) + timedelta(hours=window["utc_offset_hours"])).hour
    return not window["peak_start_hour"] <= hour < window["peak_end_hour"]


def alibaba_cost(price, tokens, *, thinking=False, at=None):
    """(USD, basis, warnings) for one call from normalised token counts and the price entry."""
    warnings = []
    prompt = tokens["total_input_tokens"]
    tier = next((t for t in price["tiers"] if t["max_input_tokens"] is None or prompt <= t["max_input_tokens"]), None)
    if tier is None:
        tier = price["tiers"][-1]
        warnings.append("input_beyond_listed_price_tiers")
    offpeak = _alibaba_offpeak(price["time_of_day"], at)
    rates = price["time_of_day"]["offpeak"] if offpeak else tier
    output = rates.get("output_thinking", rates["output"]) if thinking else rates["output"]
    cached = rates.get("cache_read")
    if cached is None and tokens["cache_read_tokens"]:
        cached = rates["input"]
        warnings.append("cached_tokens_without_listed_cache_price")
    written = rates.get("cache_write", rates["input"] * 1.25)
    cost = (tokens["input_uncached_tokens"] * rates["input"] + tokens["cache_read_tokens"] * (cached or 0)
            + tokens["cache_write_tokens"] * written + tokens["output_tokens"] * output) / 1e6
    basis = {"tier_max_input_tokens": tier["max_input_tokens"], "offpeak": offpeak, "thinking_output_rate": bool(thinking),
             "usd_per_million": {"input": rates["input"], "cache_read": cached, "cache_write": written, "output": output},
             "price_checked_at": price.get("checked_at"), "price_source": price.get("source")}
    return cost, basis, warnings


def normalize_alibaba_usage(raw, price, *, thinking=False, at=None):
    """Model Studio usage -> the normalize_usage() shape, cost from the reviewed price table.

    A thinking call is billed at the thinking output rate unless the provider reports
    zero reasoning tokens (Model Studio bills such a call at the non-thinking rate).
    Missing counts leave the cost unknown (reservation held), never estimated here.
    """
    raw = raw if isinstance(raw, dict) else {}
    issues, warnings = [], []

    def count(mapping, key, required=False):
        value = mapping.get(key)
        if value is None and not required:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append("invalid_or_missing_" + key)
            return 0
        return value
    prompt = count(raw, "prompt_tokens", True)
    completion = count(raw, "completion_tokens", True)
    pd = raw.get("prompt_tokens_details")
    pd = pd if isinstance(pd, dict) else {}
    cached, written = count(pd, "cached_tokens"), count(pd, "cache_creation_input_tokens")
    if not written and isinstance(pd.get("cache_creation"), dict):
        written = count(pd["cache_creation"], "ephemeral_5m_input_tokens")
    cd = raw.get("completion_tokens_details")
    cd = cd if isinstance(cd, dict) else {}
    reported = cd.get("reasoning_tokens")
    reasoning = count(cd, "reasoning_tokens")
    if cached + written > prompt:
        issues.append("cache_tokens_exceed_prompt")
    if reasoning > completion:
        warnings.append("reasoning_count_exceeds_completion_provider_convention")
    tokens = {"total_input_tokens": prompt, "input_uncached_tokens": max(0, prompt - cached - written),
              "cache_read_tokens": cached, "cache_write_tokens": written, "output_tokens": completion,
              "reasoning_tokens": reasoning, "total_tokens": prompt + completion}
    cost = basis = None
    if not isinstance(price, dict) or not price.get("tiers"):
        issues.append("missing_reviewed_price")
    elif not issues:
        zero = isinstance(reported, int) and not isinstance(reported, bool) and reported == 0
        cost, basis, extra = alibaba_cost(price, tokens, thinking=bool(thinking) and not zero, at=at)
        warnings.extend(extra)
    return {"cost_usd": cost, "complete": not issues and cost is not None, "issues": issues, "warnings": warnings,
            "cost_source": "price_table", "price_basis": basis, "tokens": tokens}


def alibaba_role_config(catalog, model_id, role, *, pricing, max_tokens=None, reasoning_enabled=None,
                        thinking_budget=None, reasoning_effort=None):
    """Conservative per-role parameters for a reviewed Model Studio model.

    Thinking is off unless the option enables it (``reasoning_enabled``); a thinking-only
    model cannot be switched off and a non-thinking model cannot think. Thinking output is
    bounded by ``thinking_budget`` (per-role default) or by a total output cap, depending
    on the model's ``cap_param``; an unboundable configuration is refused.
    """
    role = "memory_write" if role == "memory_extraction" else role
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if role not in ROLES or not models or model_id not in models:
        raise ValueError("Unknown role or unreviewed Alibaba model")
    row = models[model_id]
    if row.get("available") is False:
        raise ValueError("This model is not served by the reviewed Model Studio workspace")
    price = alibaba_price(pricing, model_id)
    mode = row["thinking"]
    if reasoning_enabled is not None and not isinstance(reasoning_enabled, bool):
        raise ValueError("reasoning_enabled must be boolean")
    if mode == "always":
        if reasoning_enabled is False:
            raise ValueError("This model cannot disable thinking")
        thinking = True
    elif mode == "never":
        if reasoning_enabled:
            raise ValueError("This model has no thinking mode")
        thinking = False
    else:
        thinking = bool(reasoning_enabled)
    if role == "memory_write" and thinking and row.get("thinking_requires_stream"):
        raise ValueError("This model thinks only in streaming calls; the memory writer is not streamed")
    answer = ALIBABA_ANSWER_TOKENS[role] if max_tokens is None else max_tokens
    if isinstance(answer, bool) or not isinstance(answer, int) or not 1 <= answer <= MAX_OUTPUT_TOKENS:
        raise ValueError("max_tokens must be an integer between 1 and 8192")
    cap = row.get("cap_param", "max_tokens")
    budget = None
    if thinking:
        budget = ALIBABA_THINKING_TOKENS[role] if thinking_budget is None else thinking_budget
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= ALIBABA_MAX_THINKING_BUDGET:
            raise ValueError("thinking_budget must be an integer between 1 and 32768")
        if cap == "max_tokens" and not row.get("thinking_budget_supported"):
            raise ValueError("This model's thinking output cannot be bounded")
    elif thinking_budget is not None:
        raise ValueError("thinking_budget applies only when thinking is enabled")
    if reasoning_effort is not None and (not thinking or reasoning_effort not in (row.get("reasoning_efforts") or [])):
        raise ValueError("Unsupported reasoning effort for this model")
    total = answer + (budget or 0)
    request_max = total if thinking and cap == "max_tokens_total" else answer
    limit = row.get("max_output_tokens")
    if limit and total > limit:
        raise ValueError("Output limit exceeds the selected model")
    send_budget = budget if thinking and row.get("thinking_budget_supported") and cap != "max_tokens_total" else None
    ceiling = price["ceiling"]
    return {"transport": ALIBABA, "model_id": model_id, "api_model": row["api_model"],
            "canonical_slug": row["api_model"], "provider_tag": ALIBABA, "provider_name": "Alibaba Cloud Model Studio",
            "quantization": None, "role": role, "max_tokens": answer, "request_max_tokens": request_max,
            "output_cap_total": total, "cap_param": cap, "thinking_mode": mode, "reasoning_enabled": thinking,
            "mandatory_reasoning": mode == "always", "thinking_budget": send_budget, "reasoning_effort": reasoning_effort,
            "reasoning": {"enable_thinking": thinking, "thinking_budget": budget, "reasoning_effort": reasoning_effort},
            "json_mode": row.get("json_mode"), "implicit_cache": bool(row.get("implicit_cache")),
            "max_price": {"prompt": ceiling["input"], "completion": ceiling["output"], "request": 0.0},
            "reservation_input_rate": ceiling["input"], "price": price,
            "endpoint_context_tokens": row.get("context_tokens"), "endpoint_max_completion_tokens": limit,
            "supported_parameters": [], "structured_outputs_advertised": row.get("json_mode") == "json_schema",
            "snapshot_time": catalog.get("snapshot_date")}


def make_alibaba_body(config, messages, *, stream=True, json_schema=None):
    """OpenAI-compatible Model Studio request: no routing object, explicit thinking control."""
    if config.get("transport") != ALIBABA or config.get("role") not in ROLES or not config.get("api_model"):
        raise ValueError("Use a validated Alibaba role config")
    if not isinstance(messages, list) or not messages:
        raise ValueError("A nonempty message list is required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise ValueError("Only system, user, and assistant text messages are supported")
        if not isinstance(message.get("content"), str):
            raise ValueError("This transport accepts text-only messages")
    thinking = bool(config["reasoning_enabled"])
    body = {"model": config["api_model"], "messages": copy.deepcopy(messages), "stream": bool(stream)}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if config["thinking_mode"] == "hybrid":
        body["enable_thinking"] = thinking
    if thinking and config.get("thinking_budget") is not None:
        body["thinking_budget"] = config["thinking_budget"]
    if thinking and config.get("reasoning_effort"):
        body["reasoning_effort"] = config["reasoning_effort"]
    if thinking and config["cap_param"] == "max_completion_tokens":
        body["max_completion_tokens"] = config["output_cap_total"]
    else:
        body["max_tokens"] = config["request_max_tokens"]
    mode = config.get("json_mode") if (config["role"] == "memory_write" or json_schema is not None) else None
    if mode == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "amika_memory_facts", "strict": True, "schema": copy.deepcopy(json_schema or FACT_SCHEMA)}}
    elif mode == "json_object":
        if not any("json" in message["content"].lower() for message in messages):
            raise ValueError("JSON object mode requires the word JSON in the prompt")
        body["response_format"] = {"type": "json_object"}
    _body_size(body, config)
    return body


def estimate_alibaba_reservation(config, body):
    """Conservative bound: UTF-8 bytes at the highest listed input rate plus the total output cap."""
    _, bound = _body_size(body, config)
    price = config["max_price"]
    value = (bound * config["reservation_input_rate"] + config["output_cap_total"] * price["completion"]) / 1e6
    return value * 1.10 + 0.00001


def _alibaba_model_matches(config, actual):
    api = config.get("api_model") or ""
    if actual in (api, config.get("model_id")):
        return True
    return bool(api) and actual.startswith(api) and bool(_ALIBABA_SNAPSHOT.fullmatch(actual[len(api):]))


class AlibabaClient(OpenRouterClient):
    """Model Studio chat completions with the OpenRouter client's receipts, metering and drain."""
    transport = ALIBABA

    def __init__(self, http, api_key, before_call, after_call, *, base_url, timeout=(10, 120), event_sink=None,
                 drainer=None):
        super().__init__(http, api_key, before_call, after_call, timeout=timeout, event_sink=event_sink,
                         drainer=drainer)
        self.chat_url = alibaba_base_url(base_url) + "/chat/completions"

    def _chat_body(self, config, messages, *, stream, json_schema=None):
        return make_alibaba_body(config, messages, stream=stream, json_schema=json_schema)

    def _reservation(self, config, body):
        return estimate_alibaba_reservation(config, body)

    def _prepare(self, config, body, metadata, url):
        handle, receipt = super()._prepare(config, body, metadata, url)
        # Everything the settle step needs to price the call, archived with the receipt.
        receipt["alibaba"] = {"api_model": config["api_model"], "thinking": bool(config["reasoning_enabled"]),
                              "price": copy.deepcopy(config["price"])}
        return handle, receipt

    def _usage(self, receipt, embedding):
        info = receipt.get("alibaba") or {}
        try:
            at = datetime.fromisoformat(receipt.get("started_at"))
        except (TypeError, ValueError):
            at = None
        return normalize_alibaba_usage(receipt["raw_usage"], info.get("price"), thinking=info.get("thinking", False),
                                       at=at)

    def _post(self, url, body, stream):
        return self.http.post(url, headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
                              json=body, stream=stream, timeout=self.timeout, allow_redirects=False)

    def _event(self, config, receipt, data):
        if not isinstance(data, dict):
            raise OpenRouterError("Provider returned a non-object event")
        if data.get("id"):
            receipt["request_id"] = str(data["id"])
        if data.get("model"):
            receipt["actual_model"] = str(data["model"])
        receipt["actual_provider"] = ALIBABA
        if isinstance(data.get("usage"), dict):
            _merge(receipt["raw_usage"], data["usage"])
        if data.get("error") or (data.get("code") and data.get("message") and not data.get("choices")):
            raise OpenRouterError("Model Studio returned an error event")
        if self.event_sink:
            self.event_sink(copy.deepcopy(data))
        actual = receipt["actual_model"]
        if actual and not _alibaba_model_matches(config, actual) and "model_mismatch" not in receipt["issues"]:
            receipt["issues"].append("model_mismatch")

    def embed(self, *args, **kwargs):
        raise ValueError("The Alibaba transport serves chat roles only")


def fetch_alibaba_models(http, key, base_url, *, timeout=(5, 20)):
    """Model IDs the workspace serves (``GET {base}/models``): one read-only call, no retry."""
    if not key:
        raise OpenRouterAuthError("no_key")
    url = alibaba_base_url(base_url) + "/models"
    response = http.get(url, headers={"Authorization": "Bearer " + key}, timeout=timeout, allow_redirects=False)
    try:
        status = getattr(response, "status_code", 200)
        if status in (401, 403):
            raise OpenRouterAuthError("http_" + str(status))
        if status != 200:
            raise OpenRouterHTTPError(status)
        try:
            payload = response.json()
        except ValueError:
            raise OpenRouterError("malformed_json") from None
    finally:
        with contextlib.suppress(Exception):
            response.close()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OpenRouterError("malformed_payload")
    return sorted({row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)})


# Generic OpenAI-compatible endpoint (opt-in, owner decision 2026-11-05).
#
# Same receipts, metering hooks and drain as OpenRouter, with no reviewed host list:
# the person names the endpoint and enters the prices, so nothing is discovered here.
#   * ``custom_base_url`` accepts https anywhere, and http for localhost/127.0.0.1
#     only, which is the case a local vLLM or LM Studio server needs. Credentials,
#     a query string or a fragment in the URL are refused, so a key cannot ride along.
#   * No provider routing object and no max_price ceiling: a generic server may
#     reject ``provider`` or ignore it, and there is no catalogue to price against.
#   * Prices come from the person (USD per 1M tokens, zero allowed for local servers
#     and subscription plans) and are held in the catalog row. The receipt is priced
#     from those rates, never from a provider-reported charge, so the ledger stays
#     comparable with the other transports.
#   * No thinking control: ``reasoning_enabled`` is refused rather than silently
#     dropped, because a generic endpoint has no agreed field for it.
CUSTOM = "custom"
CUSTOM_ANSWER_TOKENS = {"surface": 1024, "deep": 4096, "reflection": 4096, "memory_write": 4096}
_CUSTOM_API_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}")
_CUSTOM_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


_CUSTOM_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


def custom_base_url(url, *, allow_local_http=True):
    """The OpenAI-compatible base URL the person typed, normalised, ending in /v1.

    https is required except for a loopback host, where http is accepted when
    ``allow_local_http`` is true. No credentials, query, fragment or whitespace.
    """
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A base URL is required")
    if any(ch.isspace() for ch in url.strip()):
        raise ValueError("The base URL must not contain spaces")
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid port in the base URL") from None
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("The base URL needs a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The base URL cannot carry credentials, a query or a fragment")
    local = host in _CUSTOM_LOCAL_HOSTS
    if parts.scheme == "http":
        if not (allow_local_http and local):
            raise ValueError("http is accepted for localhost only; use https")
    elif parts.scheme != "https":
        raise ValueError("The base URL must be https, or http on localhost")
    # Any path that ends in /v1, because plenty of real servers live under a prefix
    # (https://opencode.ai/zen/v1, a LiteLLM proxy at /api/v1). Dot segments are refused so the
    # stored base cannot be made to read as something other than what the person typed.
    if "//" in parts.path.strip("/"):
        raise ValueError("The base URL path has an empty segment")
    segments = [piece for piece in parts.path.split("/") if piece]
    if not segments or segments[-1] != "v1":
        raise ValueError("Use the OpenAI-compatible base ending in /v1")
    if any(piece in (".", "..") or not _CUSTOM_PATH_SEGMENT.match(piece) for piece in segments):
        raise ValueError("The base URL path has characters this field cannot carry")
    # A literal IPv6 host needs its brackets back, or the stored base is not a URL anymore.
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    netloc = host if port is None else host + ":" + str(port)
    return parts.scheme + "://" + netloc + "/" + "/".join(segments)


def _custom_prices(raw, label):
    if not isinstance(raw, dict):
        raise ValueError("Prices are required for " + label)
    prices = {}
    for key in ("input", "output", "cache_read"):
        value = raw.get(key)
        if value is None and key == "cache_read":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Prices must be finite numbers at or above zero: " + label)
        prices[key] = float(value)
    return prices


def load_custom_catalog(path):
    """Read the person's config/custom_models.json: endpoint, models and their prices.

    Nothing is discovered and nothing is defaulted: a row without both an input and
    an output price is refused, because the meter will not reserve an unpriced model.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("provider") != CUSTOM:
        raise ValueError("Invalid custom model catalog")
    endpoint = data.get("endpoint") if isinstance(data.get("endpoint"), dict) else {}
    base = custom_base_url(endpoint.get("base_url"))
    models = {}
    for row in data.get("models") or []:
        if not isinstance(row, dict):
            raise ValueError("Custom catalog rows must be objects")
        api = row.get("api_model")
        if not isinstance(api, str) or not _CUSTOM_API_MODEL.fullmatch(api) or row.get("model_id") != CUSTOM + "/" + api:
            raise ValueError("A custom row needs model_id custom/<api_model>")
        prices = _custom_prices(row.get("prices"), api)
        if "input" not in prices or "output" not in prices:
            raise ValueError("A custom row needs input and output prices: " + api)
        context = row.get("context")
        if context is not None and (isinstance(context, bool) or not isinstance(context, int) or context <= 0):
            raise ValueError("A custom row context must be a positive integer: " + api)
        if row.get("model_id") in models:
            raise ValueError("Custom catalog needs unique model IDs")
        # Validate the mode here, not only where a role config is built: a catalog this loader
        # accepts is written to disk, and the manager refuses it at the next boot.
        if row.get("json_mode") not in (None, "json_schema"):
            # A custom model is either asked for JSON schema or not asked at all; json_object has no
            # path through this client, so accepting it in a saved row would promise what cannot run.
            raise ValueError("A custom row json_mode must be json_schema or absent: " + api)
        models[row["model_id"]] = {"model_id": row["model_id"], "api_model": api, "context": context,
                                  "prices": prices, "json_mode": row.get("json_mode")}
    if not models:
        raise ValueError("Custom catalog has no models")
    return {"base": base, "models": models}


def custom_price(catalog, model_id, pricing=None):
    """The price entry for one custom model, from the person's catalog row."""
    models = (catalog or {}).get("models") or {}
    row = models.get(model_id)
    if row is None:
        raise ValueError("Unknown custom model")
    prices = dict(row["prices"])
    prices.setdefault("cache_read", prices["input"])
    return {"input": prices["input"], "output": prices["output"], "cache_read": prices["cache_read"],
            "model_id": model_id, "source": "person", "checked_at": None}


def custom_cost(price, tokens):
    """(USD, basis) for one call from normalised token counts and the person's rates."""
    rates = {"input": price["input"], "output": price["output"], "cache_read": price["cache_read"]}
    cost = (tokens["input_uncached_tokens"] * rates["input"] + tokens["cache_read_tokens"] * rates["cache_read"]
            + tokens["output_tokens"] * rates["output"]) / 1e6
    return cost, {"usd_per_million": rates, "price_source": price.get("source")}


def normalize_custom_usage(raw, price):
    """OpenAI-style usage -> the normalize_usage() shape, priced at the person's rates.

    Missing counts leave the cost unknown, so the reservation stays held.
    """
    raw = raw if isinstance(raw, dict) else {}
    issues = []
    def count(key, required=False):
        value = raw.get(key)
        if value is None and not required:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append("invalid_or_missing_" + key)
            return 0
        return value
    prompt = count("prompt_tokens", True)
    completion = count("completion_tokens", True)
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    # Two conventions reach this function, and the ledger already reads both: OpenAI-style
    # prompt_tokens_details.cached_tokens, and DeepSeek's prompt_cache_hit_tokens. Reading only the
    # first charged every one of those tokens at the input rate while the ledger recorded them as cache.
    cached = details.get("cached_tokens")
    if cached is None:
        cached = raw.get("prompt_cache_hit_tokens")
    cached = cached if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0 else 0
    if cached > prompt:
        issues.append("cache_tokens_exceed_prompt")
        cached = prompt
    details_out = raw.get("completion_tokens_details")
    details_out = details_out if isinstance(details_out, dict) else {}
    reasoning = details_out.get("reasoning_tokens")
    reasoning = reasoning if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0 else 0
    tokens = {"total_input_tokens": prompt, "input_uncached_tokens": max(0, prompt - cached),
              "cache_read_tokens": cached, "cache_write_tokens": 0, "output_tokens": completion,
              "reasoning_tokens": reasoning, "total_tokens": prompt + completion}
    cost = basis = None
    if not isinstance(price, dict) or "input" not in price:
        issues.append("missing_price")
    elif not issues:
        cost, basis = custom_cost(price, tokens)
    return {"cost_usd": cost, "complete": not issues and cost is not None, "issues": issues, "warnings": [],
            "cost_source": "price_table", "price_basis": basis, "tokens": tokens}


def custom_role_config(catalog, model_id, role, *, max_tokens=None, reasoning_enabled=None, pricing=None):
    """Conservative per-role parameters for one custom model."""
    role = "memory_write" if role == "memory_extraction" else role
    models = (catalog or {}).get("models") or {}
    if role not in ROLES or model_id not in models:
        raise ValueError("Unknown role or custom model")
    row = models[model_id]
    if reasoning_enabled:
        raise ValueError("Custom endpoints have no reasoning control; leave it off")
    answer = CUSTOM_ANSWER_TOKENS[role] if max_tokens is None else max_tokens
    if isinstance(answer, bool) or not isinstance(answer, int) or not 1 <= answer <= MAX_OUTPUT_TOKENS:
        raise ValueError("max_tokens must be an integer between 1 and 8192")
    price = custom_price(catalog, model_id, pricing)
    json_mode = row.get("json_mode")
    if json_mode not in (None, "json_object", "json_schema"):
        raise ValueError("json_mode must be json_object, json_schema or absent")
    if role == "memory_write" and json_mode != "json_schema":
        raise ValueError("The memory writer needs an endpoint that supports a JSON schema")
    ceiling = {"prompt": max(price["input"], price["cache_read"]), "completion": price["output"], "request": 0.0}
    return {"transport": CUSTOM, "model_id": model_id, "api_model": row["api_model"], "canonical_slug": row["api_model"],
            "provider_tag": CUSTOM, "provider_name": "Custom OpenAI-compatible", "quantization": None, "role": role,
            "max_tokens": answer, "request_max_tokens": answer, "output_cap_total": answer, "cap_param": "max_tokens",
            "thinking_mode": "never", "reasoning_enabled": False, "mandatory_reasoning": False,
            "thinking_budget": None, "reasoning_effort": None, "reasoning": None,
            "json_mode": json_mode, "implicit_cache": price["cache_read"] > 0,
            "max_price": ceiling, "reservation_input_rate": ceiling["prompt"], "price": price,
            "endpoint_context_tokens": row.get("context"), "endpoint_max_completion_tokens": None,
            "supported_parameters": [], "structured_outputs_advertised": json_mode == "json_schema",
            "snapshot_time": None}


def make_custom_body(config, messages, *, stream=True, json_schema=None):
    """OpenAI-compatible request for a generic endpoint: no routing, no provider fields."""
    if config.get("transport") != CUSTOM or config.get("role") not in ROLES or not config.get("api_model"):
        raise ValueError("Use a validated custom role config")
    if not isinstance(messages, list) or not messages:
        raise ValueError("A nonempty message list is required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise ValueError("Only system, user, and assistant text messages are supported")
        if not isinstance(message.get("content"), str):
            raise ValueError("This transport accepts text-only messages")
    body = {"model": config["api_model"], "messages": copy.deepcopy(messages), "stream": bool(stream),
            "max_tokens": config["request_max_tokens"]}
    if stream:
        body["stream_options"] = {"include_usage": True}
    mode = config.get("json_mode") if (config["role"] == "memory_write" or json_schema is not None) else None
    if json_schema is not None and mode != "json_schema":
        raise ValueError("JSON schema is unsupported by this endpoint")
    if mode == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "amika_memory_facts", "strict": True, "schema": copy.deepcopy(json_schema or FACT_SCHEMA)}}
    _body_size(body, config)
    return body


def estimate_custom_reservation(config, body):
    """Conservative bound at the person's own rates, plus the same margin as the others."""
    _, bound = _body_size(body, config)
    price = config["max_price"]
    value = (bound * config["reservation_input_rate"] + config["output_cap_total"] * price["completion"]) / 1e6
    return value * 1.10 + 0.00001


class CustomClient(OpenRouterClient):
    """A person-supplied OpenAI-compatible endpoint with OpenRouter's receipts and metering."""

    transport = CUSTOM

    def __init__(self, http, api_key, before_call, after_call, *, base_url, timeout=(10, 120), event_sink=None,
                 drainer=None):
        super().__init__(http, api_key, before_call, after_call, timeout=timeout, event_sink=event_sink,
                         drainer=drainer)
        self.chat_url = custom_base_url(base_url) + "/chat/completions"

    def _chat_body(self, config, messages, *, stream, json_schema=None):
        return make_custom_body(config, messages, stream=stream, json_schema=json_schema)

    def _reservation(self, config, body):
        return estimate_custom_reservation(config, body)

    def _prepare(self, config, body, metadata, url):
        handle, receipt = super()._prepare(config, body, metadata, url)
        receipt[CUSTOM] = {"api_model": config["api_model"], "price": copy.deepcopy(config["price"])}
        return handle, receipt

    def _usage(self, receipt, embedding):
        info = receipt.get(CUSTOM) or {}
        return normalize_custom_usage(receipt["raw_usage"], info.get("price"))

    def _post(self, url, body, stream):
        return self.http.post(url, headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
                              json=body, stream=stream, timeout=self.timeout, allow_redirects=False)

    def _event(self, config, receipt, data):
        if not isinstance(data, dict):
            raise OpenRouterError("Provider returned a non-object event")
        if data.get("id"):
            receipt["request_id"] = str(data["id"])
        if data.get("model"):
            receipt["actual_model"] = str(data["model"])
        receipt["actual_provider"] = CUSTOM
        if isinstance(data.get("usage"), dict):
            _merge(receipt["raw_usage"], data["usage"])
        if data.get("error"):
            raise OpenRouterError("The endpoint returned an error event")
        if self.event_sink:
            self.event_sink(copy.deepcopy(data))
        actual = receipt["actual_model"]
        api = config.get("api_model") or ""
        if actual and actual not in (api, config.get("model_id")) and "model_mismatch" not in receipt["issues"]:
            receipt["issues"].append("model_mismatch")

    def embed(self, *args, **kwargs):
        raise ValueError("The custom transport serves chat roles only")


def fetch_custom_models(http, key, base_url, *, timeout=(5, 20)):
    """Model IDs the endpoint serves (``GET {base}/models``): one read-only call, no retry."""
    if not key:
        raise OpenRouterAuthError("no_key")
    url = custom_base_url(base_url) + "/models"
    response = http.get(url, headers={"Authorization": "Bearer " + key}, timeout=timeout, allow_redirects=False)
    try:
        status = getattr(response, "status_code", 200)
        if status in (401, 403):
            raise OpenRouterAuthError("http_" + str(status))
        if status != 200:
            raise OpenRouterHTTPError(status)
        try:
            payload = response.json()
        except ValueError:
            raise OpenRouterError("malformed_json") from None
    finally:
        with contextlib.suppress(Exception):
            response.close()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OpenRouterError("malformed_payload")
    return sorted({row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)})
