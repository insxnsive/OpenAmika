"""Persistent request-level usage accounting. No prompt, response, or credential storage.

All provider usage objects are cumulative snapshots, never additive deltas. Prices are
USD per million tokens and are copied onto each request when it starts. A configured
budget is a reservation guard, not a provider-side spending limit: callers must give
a genuinely conservative bound for each request and cap their provider output tokens.

Cost settlement (f_settle.py) adds, without touching the recorded columns: charges
looked up from OpenRouter for rows with a generation id (``settled_*``), estimates
for rows that can never be looked up (``estimated_unrecorded_usd``), and key-usage
snapshots. Summaries report them separately from the recorded cost, and the budget
counts settled and estimated amounts toward spend like recorded cost (see _budget).
Drained rows (status "drained", ``discarded=1``) are ordinary recorded usage.

Alibaba Model Studio rows (provider "alibaba", added 2026-10-09) report OpenAI-style
token usage but no charge. The transport prices them from the reviewed price table
(f_openrouter.normalize_alibaba_usage: input tiers, peak hours, cached input) and passes
that amount as ``reported_cost_usd``; it is recorded with ``cost_source`` "price_table".
Without it the row falls back to this meter's snapshot rates like any native provider.

Prompt cache: ``cache_reported`` (per row) says whether the provider's usage carried a
cache figure at all (OpenRouter / OpenAI-style ``prompt_tokens_details.cached_tokens``,
Anthropic ``cache_read_input_tokens``, Gemini on every chat call). Ollama and the local
embedder never report one, so their calls have no cache share ("n/a"), not 0 %. The
summaries' ``cache_hit_pct`` is cache reads over the input tokens of reporting calls only.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from f_settle import backfill, ensure_schema, key_usage_view, refresh_row, row_settlement


TOKEN_KEYS = (
    "input_uncached_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_unknown_tokens",
    "total_input_tokens", "total_tokens", "reasoning_tokens",
)
RATE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
# "drained": a cancelled turn's OpenRouter stream read to its end in the background
# (f_openrouter.StreamDrainer). Exact usage and charge, content discarded.
STATUS_VALUES = {"complete", "drained", "cancelled", "failed", "unknown"}
FINAL_STATUSES = ("complete", "drained")
# Providers that report tokens but no cost; their transport supplies the price-table amount.
PRICE_TABLE_PROVIDERS = {"alibaba", "custom"}
# Prompt cache. Embeddings are never served from a prompt cache; rows written before the
# per-row ``cache_reported`` flag existed are judged by what their provider normally reports.
EMBEDDING_PHASES = ("query_embedding", "memory_embedding")
CACHE_REPORTING_PROVIDERS = {"anthropic", "openai", "xai", "groq", "openrouter", "deepseek", "alibaba", "gemini"}


def row_cache_reported(row) -> bool:
    """Whether this ledger row's provider reported a prompt-cache figure (its cache share is known)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    value = row["cache_reported"] if "cache_reported" in keys else None
    if value is not None:
        return bool(value)
    model, phase = str(row["model"] or "").lower(), str(row["phase"] or "")
    # helper (agent:<server>) bookings are priced from the tokens the agent reports, with no cache figure
    return (row["provider"] in CACHE_REPORTING_PROVIDERS and phase not in EMBEDDING_PHASES
            and not phase.startswith("agent:") and "embed" not in model)


class BudgetExceeded(RuntimeError):
    """A request cannot safely be admitted within the configured test budget."""


class DisplayResetBusy(RuntimeError):
    """Display counters cannot be reset while a request is in flight."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _provider(value: str) -> str:
    return {"google": "gemini", "google-genai": "gemini", "grok": "xai",
            "dashscope": "alibaba", "bailian": "alibaba"}.get(value.lower(), value.lower())


def _label(value: Any, maximum: int = 160) -> str:
    value = str(value or "")
    if len(value) > maximum or not re.fullmatch(r"[\w.:/@+\- ]*", value, re.ASCII):
        raise ValueError("Usage labels must be short identifiers, not request content")
    if re.search(r"(?:sk-|AIza|Bearer )", value, re.I):
        raise ValueError("Credential-like usage label refused")
    return value


def _error_code(error: Any) -> str | None:
    if not error:
        return None
    # Never retain arbitrary exception or provider-response text.
    if isinstance(error, BaseException):
        name = type(error).__name__
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", name) else "request_error"
    value = str(error)
    allowed = {"cancelled", "timeout", "network_error", "provider_error", "request_error", "unknown"}
    if value in allowed:
        return value
    match = re.fullmatch(r"(?:HTTP[ _-]?)?([45]\d\d)", value, re.I)
    return "http_" + match.group(1) if match else "request_error"


def merge_usage(previous: dict | None, current: dict | None) -> dict:
    """Merge cumulative provider snapshots without summing repeatedly reported counts."""
    result = dict(previous or {})
    for key, value in (current or {}).items():
        if isinstance(value, dict):
            result[key] = merge_usage(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        elif (key.endswith("tokens") or key.endswith("TokenCount")) and isinstance(value, (int, float)) and isinstance(result.get(key), (int, float)):
            # Some streaming end events repeat zero defaults alongside output totals.
            result[key] = max(result[key], value)
        elif value is not None:
            result[key] = value
    return result


def normalize_usage(provider: str, usage: dict | None, model: str = "") -> dict:
    """Extract count fields only; missing required totals remain explicitly incomplete."""
    provider = _provider(provider)
    raw = usage if isinstance(usage, dict) else {}
    if isinstance(raw.get("usageMetadata"), dict):
        raw = raw["usageMetadata"]
    elif isinstance(raw.get("usage"), dict):
        raw = raw["usage"]
    tokens = dict.fromkeys(TOKEN_KEYS, 0)
    issues = []
    recognized_count = False

    def count(key: str, source: dict | None = None, required: bool = False) -> int:
        nonlocal recognized_count
        source = raw if source is None else source
        value = source.get(key)
        if value is None:
            if required:
                issues.append("missing_" + key)
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
            issues.append("invalid_" + key)
            return 0
        recognized_count = True
        return int(value)

    cache_reported = False      # did the usage carry a prompt-cache figure at all (see the module docstring)
    if provider == "anthropic":
        tokens["input_uncached_tokens"] = count("input_tokens", required=True)
        tokens["output_tokens"] = count("output_tokens", required=True)
        cache_reported = raw.get("cache_read_input_tokens") is not None or raw.get("cache_creation_input_tokens") is not None
        tokens["cache_read_tokens"] = count("cache_read_input_tokens")
        creation = count("cache_creation_input_tokens")
        breakdown = raw.get("cache_creation") or {}
        if not isinstance(breakdown, dict):
            breakdown = {}
            issues.append("invalid_cache_creation")
        if breakdown:
            five = count("ephemeral_5m_input_tokens", breakdown)
            hour = count("ephemeral_1h_input_tokens", breakdown)
            if "cache_creation_input_tokens" not in raw:
                creation = five + hour
            tokens["cache_write_5m_tokens"] = five
            tokens["cache_write_1h_tokens"] = hour
            tokens["cache_write_unknown_tokens"] = max(0, creation - five - hour)
            if five + hour != creation:
                issues.append("cache_creation_breakdown_mismatch")
        else:
            # Anthropic's ordinary cache TTL is five minutes when no split is supplied.
            tokens["cache_write_5m_tokens"] = creation
    elif provider in {"openai", "xai", "groq", "openrouter", "deepseek", "local", "alibaba", "custom"}:   # local = embed_server.py (OpenAI-style usage, zero price)
        prompt = count("prompt_tokens", required=True)
        embedding = (provider == "openrouter" and "embed" in model.lower()) or provider == "local"
        tokens["output_tokens"] = count("completion_tokens", required=not embedding)
        details = raw.get("prompt_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        if details.get("cached_tokens") is None and raw.get("prompt_cache_hit_tokens") is not None:
            cached = count("prompt_cache_hit_tokens")              # DeepSeek's own API
            cache_reported = not embedding
        else:
            cache_reported = details.get("cached_tokens") is not None and not embedding
            cached = count("cached_tokens", details)
        if cached > prompt:
            issues.append("cached_tokens_exceed_prompt")
            cached = prompt
        tokens["cache_read_tokens"] = cached
        tokens["input_uncached_tokens"] = prompt - cached
        details = raw.get("completion_tokens_details") or {}
        tokens["reasoning_tokens"] = count("reasoning_tokens", details if isinstance(details, dict) else {})
        # OpenAI-compatible completion_tokens already includes reasoning tokens.
    elif provider == "ollama":
        # Ollama's counts (f_ollama maps prompt_eval_count / eval_count onto prompt_tokens / completion_tokens).
        # Ollama reuses its own KV cache but never reports how much: these calls have no cache share.
        prompt_key = "prompt_tokens" if "prompt_tokens" in raw else "prompt_eval_count"
        output_key = "completion_tokens" if "completion_tokens" in raw else "eval_count"
        tokens["input_uncached_tokens"] = count(prompt_key, required=True)
        tokens["output_tokens"] = count(output_key, required=True)
    elif provider == "gemini":
        prompt = count("promptTokenCount", required=True)
        cached = count("cachedContentTokenCount")
        if cached > prompt:
            issues.append("cached_tokens_exceed_prompt")
            cached = prompt
        tokens["input_uncached_tokens"] = prompt - cached
        tokens["cache_read_tokens"] = cached
        embedding = "embed" in model.lower()
        cache_reported = not embedding     # Gemini leaves cachedContentTokenCount out when nothing was cached
        candidates = count("candidatesTokenCount", required=not embedding)
        thoughts = count("thoughtsTokenCount")
        tokens["output_tokens"] = candidates + thoughts
        tokens["reasoning_tokens"] = thoughts
    else:
        issues.append("unknown_provider")
    tokens["total_input_tokens"] = sum(tokens[k] for k in (
        "input_uncached_tokens", "cache_read_tokens", "cache_write_5m_tokens",
        "cache_write_1h_tokens", "cache_write_unknown_tokens"))
    tokens["total_tokens"] = tokens["total_input_tokens"] + tokens["output_tokens"]
    return {"tokens": tokens, "complete": not issues, "issues": issues, "has_usage": recognized_count,
            "cache_reported": bool(cache_reported and recognized_count)}


def _pricing(value: dict | str | Path | None) -> dict:
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_text(encoding="utf-8"))
    value = value or {}
    models = {}
    aliases = {"input_per_million": "input", "output_per_million": "output", "cache_read_per_million": "cache_read",
               "cache_write_5m_per_million": "cache_write_5m", "cache_write_1h_per_million": "cache_write_1h"}
    for name, supplied in value.get("models", {}).items():
        if not isinstance(supplied, dict):
            continue
        rates = {}
        for field, rate in supplied.items():
            key = aliases.get(field, field)
            if key in RATE_KEYS:
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
                    raise ValueError("Prices must be finite, nonnegative numbers")
                rates[key] = float(rate)
        models[_label(name)] = rates
    metadata = {}
    for key in ("currency", "unit", "checked_at", "source", "sources", "effective_at", "note", "name"):
        field = value.get("metadata", {}).get(key)
        if isinstance(field, (str, int, float, list)):
            metadata[key] = field
    metadata.setdefault("currency", "USD")
    metadata.setdefault("unit", "per_million_tokens")
    return {"models": models, "metadata": metadata}


class UsageMeter:
    def __init__(self, db_path: str | Path, pricing: dict | str | Path | None = None,
                 budget_usd: float | None = None, budget_scope: str = "deployment-test"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pricing = _pricing(pricing)
        self.budget_scope = _label(budget_scope) if budget_usd is not None else None
        self.budget_usd = None if budget_usd is None else float(budget_usd)
        if self.budget_usd is not None and (not math.isfinite(self.budget_usd) or self.budget_usd <= 0):
            raise ValueError("Budget must be a finite positive USD amount")
        self._lock = threading.RLock()
        self._finish_listeners = []
        # Set by f_settle.Settler / the bridge's StreamDrainer to report worker state
        # in summary(); None = not installed.
        self.settlement_worker = None
        self.drain_worker = None
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    phase TEXT NOT NULL, turn_id TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, tokens_json TEXT,
                    usage_complete INTEGER NOT NULL DEFAULT 0, pricing_status TEXT NOT NULL,
                    cost_usd REAL, pricing_json TEXT NOT NULL, issues_json TEXT NOT NULL,
                    error_code TEXT, budget_scope TEXT, reservation_usd REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS request_budget ON requests(budget_scope);
                CREATE TABLE IF NOT EXISTS budgets (scope TEXT PRIMARY KEY, limit_usd REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS display_window (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    after_rowid INTEGER NOT NULL DEFAULT 0, reset_at TEXT,
                    reset_count INTEGER NOT NULL DEFAULT 0, reset_id TEXT
                );
            """)
            # Additive migration; serialize independent meter instances as well as threads.
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
            for name, kind in (("estimated_cost_usd", "REAL"), ("cost_source", "TEXT"),
                               ("actual_model", "TEXT"), ("actual_provider", "TEXT"),
                               ("provider_request_id", "TEXT"), ("discarded", "INTEGER"),
                               # 1/0: the usage carried a prompt-cache figure; NULL: older row (row_cache_reported)
                               ("cache_reported", "INTEGER")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {kind}")
            # Settlement columns and key_usage_snapshots (additive, in place), then
            # classify finished unconfirmed rows an older build left unclassified.
            ensure_schema(conn)
            backfill(conn)
            conn.execute("INSERT OR IGNORE INTO display_window(singleton) VALUES (1)")
            if self.budget_scope:
                row = conn.execute("SELECT limit_usd FROM budgets WHERE scope=?", (self.budget_scope,)).fetchone()
                if row and abs(row[0] - self.budget_usd) > 1e-9:
                    raise ValueError("Existing budget scope has a different limit; use a new explicit scope")
                conn.execute("INSERT OR IGNORE INTO budgets VALUES (?,?)", (self.budget_scope, self.budget_usd))

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Serialized write transaction (used by the settlement worker)."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @contextmanager
    def read_connection(self):
        """Unlocked read-only connection; WAL readers never wait for writers."""
        with self._connect() as conn:
            yield conn

    def add_finish_listener(self, callback) -> None:
        """``callback(receipt)`` runs after every committed finish(); errors are ignored."""
        self._finish_listeners.append(callback)

    def _rates(self, provider: str, model: str) -> dict | None:
        return self.pricing["models"].get(provider + ":" + model) or self.pricing["models"].get(model)

    def estimate_reservation(self, provider: str, model: str, input_tokens_upper_bound: int,
                             output_tokens_upper_bound: int) -> float:
        rates = self._rates(_provider(provider), model)
        if not rates or "input" not in rates or "output" not in rates:
            raise BudgetExceeded("Cannot reserve an unpriced model")
        if input_tokens_upper_bound < 0 or output_tokens_upper_bound < 0:
            raise ValueError("Token bounds must be nonnegative")
        # A cache write can cost more than ordinary input; reserve the greatest rate.
        worst_input = max(rates[k] for k in ("input", "cache_read", "cache_write_5m", "cache_write_1h") if k in rates)
        return (input_tokens_upper_bound * worst_input + output_tokens_upper_bound * rates["output"]) / 1_000_000

    @staticmethod
    def _budget(conn: sqlite3.Connection, scope: str, limit: float) -> dict:
        """Scoped spend and holds.

        Settled and estimated amounts count toward spend exactly like recorded cost:
        a settled row spends its OpenRouter charge and holds nothing (its reservation
        is released); an estimated row spends recorded + estimate and holds only the
        rest of its reservation, as a partially recorded row always has. Rows
        awaiting settlement hold their reservation minus any recorded cost.
        ``spent_known_usd`` keeps its meaning (recorded cost only); ``spent_usd`` is
        the total that admission uses.
        """
        rows = conn.execute("""SELECT status, finished_at, provider, provider_request_id, cost_usd, reservation_usd,
            usage_complete, pricing_status, settlement_status, settled_cost_usd, estimated_unrecorded_usd
            FROM requests WHERE budget_scope=?""", (scope,)).fetchall()
        recorded = settled_extra = estimated = held = 0.0
        for row in rows:
            cost = row["cost_usd"] or 0
            recorded += cost
            part = row_settlement(row)
            if part["settled"]:
                settled_extra += part["settled_extra"]
                continue
            estimate = part["estimate"] or 0
            estimated += estimate
            if not row["usage_complete"] or row["pricing_status"] != "priced":
                held += max(0, row["reservation_usd"] - cost - estimate)
        spent = recorded + settled_extra + estimated
        return {"scope": scope, "limit_usd": limit, "spent_known_usd": recorded,
                "spent_settled_extra_usd": settled_extra, "spent_estimated_usd": estimated, "spent_usd": spent,
                "held_usd": held, "remaining_usd": max(0, limit - spent - held), "overrun_usd": max(0, spent + held - limit)}

    def begin(self, provider: str, model: str, phase: str, turn_id: str = "",
              estimated_max_usd: float | None = None) -> str:
        provider, model, phase, turn_id = _provider(_label(provider)), _label(model), _label(phase), _label(turn_id)
        rates = self._rates(provider, model)
        reserve = float(estimated_max_usd or 0)
        if not math.isfinite(reserve) or reserve < 0:
            raise ValueError("Reservation must be a finite nonnegative USD amount")
        if self.budget_scope and (reserve <= 0 or not rates or "input" not in rates or "output" not in rates):
            raise BudgetExceeded("Budgeted calls need a positive conservative reservation and a priced model")
        snapshot = {"rates": rates, "metadata": self.pricing["metadata"]}
        request_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.budget_scope:
                budget = self._budget(conn, self.budget_scope, self.budget_usd)
                if reserve > budget["remaining_usd"] + 1e-12:
                    raise BudgetExceeded("Request reservation exceeds remaining test budget")
            conn.execute("""INSERT INTO requests
                (id,provider,model,phase,turn_id,started_at,status,pricing_status,pricing_json,issues_json,budget_scope,reservation_usd)
                VALUES (?,?,?,?,?,?,'started','missing_usage',?,'[]',?,?)""",
                         (request_id, provider, model, phase, turn_id, _now(), json.dumps(snapshot), self.budget_scope, reserve))
        return request_id

    def finish(self, request_id: str, usage: dict | None = None, status: str = "complete", error: Any = None,
               reported_cost_usd: float | None = None, actual_model: str | None = None,
               actual_provider: str | None = None, provider_request_id: str | None = None) -> dict:
        receipt = self._finish(request_id, usage, status, error, reported_cost_usd, actual_model,
                               actual_provider, provider_request_id)
        # After commit and outside the lock: the settlement worker only notes the
        # time and wakes up; a listener can never delay or fail a paid receipt.
        for callback in list(self._finish_listeners):
            try:
                callback(receipt)
            except Exception:
                pass
        return receipt

    def _finish(self, request_id, usage, status, error, reported_cost_usd, actual_model,
                actual_provider, provider_request_id) -> dict:
        if status not in STATUS_VALUES:
            raise ValueError("Unknown request status")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown usage request")
            normalized = normalize_usage(row["provider"], usage, row["model"])
            tokens, issues = normalized["tokens"], list(normalized["issues"])
            cache_reported = int(normalized["cache_reported"]) if normalized["has_usage"] else None
            complete = normalized["complete"] and status in FINAL_STATUSES
            is_router = row["provider"] == "openrouter"
            raw = usage if isinstance(usage, dict) else {}
            raw = raw["usage"] if isinstance(raw.get("usage"), dict) else raw
            reported = reported_cost_usd if reported_cost_usd is not None else raw.get("cost")
            reported_valid = (not isinstance(reported, bool) and isinstance(reported, (int, float))
                              and math.isfinite(reported) and reported >= 0)
            if status not in FINAL_STATUSES:
                issues.append("request_" + status)
            # A cleanup/finally call without usage must not erase a finished receipt.
            if row["finished_at"]:
                if not is_router:
                    if not normalized["has_usage"] or row["usage_complete"]:
                        return self._receipt(row)
                elif (row["usage_complete"] and row["pricing_status"] == "priced") or (not normalized["has_usage"] and not reported_valid):
                    return self._receipt(row)
                elif not normalized["has_usage"]:
                    # A later billing receipt may contain cost only. Retain known token
                    # counts and request outcome while resolving the held reservation.
                    tokens = json.loads(row["tokens_json"]) if row["tokens_json"] else tokens
                    cache_reported = row["cache_reported"]
                    complete = bool(row["usage_complete"])
                    status = row["status"]
                    issues = [issue for issue in json.loads(row["issues_json"])
                              if issue not in {"missing_reported_cost", "invalid_reported_cost"}]
                    normalized["has_usage"] = row["tokens_json"] is not None
            rates = json.loads(row["pricing_json"])["rates"]
            category_rates = {"input_uncached_tokens": "input", "output_tokens": "output",
                              "cache_read_tokens": "cache_read", "cache_write_5m_tokens": "cache_write_5m",
                              "cache_write_1h_tokens": "cache_write_1h"}
            missing_rate = not rates or any(tokens[t] and r not in rates for t, r in category_rates.items()) or tokens["cache_write_unknown_tokens"] > 0
            if not normalized["has_usage"]:
                cost, pricing_status = None, "missing_usage"
            elif missing_rate:
                cost, pricing_status = None, "unpriced"
            else:
                cost = sum(tokens[t] * rates.get(r, 0) for t, r in category_rates.items()) / 1_000_000
                pricing_status = "priced" if complete else "partial"
            estimated_cost = cost if is_router else None
            cost_source = "snapshot_rates" if cost is not None else "unavailable"
            if row["provider"] in PRICE_TABLE_PROVIDERS and reported_valid and normalized["has_usage"]:
                # Model Studio returns no charge: the transport priced these exact counts
                # from the reviewed list prices (tier, peak hours, cached input).
                cost, cost_source = float(reported), "price_table"
                pricing_status = "priced" if complete else "partial"
            if is_router:
                if reported_valid:
                    cost, cost_source = float(reported), "provider_reported"
                    pricing_status = "priced" if complete else "partial"
                else:
                    issues.append("missing_reported_cost" if reported is None else "invalid_reported_cost")
                    # An estimate is not a charge confirmation. Keep the full budget
                    # reservation held, including when the local estimate is zero.
                    cost, cost_source = None, "unavailable"
                    pricing_status = "estimated" if estimated_cost is not None else "unpriced"
                    if row["cost_source"] == "provider_reported":
                        # Do not lose a known partial charge when later usage lacks cost.
                        cost, cost_source, pricing_status = row["cost_usd"], "provider_reported", "partial"
            metadata = {}
            for field, value in (("actual_model", actual_model), ("actual_provider", actual_provider),
                                 ("provider_request_id", provider_request_id)):
                metadata[field] = row[field]
                if value is not None:
                    try:
                        metadata[field] = _label(value) or None
                    except ValueError:
                        # Never reject paid usage accounting or store unsafe response text.
                        issues.append("invalid_" + field)
            # A drained request's content was discarded (the turn was cancelled).
            discarded = 1 if status == "drained" else row["discarded"]
            conn.execute("""UPDATE requests SET finished_at=?,status=?,tokens_json=?,usage_complete=?,
                pricing_status=?,cost_usd=?,issues_json=?,error_code=?,estimated_cost_usd=?,cost_source=?,
                actual_model=?,actual_provider=?,provider_request_id=?,discarded=?,cache_reported=? WHERE id=?""",
                         (_now(), status, json.dumps(tokens), int(complete), pricing_status, cost,
                          json.dumps(issues), _error_code(error) or row["error_code"], estimated_cost, cost_source,
                          metadata["actual_model"], metadata["actual_provider"], metadata["provider_request_id"],
                          discarded, cache_reported, request_id))
            # Settlement bookkeeping in the same transaction: pending (has an
            # OpenRouter id), no_id + estimate, or cleared once confirmed.
            refresh_row(conn, request_id)
            return self._receipt(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict:
        result = {key: row[key] for key in ("id", "provider", "model", "phase", "turn_id", "started_at", "finished_at",
                  "status", "pricing_status", "cost_usd", "error_code", "budget_scope", "reservation_usd",
                  "estimated_cost_usd", "cost_source", "actual_model", "actual_provider", "provider_request_id")}
        result.update(tokens=json.loads(row["tokens_json"]) if row["tokens_json"] else None,
                      usage_complete=bool(row["usage_complete"]), pricing=json.loads(row["pricing_json"]),
                      issues=json.loads(row["issues_json"]))
        # Settlement state is separate from the recorded columns above.
        for key in ("settlement_status", "settled_cost_usd", "settled_cancelled", "settled_at",
                    "estimated_unrecorded_usd", "estimate_basis"):
            result[key] = row[key]
        result["discarded"] = bool(row["discarded"])
        # Whether the provider reported a prompt-cache figure (None: the call has no usage)
        no_usage = not row["tokens_json"] or row["pricing_status"] == "missing_usage"
        result["cache_reported"] = None if no_usage else row_cache_reported(row)
        return result

    # A request still "started" after this long never finished: the thread that began it is gone (an
    # exception between begin() and finish()). Longer than every drain deadline (build_f.json: 120 s).
    STALE_AFTER_S = 900

    def sweep_stale(self, max_age_s: float = STALE_AFTER_S) -> list:
        """Retire requests that began but never finished (status 'started', older than max_age_s).

        They are recorded with the 'unknown' outcome so they stop counting as active — otherwise one dead
        request blocks the memory wipe and the display reset forever (seen 2026-11-01: a reflection call
        from the previous day). Returns the retired ids."""
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(0.0, float(max_age_s)))).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT id FROM requests WHERE status='started' AND started_at < ?",
                                (cutoff,)).fetchall()
        retired = []
        for row in rows:
            try:
                self._finish(row["id"], None, "unknown", "stale: no completion was recorded", None, None, None, None)
                retired.append(row["id"])
            except Exception:
                pass
        return retired

    def reset_display(self) -> dict:
        """Start a new display window without deleting receipts or changing any budget.

        The rowid boundary is captured in the same write transaction as the active
        request check, so concurrent begin() calls fall entirely before or after it.
        """
        self.sweep_stale()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM requests WHERE status='started' LIMIT 1").fetchone():
                raise DisplayResetBusy("Wait for in-flight requests before resetting displayed usage")
            boundary = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM requests").fetchone()[0]
            conn.execute("""UPDATE display_window SET after_rowid=?,reset_at=?,reset_count=reset_count+1,
                         reset_id=? WHERE singleton=1""", (boundary, _now(), uuid.uuid4().hex))
            return dict(conn.execute("SELECT after_rowid,reset_at,reset_count,reset_id FROM display_window WHERE singleton=1").fetchone())

    def _summarize_rows(self, rows: list, recent_limit: int) -> dict:
        def empty() -> dict:
            return {"requests": 0, "priced_known_usd": 0.0, "incomplete_requests": 0, "unpriced_requests": 0,
                    "unconfirmed_cost_requests": 0, "unconfirmed_estimate_usd": 0.0,
                    "provider_reported_usd": 0.0,
                    "tokens": dict.fromkeys(TOKEN_KEYS, 0),
                    # Settlement view (f_settle): never part of priced_known_usd.
                    "settled_extra_usd": 0.0, "settled_calls": 0, "pending_settlement_calls": 0,
                    "no_id_calls": 0, "failed_settlement_calls": 0, "partial_recorded_calls": 0,
                    "estimated_calls": 0, "estimated_unrecorded_usd": 0.0,
                    # Drained speculative calls: recorded usage, so already in priced_known_usd.
                    "drained_calls": 0, "drained_usd": 0.0,
                    # Prompt cache: only calls whose provider reports a cache figure count (row_cache_reported);
                    # chat calls that report none (Ollama) are counted apart, embeddings never.
                    "cache_input_tokens": 0, "cache_read_tokens": 0, "cache_unreported_requests": 0}
        def add(group: dict, row: sqlite3.Row):
            group["requests"] += 1
            group["priced_known_usd"] += row["cost_usd"] or 0
            group["incomplete_requests"] += int(not row["usage_complete"])
            group["unpriced_requests"] += int(row["pricing_status"] in {"unpriced", "missing_usage", "estimated"})
            group["unconfirmed_cost_requests"] += int(row["pricing_status"] != "priced")
            if row["cost_usd"] is None:
                group["unconfirmed_estimate_usd"] += row["estimated_cost_usd"] or 0
            if row["cost_source"] == "provider_reported":
                group["provider_reported_usd"] += row["cost_usd"] or 0
            tokens = json.loads(row["tokens_json"]) if row["tokens_json"] else {}
            for key, value in tokens.items():
                group["tokens"][key] += value
            if tokens and row["pricing_status"] != "missing_usage":
                if row_cache_reported(row):
                    group["cache_input_tokens"] += tokens.get("total_input_tokens", 0)
                    group["cache_read_tokens"] += tokens.get("cache_read_tokens", 0)
                elif row["phase"] not in EMBEDDING_PHASES and not str(row["phase"]).startswith("agent:"):
                    group["cache_unreported_requests"] += 1
            if row["status"] == "drained":
                group["drained_calls"] += 1
                group["drained_usd"] += row["cost_usd"] or 0
            part = row_settlement(row)
            group["settled_calls"] += int(part["settled"])
            group["settled_extra_usd"] += part["settled_extra"]
            group["pending_settlement_calls"] += int(part["pending"])
            group["no_id_calls"] += int(part["no_id"])
            group["failed_settlement_calls"] += int(part["failed"])
            group["partial_recorded_calls"] += int(part["partial"])
            if part["estimate"] is not None:
                group["estimated_calls"] += 1
                group["estimated_unrecorded_usd"] += part["estimate"]
        def finalize(group: dict) -> dict:
            group["total_estimated_usd"] = group["priced_known_usd"] + group["settled_extra_usd"] + group["estimated_unrecorded_usd"]
            # None = no call in this group reported a cache figure ("n/a"), never 0 %
            group["cache_hit_pct"] = (100.0 * group["cache_read_tokens"] / group["cache_input_tokens"]
                                      if group["cache_input_tokens"] else None)
            return group
        total, phases, models = empty(), {}, {}
        # Her own calls vs the helpers' runs (phase "agent:<server>", booked by f_tools from the tokens the agent
        # reports): the Use page shows them apart, and the cache-hit rate is hers alone — agent bookings carry no
        # cache figures and were dragging the headline number down (47% shown vs 76% real, 2026-10-24).
        amika, agents, dialogues = empty(), empty(), set()
        for row in rows:
            add(total, row)
            phase = phases.setdefault(row["phase"], {"phase": row["phase"], **empty()})
            model = models.setdefault((row["provider"], row["model"]), {"provider": row["provider"], "model": row["model"], **empty()})
            add(phase, row)
            add(model, row)
            if str(row["phase"]).startswith("agent:"):
                add(agents, row)
            else:
                add(amika, row)
                if row["phase"] in ("surface", "deep"):
                    dialogues.add(row["turn_id"])        # one exchange she spoke in (user turns and her own openings)
        for group in (total, amika, agents, *phases.values(), *models.values()):
            finalize(group)
        total["total_requests"] = total.pop("requests")
        amika_tokens = amika["tokens"]
        amika_input = amika_tokens.get("total_input_tokens") or (amika_tokens.get("input_uncached_tokens", 0) + amika_tokens.get("cache_read_tokens", 0))
        split = {
            # cache_hit_pct = cache_read_tokens / cache_input_tokens over her calls whose provider reports a cache
            # figure (embeddings and Ollama excluded: their share is unknown, not 0 %); input_tokens is all her input.
            "amika": {"cost_usd": amika["total_estimated_usd"], "requests": amika["requests"], "dialogues": len(dialogues),
                      "avg_per_dialogue_usd": (amika["total_estimated_usd"] / len(dialogues)) if dialogues else None,
                      "cache_read_tokens": amika["cache_read_tokens"], "input_tokens": amika_input,
                      "cache_input_tokens": amika["cache_input_tokens"],
                      "cache_unreported_requests": amika["cache_unreported_requests"],
                      "cache_hit_pct": amika["cache_hit_pct"]},
            "agents": {"cost_usd": agents["total_estimated_usd"], "requests": agents["requests"],
                       "avg_per_request_usd": (agents["total_estimated_usd"] / agents["requests"]) if agents["requests"] else None,
                       "input_tokens": agents["tokens"].get("total_input_tokens") or agents["tokens"].get("input_uncached_tokens", 0),
                       "output_tokens": agents["tokens"].get("output_tokens", 0)},
        }
        return {**total, **split, "active_requests": sum(row["status"] == "started" for row in rows),
                "cost_complete": total["unconfirmed_cost_requests"] == 0,
                "by_phase": list(phases.values()), "by_model": list(models.values()),
                "recent_requests": [self._receipt(row) for row in rows[:max(0, min(int(recent_limit), 200))]]}

    def summary(self, recent_limit: int = 20) -> dict:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT rowid AS ledger_rowid,* FROM requests ORDER BY started_at DESC, id DESC").fetchall()
            window = dict(conn.execute("SELECT after_rowid,reset_at,reset_count,reset_id FROM display_window WHERE singleton=1").fetchone())
            budget = self._budget(conn, self.budget_scope, self.budget_usd) if self.budget_scope else None
            # Key usage: since the current display reset (display view) and since
            # the first snapshot (lifetime view). The key is shared: upper bound.
            key_display = key_usage_view(conn, window, lifetime=False)
            key_lifetime = key_usage_view(conn, window, lifetime=True)
        lifetime = self._summarize_rows(rows, recent_limit)
        display = self._summarize_rows([row for row in rows if row["ledger_rowid"] > window["after_rowid"]], recent_limit)
        lifetime.update(key_lifetime)
        display.update(key_display)
        def worker_status(hook):
            try:
                return hook() if callable(hook) else None
            except Exception:
                return None
        worker = worker_status(self.settlement_worker)
        drain = worker_status(self.drain_worker)
        # Existing top-level totals retain their lifetime meaning. Budget is also
        # lifetime/scoped; neither is affected by the resettable display window.
        return {**lifetime, "display": display, "display_window": window,
                "can_reset_display": lifetime["active_requests"] == 0,
                "pricing_metadata": self.pricing["metadata"], "budget": budget,
                "settlement_worker": worker or {"enabled": False, "running": False},
                "drain_worker": drain or {"enabled": False}}
