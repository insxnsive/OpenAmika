"""Personality editing endpoints for the Setup page.

What "personality" is in this stack (docs/PERSONALITY.md):
  * identity   — build_f.json `user_name` (how she names you) + `persona_name` (who she is);
  * scenes.json — her daily life, mood, relationship stages, communication style; persona
    text may use the {user_name} token, substituted at load;
  * the three prompt templates — 例子.txt (deep), 表层.txt (surface), 思考.txt (reflection).
    Their FORMAT sections are contract-bound (po_contract.check_template guards the
    placeholders and vocab lines); the persona prose around them is freely editable.

Saves are validated (JSON parse for scenes; check_template for templates — problems are
returned as warnings, the file still saves, matching the boot-time warn-only self-check),
written atomically, and hot-reloaded into the running brain.
"""
import contextlib
import io
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import po_contract

BRAIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BRAIN_DIR.parent / "config" / "build_f.json"

# Her clock: build_f.json "timezone" — the zone her prompts, scenes (time of day, weekday) and memory
# timestamps live in. webui.LOCAL_TZ is loaded from it at boot and hot-applied here on save.
DEFAULT_TIMEZONE = "Asia/Tokyo"
COMMON_TIMEZONES = (
    "Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles", "America/Vancouver", "America/Denver",
    "America/Chicago", "America/Mexico_City", "America/New_York", "America/Toronto", "America/Sao_Paulo",
    "UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam",
    "Europe/Istanbul", "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok", "Asia/Jakarta",
    "Asia/Singapore", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Manila", "Asia/Seoul", "Asia/Tokyo",
    "Australia/Perth", "Australia/Sydney", "Australia/Melbourne", "Pacific/Auckland",
)


def _zone(name):
    """ZoneInfo for an IANA name, or None when the name is unknown."""
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _clock(zone_name):
    """What time it is for her right now, for the Setup page: {timezone, local, abbrev, offset}."""
    zone = _zone(zone_name)
    if zone is None:
        return None
    now = datetime.now(zone)
    return {"timezone": zone_name, "local": now.strftime("%a %H:%M"), "abbrev": now.tzname() or "",
            "offset": now.strftime("%z")}

TARGETS = {
    "scenes":   {"file": "scenes.json",  "kind": None},
    "deep":     {"file": "例子.txt",      "kind": "deep"},
    "surface":  {"file": "表层.txt",      "kind": "surface"},
    "thinking": {"file": "思考.txt",      "kind": "thinking"},
}
MAX_BYTES = 262144
# The shipped persona (brain/defaults/): what "Reset everything" restores — the four persona files and the
# identity fields below. Keep it in sync when the shipped templates change.
DEFAULTS_DIR = BRAIN_DIR / "defaults"
IDENTITY_KEYS = ("user_name", "persona_name", "timezone", "city", "onboarded")
IDENTITY_DEFAULTS = {"user_name": "User", "persona_name": "Amika", "timezone": DEFAULT_TIMEZONE,
                     "city": "", "onboarded": False}
MAX_DAY_OPTIONS = 5   # possibilities per time slot (each picked with equal chance for the day)

# ── Simple mode: fill-in-the-blank persona builder ──────────────────────────
# The surface template's persona region is a run of [SECTION] blocks before the
# first `---` (the FORMAT boundary). The same skeleton holds across characters
# (Amika and MekaHime's other characters): [CHARACTER — …] carries the hard rules and is
# NOT editable here; everything from the next section through [PERSONALITY] is
# free persona prose. Days/mood/relationship live in scenes.json.
import re as _re

_SECTION_RE = _re.compile(r"(?m)^\[([^\]\n\r]+)\][ \t]*\r?$")


def _surface_layout(raw: str):
    """(sections, region_end): ordered [{name, body_start, body_end}] before `---`."""
    m = _re.search(r"(?m)^---[ \t]*\r?$", raw)
    region_end = m.start() if m else len(raw)
    heads = [(h.group(1).strip(), h.start(), h.end()) for h in _SECTION_RE.finditer(raw[:region_end])]
    sections = []
    for i, (name, _hs, he) in enumerate(heads):
        # $ in the header match stops BEFORE the \n — include it, or a splice at
        # body_start would wedge text between \r and \n and break the header.
        if raw[he:he + 1] == "\n":
            he += 1
        body_end = heads[i + 1][1] if i + 1 < len(heads) else region_end
        sections.append({"name": name, "body_start": he, "body_end": body_end})
    return sections, region_end


def _eol_of(raw: str) -> str:
    return "\r\n" if "\r\n" in raw else "\n"


def _load_scenes_raw():
    return json.loads(po_contract.read_text_bom(BRAIN_DIR / "scenes.json"))


def _active_stage_index(scenes) -> int:
    stages = (((scenes.get("deep") or {}).get("static") or {}).get("relationship_stages") or [])
    # webui pins the relationship to stage index 2 ("重要的人"); clamp if shorter
    return min(2, max(0, len(stages) - 1)) if stages else -1


def _write_atomic(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    # newline="" — no \n→os.linesep translation; content may already carry \r\n,
    # and text-mode writing on Windows would double it into \r\r\n.
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, path)


KEY_NAME = "CUSTOM_OPENAI_API_KEY"


def _env_without_key(text, name):
    """Drop every binding of the key, keeping unrelated content exactly as it was.

    Offsets come from the untouched text, one span per parsed binding and in source order, because
    searching text that is being shortened can land on an identical line inside an earlier value.
    """
    from dotenv.parser import parse_stream

    spans = []
    cursor = 0
    for binding in parse_stream(io.StringIO(text)):
        if getattr(binding, "error", False):
            raise ValueError("brain/.env has a line that cannot be parsed, so nothing was changed.")
        chunk = binding.original.string
        index = text.find(chunk, cursor)
        if index == -1:  # pragma: no cover - the parser hands back the source it read
            raise ValueError("brain/.env could not be rewritten safely, so nothing was changed.")
        cursor = index + len(chunk)
        if (binding.key or "").strip() == name:
            spans.append((index, cursor))
    kept = text
    for start_at, end_at in reversed(spans):  # back to front, so earlier offsets stay valid
        kept = kept[:start_at] + kept[end_at:]
    return kept.splitlines()


def _commit_custom_config(catalog, payload, key, manager, g, catalog_path, roles_path, env_path, marker_path,
                          write=None, remove=None):
    """Write the catalog, the role rows and the key as one unit, or put every one of them back.

    Held inside the manager's own exclusion and testable on its own: the production route and the
    tests call this same function, so the rollback, the key publication order and the blocked state
    are the code that ships, not a copy of it.
    """
    from f_roles import sync_custom_rows

    write = write or _write_atomic
    remove = remove or (lambda path: path.unlink(missing_ok=True))
    previous_env_value = os.environ.get("CUSTOM_OPENAI_API_KEY")
    state = {"wrote": False, "key_written": False, "catalog": None, "roles": None, "env": None}

    def _commit():
        state["catalog"] = catalog_path.read_text(encoding="utf-8") if catalog_path.is_file() else None
        state["roles"] = roles_path.read_text(encoding="utf-8") if roles_path.is_file() else None
        state["env"] = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
        document = sync_custom_rows(json.loads(po_contract.read_text_bom(roles_path)), catalog)
        write(catalog_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        state["wrote"] = True
        write(roles_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        if key:
            # Every definition goes, not just the first: a duplicate further down (including an
            # "export KEY=" form) wins on the next boot while this process uses the new value, so the
            # endpoint would come back with a credential nobody typed.
            lines = _env_without_key(state["env"] or "", KEY_NAME)
            lines.append(KEY_NAME + "=" + key)
            candidate = "\n".join(lines) + "\n"
            # Prove what a restart will read before committing it.
            from dotenv import dotenv_values
            parsed = dotenv_values(stream=io.StringIO(candidate)).get(KEY_NAME)
            if parsed != key:
                raise ValueError("brain/.env would read this key back as a different value ("
                                 + repr(parsed) + "), so nothing was changed.")
            write(env_path, candidate)
            state["key_written"] = True
            # Nothing is observable until the guard is released, so publishing here cannot be seen
            # paired with the previous catalog.
            os.environ["CUSTOM_OPENAI_API_KEY"] = key
        if manager is not None:
            # The repair is on disk now, so the reload has to see a live provider: leaving the block in
            # place would rebuild the rows as off and the person would have to restart anyway. A failed
            # reload puts the block back through the caller's restore path.
            g.pop("_custom_unavailable", None)
            manager.reload_custom()

    def _restore():
        if state["key_written"]:
            if previous_env_value is None:
                os.environ.pop("CUSTOM_OPENAI_API_KEY", None)
            else:
                os.environ["CUSTOM_OPENAI_API_KEY"] = previous_env_value
        failures = []
        entries = [(catalog_path, state["catalog"]), (roles_path, state["roles"])]
        if state["key_written"]:
            entries.append((env_path, state["env"]))
        for target, content in entries:
            try:
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    write(target, content)
            except OSError as restore_error:
                failures.append(target.name + ": " + str(restore_error))
        return failures

    def _mark(reason):
        write(marker_path, json.dumps({"reason": reason}, ensure_ascii=False, indent=2) + "\n")

    def _clear_marker():
        """Remove the recovery note. False when it cannot go, so nobody claims a recovery they did not get."""
        try:
            remove(marker_path)
            return True
        except OSError:
            return False

    # What the installation looked like before this attempt. A repair that fails must leave that state
    # intact: the files it started from were already untrusted, so a successful restore of them is not
    # a recovery, and dropping the note would let the next start use them again.
    entry_reason = g.get("_custom_unavailable")
    entry_marker = marker_path.is_file() if not entry_reason else True

    def _keep_entry_state(fallback_reason=None):
        reason = entry_reason or fallback_reason
        if reason:
            g["_custom_unavailable"] = reason
        if entry_marker and reason:
            try:
                _mark(reason)
            except OSError:
                pass

    guard = manager.custom_write_guard() if manager is not None else contextlib.nullcontext()
    # Rollback and blocked-state publication stay inside the guard: releasing it first would let a
    # reader build a client from files mid-restore.
    with guard:
        if g.get("_custom_unavailable") and not key:
            raise ValueError("Enter the key for this endpoint. A save that repairs the custom "
                             "endpoint cannot reuse the key that is already stored.")
        # The intent is recorded on disk BEFORE anything moves. If it cannot be written, the save is
        # refused: a crash mid-write would otherwise leave split files with nothing to notice them.
        try:
            _mark("a save was in progress and did not report back")
        except OSError as error:
            raise ValueError("Could not record the save safely (" + str(error) +
                             "), so nothing was changed.") from error
        try:
            _commit()
        except Exception as error:
            if not state["wrote"]:
                _keep_entry_state()
                if not entry_marker and not _clear_marker():
                    g["_custom_unavailable"] = "a recovery note could not be removed"
                if isinstance(error, OSError):
                    raise ValueError("Could not write the configuration: " + str(error)) from error
                raise
            failures = _restore()
            if failures:
                reason = "; ".join(failures)
                g["_custom_unavailable"] = reason
                try:
                    _mark(reason)      # replace the intent note with what actually went wrong
                except OSError:
                    pass               # the intent note written above already blocks the next start
                entry_reason = entry_reason or reason
                raise ValueError(
                    "The save failed and part of it could not be undone (" + reason +
                    "). Fix the write problem, then save the whole configuration again; she keeps the "
                    "custom endpoint off until that save succeeds.") from error
            if entry_marker:
                # Restoring the pre-repair files is not a recovery: they were already in doubt.
                _keep_entry_state()
            elif not _clear_marker():
                g["_custom_unavailable"] = "a recovery note could not be removed"
                raise ValueError("The configuration was restored, but the recovery note could not be "
                                 "removed, so she stays off the custom endpoint until it can.")
            if isinstance(error, OSError):
                raise ValueError("Could not write the configuration: " + str(error)) from error
            raise
        # A coherent commit is what earns the removal; a note that will not go is not a recovery.
        if not _clear_marker():
            g["_custom_unavailable"] = "a recovery note could not be removed"
            raise ValueError("Saved, but the recovery note could not be removed, so she stays off the "
                             "custom endpoint until the file next to config/custom_models.json is gone.")
        g.pop("_custom_unavailable", None)


def install_persona(g):
    app, request, jsonify = g["app"], g["request"], g["jsonify"]

    def action_request(action):
        if request.headers.get("Origin") or request.headers.get("X-Build-F-Action") != action:
            raise PermissionError("Use the OpenAmika page for this action.")
        value = request.get_json(silent=True) if request.is_json else None
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required.")
        return value

    def _reload(target):
        try:
            if target == "scenes":
                g["_scenes_cache"] = None
            elif target == "deep":
                g["reload_template"]()
            elif target == "surface":
                g["reload_surface_template"]()
            elif target == "thinking":
                g["reload_thinking_template"]()
            if target != "scenes" and "run_template_check" in g:
                g["run_template_check"]()
        except Exception as error:
            return f"saved, but hot reload reported: {error}"
        return None

    @app.route("/api/build_f/persona", methods=["GET"])
    def persona_get():
        files = {}
        for key, spec in TARGETS.items():
            path = BRAIN_DIR / spec["file"]
            try:
                files[key] = po_contract.read_text_bom(path)
            except Exception:
                files[key] = ""
        try:
            cfg = json.loads(po_contract.read_text_bom(CONFIG_PATH))
        except Exception:
            cfg = {}
        timezone = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
        return jsonify({"ok": True,
                        "user_name": cfg.get("user_name", "User"),
                        "persona_name": cfg.get("persona_name", "Amika"),
                        "timezone": timezone,
                        "city": str(cfg.get("city") or ""),
                        # the first-run wizard on the Use page plays until this is true (Reset clears it)
                        "onboarded": bool(cfg.get("onboarded", False)),
                        "clock": _clock(timezone),
                        "timezones": sorted(set(COMMON_TIMEZONES) | {timezone}),
                        "files": files,
                        "template_check": g.get("TEMPLATE_CHECK") or {}})

    @app.route("/api/build_f/persona/save", methods=["POST"])
    def persona_save():
        try:
            value = action_request("persona-save")
            target = value.get("target")
            content = value.get("content")
            if target not in TARGETS or not isinstance(content, str):
                raise ValueError("Send {target: scenes|deep|surface|thinking, content: <text>}.")
            if len(content.encode("utf-8")) > MAX_BYTES:
                raise ValueError("Content is too large.")
            if not content.strip():
                raise ValueError("Content is empty.")
            problems = []
            if target == "scenes":
                try:
                    doc = json.loads(content)
                    if not isinstance(doc, dict):
                        raise ValueError("scenes.json must be a JSON object")
                except ValueError as error:
                    raise ValueError(f"scenes.json is not valid JSON: {error}") from None
            else:
                kind = TARGETS[target]["kind"]
                try:
                    problems = po_contract.check_template(kind, content) or []
                except Exception as error:
                    problems = [f"template check failed to run: {error}"]
            _write_atomic(BRAIN_DIR / TARGETS[target]["file"], content)
            note = _reload(target)
            return jsonify({"ok": True, "target": target, "problems": problems,
                            "note": note, "template_check": g.get("TEMPLATE_CHECK") or {}})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    @app.route("/api/build_f/persona/simple", methods=["GET"])
    def persona_simple_get():
        raw = po_contract.read_text_bom(BRAIN_DIR / "表层.txt")
        sections, _ = _surface_layout(raw)
        story = []
        for s in sections:
            if s["name"].upper().startswith("CHARACTER"):
                continue  # hard rules live here — professional mode only
            body = raw[s["body_start"]:s["body_end"]].replace("\r\n", "\n").strip()
            story.append({"key": s["name"], "text": body})
        try:
            scenes = _load_scenes_raw()
        except Exception:
            scenes = {}
        deep = scenes.get("deep") or {}
        static = deep.get("static") or {}
        days = {k: v for k, v in (deep.get("daily_context") or {}).items() if not k.startswith("_")}
        idx = _active_stage_index(scenes)
        stages = static.get("relationship_stages") or []
        try:
            cfg = json.loads(po_contract.read_text_bom(CONFIG_PATH))
        except Exception:
            cfg = {}
        return jsonify({
            "ok": True,
            "user_name": cfg.get("user_name", "User"),
            "persona_name": cfg.get("persona_name", "Amika"),
            "story": story,
            "days": days,
            "about": {
                "recent_mood": static.get("recent_mood", ""),
                "current_action": static.get("current_action", ""),
                "communication_style": static.get("communication_style", ""),
                "surface_context": (scenes.get("surface") or {}).get("context", ""),
                "relationship": stages[idx].get("content", "") if idx >= 0 else "",
                "relationship_name": stages[idx].get("name", "") if idx >= 0 else "",
            },
        })

    @app.route("/api/build_f/persona/simple", methods=["POST"])
    def persona_simple_save():
        try:
            value = action_request("persona-simple")
            if not set(value) <= {"story", "days", "about"} or not value:
                raise ValueError("Send story and/or days and/or about.")

            def _clean(v, what):
                if not isinstance(v, str):
                    raise ValueError(f"{what} must be text.")
                v = v.replace("\r\n", "\n").strip()
                if len(v) > 20000:
                    raise ValueError(f"{what} is too long.")
                return v

            problems, touched = [], []

            story = value.get("story")
            if isinstance(story, dict) and story:
                path = BRAIN_DIR / "表层.txt"
                raw = po_contract.read_text_bom(path)
                sections, _ = _surface_layout(raw)
                by_name = {s["name"]: s for s in sections}
                eol = _eol_of(raw)
                edits = []
                for name, text in story.items():
                    if name not in by_name or name.upper().startswith("CHARACTER"):
                        raise ValueError(f"Unknown story section: {name!r}")
                    body = _clean(text, f"section {name}")
                    if not body:
                        raise ValueError(f"Section {name!r} cannot be empty.")
                    # body_start sits just after the header's newline → pad with one
                    # blank line on each side, matching the file's own style.
                    edits.append((by_name[name], eol + body.replace("\n", eol) + eol + eol))
                for section, body in sorted(edits, key=lambda e: -e[0]["body_start"]):
                    raw = raw[:section["body_start"]] + body + raw[section["body_end"]:]
                try:
                    problems = po_contract.check_template("surface", raw) or []
                except Exception as error:
                    problems = [f"template check failed to run: {error}"]
                _write_atomic(path, raw)
                _reload("surface")
                touched.append("表层.txt")

            days = value.get("days")
            about = value.get("about")
            if (isinstance(days, dict) and days) or (isinstance(about, dict) and about):
                scenes = _load_scenes_raw()
                deep = scenes.setdefault("deep", {})
                daily = deep.setdefault("daily_context", {})
                static = deep.setdefault("static", {})
                if isinstance(days, dict):
                    for key, text in days.items():
                        if key.startswith("_") or key not in daily:
                            raise ValueError(f"Unknown day period: {key!r}")
                        if isinstance(text, list):
                            # the slot's possibilities ([1]…[5] on the Setup page); the brain picks one per day
                            if not 1 <= len(text) <= MAX_DAY_OPTIONS:
                                raise ValueError(f"Day {key!r} takes 1 to {MAX_DAY_OPTIONS} possibilities.")
                            options = [_clean(t, f"day {key}") for t in text]
                            if not all(options):
                                raise ValueError(f"Day {key!r} has an empty possibility.")
                            daily[key] = options
                        else:
                            daily[key] = _clean(text, f"day {key}")
                if isinstance(about, dict):
                    for key in ("recent_mood", "current_action", "communication_style"):
                        if key in about:
                            static[key] = _clean(about[key], key)
                    if "surface_context" in about:
                        scenes.setdefault("surface", {})["context"] = _clean(about["surface_context"], "surface_context")
                    if "relationship" in about:
                        idx = _active_stage_index(scenes)
                        if idx >= 0:
                            static["relationship_stages"][idx]["content"] = _clean(about["relationship"], "relationship")
                _write_atomic(BRAIN_DIR / "scenes.json",
                              json.dumps(scenes, ensure_ascii=False, indent=2) + "\n")
                _reload("scenes")
                touched.append("scenes.json")

            return jsonify({"ok": True, "touched": touched, "problems": problems})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    @app.route("/api/build_f/openrouter_key", methods=["GET"])
    def openrouter_key_get():
        env_path = BRAIN_DIR / ".env"
        key = None
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        present = bool(key) and not key.endswith("your-key-here")
        hint = (key[:9] + "…" + key[-4:]) if present and len(key) > 16 else None
        return jsonify({"ok": True, "present": present, "hint": hint})

    @app.route("/api/build_f/openrouter_key", methods=["POST"])
    def openrouter_key_set():
        try:
            value = action_request("openrouter-key")
            key = str(value.get("key") or "").strip()
            if not _re.fullmatch(r"sk-or-[A-Za-z0-9_\-]{20,200}", key):
                raise ValueError("That doesn't look like an OpenRouter key (expected sk-or-…).")
            env_path = BRAIN_DIR / ".env"
            lines = []
            if env_path.is_file():
                lines = env_path.read_text(encoding="utf-8-sig").splitlines()
            replaced = False
            for i, line in enumerate(lines):
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    lines[i] = f"OPENROUTER_API_KEY={key}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"OPENROUTER_API_KEY={key}")
            _write_atomic(env_path, "\n".join(lines) + "\n")
            os.environ["OPENROUTER_API_KEY"] = key   # the transport reads it per call
            return jsonify({"ok": True, "present": True, "hint": key[:9] + "…" + key[-4:],
                            "note": "Saved to brain/.env — takes effect on her next reply."})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    def _apply_identity(updates):
        """Write identity fields to build_f.json and hot-apply them in the running brain."""
        cfg = json.loads(po_contract.read_text_bom(CONFIG_PATH))
        cfg.update(updates)
        _write_atomic(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        if "timezone" in updates:
            g["LOCAL_TZ"] = _zone(updates["timezone"])   # every "now" in the brain reads it at call time
        if "user_name" in updates:
            g["USER_NAME"] = updates["user_name"]
        if "city" in updates:
            g["CITY"] = updates["city"]
        if "user_name" in updates or "city" in updates:
            g["_scenes_cache"] = None  # {user_name} / {city} in scenes re-substitute on next load
        if "persona_name" in updates:
            po_contract.PERSONA_NAME = updates["persona_name"]
            for mod_name in ("f_extract", "f_memory", "f_method"):
                try:
                    __import__(mod_name).PERSONA_NAME = updates["persona_name"]
                except Exception:
                    pass
            g["F_PERSONA_NAME"] = updates["persona_name"]
        return cfg

    def _identity_payload(cfg, **extra):
        timezone = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
        return jsonify({"ok": True, "user_name": cfg.get("user_name"),
                        "persona_name": cfg.get("persona_name"),
                        "timezone": timezone, "clock": _clock(timezone),
                        "city": str(cfg.get("city") or ""),
                        "onboarded": bool(cfg.get("onboarded", False)), **extra})

    @app.route("/api/build_f/persona/identity", methods=["POST"])
    def persona_identity():
        try:
            value = action_request("persona-identity")
            if not set(value) <= set(IDENTITY_KEYS) or not value:
                raise ValueError("Send user_name, persona_name, timezone, city and/or onboarded.")
            updates = {}
            for key in ("user_name", "persona_name"):
                if key in value:
                    name = value[key]
                    if not isinstance(name, str) or not name.strip() or len(name) > 40:
                        raise ValueError(f"{key} must be a short non-empty string.")
                    updates[key] = name.strip()
            if "timezone" in value:
                name = value["timezone"]
                if not isinstance(name, str) or not name.strip() or len(name) > 64 or _zone(name.strip()) is None:
                    raise ValueError("timezone must be an IANA zone name such as America/Los_Angeles or Asia/Tokyo.")
                updates["timezone"] = name.strip()
            if "city" in value:
                city = value["city"]
                if not isinstance(city, str) or len(city) > 80:
                    raise ValueError("city must be a short string (it may be empty).")
                updates["city"] = " ".join(city.split())
            if "onboarded" in value:
                if not isinstance(value["onboarded"], bool):
                    raise ValueError("onboarded must be true or false.")
                updates["onboarded"] = value["onboarded"]
            cfg = _apply_identity(updates)
            return _identity_payload(cfg, note="Applied to new turns. Memory rendering and the history window "
                                               "pick the new names up fully after a restart.")
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    CUSTOM_CATALOG = BRAIN_DIR.parent / "config" / "custom_models.json"
    # Written when a save could not be fully undone, and honoured on the next start: a restart alone
    # does not repair split files, so the provider stays off until a save rewrites the set cleanly.
    RECOVERY_MARKER = BRAIN_DIR.parent / "config" / "custom_models.recovery.json"
    # One save at a time: the probe file, the two documents and the key are a single unit.
    CUSTOM_SAVE_LOCK = threading.Lock()
    ROLE_DOCUMENT = BRAIN_DIR.parent / "config" / "llm_roles.json"

    def _custom_catalog():
        from f_openrouter import load_custom_catalog
        if not CUSTOM_CATALOG.is_file():
            return None
        return load_custom_catalog(CUSTOM_CATALOG)

    def _validate_custom_key(raw):
        """A key that survives the trip through brain/.env and into a header unchanged."""
        key = str(raw or "").strip()
        if not key:
            return ""
        if any(character in key for character in ("\x00", "\n", "\r")):
            raise ValueError("The key contains a character that cannot be saved.")
        if "${" in key or re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", key):
            # load_dotenv expands these at the next start, so the endpoint would receive something
            # other than the text the person pasted. ${1:-fallback} expands just as well as ${NAME}.
            raise ValueError("That looks like a variable reference. Paste the key itself, not a name in braces.")
        if any(character in key for character in ("\"", "'", "`")):
            # dotenv strips a surrounding quote on load, so a quoted key becomes a different string.
            raise ValueError("The key has a quote in it. Paste the key itself.")
        if any(character.isspace() for character in key) or "#" in key:
            raise ValueError("The key has a space or a # in it. Paste the key itself.")
        return key

    def _stored_custom_pair():
        """The endpoint and the key that are live together, or None when there is no usable pair.

        Read through the manager so the two values cannot come from different saves.
        """
        manager = g.get("F_ROLES")
        if manager is None:
            return None
        try:
            pair = manager.custom_runtime()
        except (RuntimeError, ValueError):
            return None
        if not isinstance(pair, dict) or not pair.get("base"):
            return None
        return pair

    def _custom_key_hint():
        env_path = BRAIN_DIR / ".env"
        if not env_path.is_file():
            return None
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("CUSTOM_OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                return (key[:9] + "…" + key[-4:]) if len(key) > 16 else "saved"
        return None

    @app.route("/api/build_f/custom", methods=["GET"])
    def custom_provider_get():
        try:
            catalog = _custom_catalog()
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        models = []
        if catalog:
            for model_id, row in catalog["models"].items():
                models.append({"model_id": model_id, "api_model": row["api_model"], "context": row.get("context"),
                               "prices": row["prices"], "json_mode": row.get("json_mode")})
        manager = g.get("F_ROLES")
        preset_ready = bool(catalog) and bool(manager) and bool(manager.custom_preset_ready())
        return jsonify({"ok": True, "configured": bool(catalog), "blocked": g.get("_custom_unavailable"),
                        "base_url": catalog["base"] if catalog else "", "models": models,
                        "key_hint": _custom_key_hint(),
                        "key_present": bool(os.environ.get("CUSTOM_OPENAI_API_KEY")),
                        "preset_ready": preset_ready})

    @app.route("/api/build_f/custom", methods=["POST"])
    def custom_provider_set():
        from f_openrouter import fetch_custom_models, load_custom_catalog
        from f_roles import sync_custom_rows
        # Two saves at once would share a probe file and interleave three writes.
        if not CUSTOM_SAVE_LOCK.acquire(blocking=False):
            return jsonify({"ok": False, "error": "Another save is already running."}), 409
        try:
            value = action_request("custom-provider")
            if g.get("_ws_active") or g.get("_thinking_inflight"):
                raise ValueError("Wait for the current reply to finish, then save again.")
            payload = {"version": 1, "provider": "custom",
                       "endpoint": {"base_url": str(value.get("base_url") or "")},
                       "models": value.get("models")}
            # Validate through the transport before anything reaches disk, in a file nobody else uses.
            probe = Path(BRAIN_DIR.parent) / "config" / (".custom_models.probe-" + uuid.uuid4().hex + ".json")
            _write_atomic(probe, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            try:
                catalog = load_custom_catalog(probe)
            finally:
                probe.unlink(missing_ok=True)
            # os.environ refuses NUL, CR and LF, and load_dotenv would expand ${NAME}: both have to be
            # settled before the first write, not after the files are on disk.
            key = _validate_custom_key(value.get("key"))
            pair = _stored_custom_pair()
            same_endpoint = bool(pair) and pair["base"] == catalog["base"]
            if not key and not same_endpoint:
                # Setup clears the key field after a save, so a model-only save for a NEW url arrives
                # with no key. Falling back to the stored one would hand the previous provider's
                # credential to the host that was just typed in.
                raise ValueError("Enter the key for this endpoint. A saved key belongs to the endpoint "
                                 "it was saved with, so a new URL needs its own key.")
            if g.get("_custom_unavailable") and not key:
                # A repair has to say which key belongs to the endpoint it repairs: the one already
                # stored may be the key of the endpoint that failed.
                raise ValueError("Enter the key for this endpoint. A save that repairs the custom "
                                 "endpoint cannot reuse the key that is already stored.")
            warning = ""
            verified = False
            probe_key = key or (pair["key"] if same_endpoint else "")
            if probe_key:
                try:
                    verified = bool(fetch_custom_models(g["http_requests"], probe_key, catalog["base"]))
                except Exception as error:  # noqa: BLE001 - every transport answer is reported, none raised
                    # A transport exception can quote the whole Authorization header back, so the
                    # reason stays generic and only the exception type is named.
                    warning = (" It did not answer yet (" + type(error).__name__ +
                               "), so check the URL and the key with Test connection.")
            # A save that leaves the key field empty still has a live key for this endpoint, and the file
            # on disk may hold duplicates or an old multiline value from a manual edit: pass the key that
            # is actually in use so every save leaves one canonical definition behind.
            effective_key = probe_key
            _commit_custom_config(catalog=catalog, payload=payload, key=effective_key, manager=g.get("F_ROLES"), g=g,
                                  catalog_path=CUSTOM_CATALOG, roles_path=ROLE_DOCUMENT, env_path=BRAIN_DIR / ".env",
                                  marker_path=RECOVERY_MARKER)
            manager = g.get("F_ROLES")
            preset_ready = bool(manager) and bool(manager.custom_preset_ready())
            note = "Saved. New models are in the role lists now."
            if key:
                note = "Saved. The key takes effect on her next reply."
            if not preset_ready:
                # Without one JSON-schema model the memory writer has nowhere to go, so all four roles
                # cannot switch to this endpoint. Saying so here beats a save that looks finished.
                note += (" One of these models needs the JSON schema box ticked, or she cannot run all "
                         "four roles on this endpoint.")
            note += warning
            return jsonify({"ok": True, "configured": True, "base_url": catalog["base"],
                            "models": len(catalog["models"]), "note": note,
                            "verified": verified, "preset_ready": preset_ready})
        except RuntimeError as error:
            return jsonify({"ok": False, "error": str(error)}), 409
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        finally:
            CUSTOM_SAVE_LOCK.release()

    @app.route("/api/build_f/custom/test", methods=["POST"])
    def custom_provider_test():
        try:
            from f_openrouter import (OpenRouterAuthError, OpenRouterError, OpenRouterHTTPError,
                                      custom_base_url, fetch_custom_models)
            value = action_request("custom-provider-test")
            base = custom_base_url(value.get("base_url"))
            key = _validate_custom_key(value.get("key"))
            pair = _stored_custom_pair()
            if not key:
                if not pair or pair["base"] != base:
                    raise ValueError("Enter the key for this endpoint. A saved key only works for the "
                                     "endpoint it was saved with.")
                key = pair["key"]
            try:
                models = fetch_custom_models(g["http_requests"], key, base)
            except ValueError:
                # requests.InvalidHeader is a ValueError whose message carries the header value.
                return jsonify({"ok": False, "error": "The endpoint did not accept that key."}), 400
            except OpenRouterAuthError:
                # A key the endpoint refused is a confirmed answer, not an unknown: say so, and say
                # nothing that could carry the header back.
                return jsonify({"ok": False, "error": "The endpoint refused that key. Check it and try again."}), 400
            except OpenRouterHTTPError as error:
                return jsonify({"ok": False, "error": "The endpoint answered HTTP " + str(error.status)
                                + " when asked for its model list, so it is not serving that URL."}), 502
            except OpenRouterError:
                return jsonify({"ok": False, "error": "The endpoint answered, but not with an OpenAI-style "
                                "model list. Check the base URL ends at /v1."}), 502
            return jsonify({"ok": True, "base_url": base, "models": models})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except Exception as error:
            return jsonify({"ok": False, "error": "The endpoint did not answer: " + type(error).__name__}), 502

    @app.route("/api/build_f/reset", methods=["POST"])
    def persona_reset_all():
        """Reset everything: her memory (the confirmed wipe, backup kept), the four persona files back to the
        shipped defaults in brain/defaults/, and the identity fields back to IDENTITY_DEFAULTS — so the
        first-run wizard plays again. Refuses while she is busy, exactly like the wipe."""
        try:
            value = action_request("reset-all")
            if value.get("confirmation") != "RESET":
                raise ValueError("Type RESET to confirm.")
            reset_memory = g.get("F_RESET_MEMORY")
            if not callable(reset_memory):
                raise ValueError("Memory reset is not available in this brain.")
            result, code = reset_memory()
            if code != 200:
                return jsonify(result), code
            restored = []
            for key, spec in TARGETS.items():
                source = DEFAULTS_DIR / spec["file"]
                if source.is_file():
                    _write_atomic(BRAIN_DIR / spec["file"], po_contract.read_text_bom(source))
                    _reload(key)
                    restored.append(spec["file"])
            cfg = _apply_identity(dict(IDENTITY_DEFAULTS))
            response = _identity_payload(cfg, backup_id=result.get("backup_id"), restored=restored,
                                         memory=result.get("memory"), usage=result.get("usage"))
            return response
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    print("[persona] personality endpoints installed")
