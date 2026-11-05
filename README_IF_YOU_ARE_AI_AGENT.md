# OpenAmika — README for AI agents

You are an AI coding agent (Claude Code, Codex, Cursor, Gemini CLI or similar) asked to set up, run, debug or change
OpenAmika. This file is the operator's manual: who she is, every process and port, every model and where it comes
from, the exact setup sequence with its expected output, and how to read every failure. Humans read
[README.md](README.md). Everything below was checked against the code in this repository; if this file and the code
ever disagree, the code wins, and you should fix this file in the same change.

## 0. Hard rules

1. **Her assets are not yours to share.** Amika's voice (GPT-SoVITS weights + reference clips), her Live2D model, her
   art and the paper doll that stands in for her helpers (`live2d/Host`) come from the Hugging Face repository
   `MekaHime/MekaHime_Amika` and belong to MekaHime Inc. under a
   personal-use license: personal use only (plus its creator rules: social media with visible credit, fan merch up
   to US$1,000 as an individual), 18+, no redistribution or re-hosting, no training,
   fine-tuning or distilling of other models on them, visible credit to MekaHime when the person posts videos or
   streams with her. Never commit them, upload them, bake them into an image or a release, mirror them, or use them
   as training data. Installing them on the person's own machine with `scripts/setup_tts.py` is the intended use.
2. **The license is the person's decision.** `scripts/setup_tts.py` shows the terms and asks the person to type
   `yes`. Run it in the person's own terminal, or pass `--accept-voice-license` only after the person has read the
   terms (link below) and told you they accept. Never accept it on their behalf.
3. **IoT needs the person's acceptance, never yours.** Never accept the IoT safety statement for the person, never
   write an `iot_disclaimer` entry into `config/tools.json`, never set `services.mqtt` / `services.zigbee2mqtt`
   `enabled` or enable the `iot` MCP server on your own, and never connect heaters, locks, valves, alarms, medical,
   child- or pet-safety equipment or anything that can cause harm. The person accepts the statement in the dialog on
   the Tools page (§8).
4. **Secrets stay where they are.** `brain/.env` (OpenRouter and other API keys), `config/messengers.json` (bot
   tokens), `config/tools.json` (agent keys), `HF_TOKEN`, and the helpers' own files (Hermes's home
   `%LOCALAPPDATA%\hermes`: `.env`, `auth.json`, memories, sessions; nanoMuse's `Nanomuse\data`: `server_token`,
   `vault.*`, its databases and threads) are the person's. Never print them, never commit them,
   never put a token on a command line that ends up in a log (prefer environment variables). `.gitignore` already
   excludes every such file; never `git add -f` one.
5. **Never commit machine-local state**: `settings.json`, `envs/`, `logs/`, `memory_f/`, `brain/memories/`,
   `config/ollama.json`, `config/seed.json`, `config/voice_map.json`, `avatar/models/*` (except its README),
   `speech/models/*` (except `DOWNLOAD.md`), `vendor/tts-engine/*` (except `main.py`, `requirements-tts.txt`,
   `README.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`). After onboarding, `config/build_f.json` and
   `config/word_replace.json` hold the person's name and city: do not commit those changes to a public fork.
6. **Do not disturb a running stack.** Her default ports are 53000, 5202, 8011, 8002, 8014 and 5215, her helpers'
   8642 (Hermes Agent) and 8787 (nanoMuse) (§2). To inspect,
   use `launch.py --check` (starts nothing; while she runs, her own ports read `OK … held by this OpenAmika
   (running)`) and the health URLs; to experiment, use other ports. `launch.py` stops an earlier instance *of the
   same folder* when it starts ("grace"), so starting her twice restarts her.
7. **Keep the output contract.** `brain/po_contract.py` and `speech/po_contract.py` are identical
   (`tests/test_shipped_copies.py` compares them); the 69 emotion keys, the `我说：` / `我心想：` tokens and the SFX
   names are wired through both parsers, the voice adapter, the Live2D banks and the tests. The persona files in
   `brain/` and their shipped copies in `brain/defaults/` are identical in git (the same test, on git's index: the
   Setup page edits the working-tree copies on purpose).
8. **Keep CI green.** `python -B -m pytest tests -k "not live" -p no:cacheprovider -q` must pass. pytest's `-k`
   matches substrings of test, class and file names, so a test whose name merely contains `live` (`delivery`,
   `lives_in`) would silently never run in CI: keep that word out of test names (`tests/test_ci_filter.py` enforces
   it). Only a test that needs a running stack may carry it, named `test_live_*`; there are none today.

## 1. Who Amika is

- **Amika** (桜井美香, Sakurai Mika), MekaHime's AI companion: an 18-year-old who grew up in Kyoto as the heir of a
  shrine family and, tired of a life where every step was arranged, moved to **the person's city** to live with
  **them** as roommates (rent-free; her side of the deal is being the best company she can). The persona is written
  for a young man a little older than her (the Chinese prompts say 他). When he eats she teases him with one fixed
  English line, "You Holy Big Fat Ass", in every language: a running joke of her fans, kept on purpose.
- **Languages**: she answers in the language the person speaks (English, Japanese or Chinese); "say it in Japanese"
  switches for one turn. Her prompts are Chinese on purpose and her inner thoughts (`我心想：`) stay Chinese.
- **Three tiers, three LLM roles** (plus a fourth for memory), each with its own template, hot-reloaded on save:

  | role | template | what it does |
  |---|---|---|
  | `surface` | `brain/表层.txt` | the instant reflex (~0.5 s): exactly four lines `status:` / `tone:` / `mode:` / `reply:` |
  | `deep` | `brain/例子.txt` | her real reply: `[register_emotion]"spoken sentence"` runs, then `我心想：` inner thoughts; `【想想】` asks the reflection tier |
  | `reflection` | `brain/思考.txt` | a background coach between turns: `<提示>` hint, `<计划>` plan, `<开口>` makes her speak first |
  | `memory_write` | (in code) | extracts quoted facts from each protected turn into her evidence memory |

- **Her day**: `brain/scenes.json` → `deep.daily_context` holds, per time slot of a weekday and a weekend, five
  possibilities; the brain picks one per day and slot, seeded by the date. `deep.current_scene` is where she is when
  the person talks to her on the stage; `deep.text_scene` is where she is when a message arrives from Discord or
  Telegram. `surface.context` is the surface tier's one-page summary.
- **Identity tokens**: `{user_name}` and `{city}` anywhere in the templates and scenes are filled from
  `config/build_f.json` (`user_name`, `city`; also `persona_name`, `timezone` = her clock, `onboarded`). The first-run
  wizard on the Use page writes them. The shipped values are neutral (`"User"`, `""`, `false`); a test enforces it.
- **Shipped copies**: `brain/defaults/` holds `scenes.json`, `例子.txt`, `表层.txt`, `思考.txt` exactly as shipped;
  Setup → Danger zone → *Reset everything* restores them. Read [docs/PERSONALITY.md](docs/PERSONALITY.md) before
  editing her (the 你/我 voice rule, which blocks are contract-checked).
- **Emotion keys (the V2 vocabulary, 69 keys = register × emotion)**, defined in `po_contract.py`
  (`REGISTERS`, `EMOTION_SETS`); every spoken sentence carries one, and it drives her voice, her face and her motion:
  - `polite_` (11): neutral smile happy caring sad worried cold angry embarrassed surprise fear
  - `casual_` (19): neutral smile happy very_happy playful laughing relieved shy embarrassed mumbling jealous caring
    surprise angry frustrated sad tired fear worried
  - `serious_` (11): neutral cold disappointed surprise angry very_angry protective worried anxious sad fear
  - `intimate_` (18): neutral happy very_happy smile playful shy very_shy caring very_caring jealous mumbling relieved
    tearful sad very_sad sleepy tired worried
  - `whisper_` (10): neutral happy smile caring relieved shy sad worried tearful sleepy
- **Voice tags (the V1 vocabulary)**: her voice speaks 69 tags written `【happy2】` (the 67 `V1_IDS` in
  `speech/voice_adapter.py` plus `happy4` and `sad4`), each with a recorded reference clip.
  `speech/voice_adapter.py` maps each V2 key to a V1 tag at the TTS seam (on by default; `config/voice_map.json`
  holds edits made on Setup → Voice); the browser and the avatar always get the V2 key, so face and voice agree.
- **Sound effects**: nine closed names (`Laugh`, `Can_t_help_laughing`, `Soft_hmph`, `Sigh`, `Deep_breath`, `Yawn`,
  `Sniff`, `Light_cough`, `Throat_clear`) written `【Sigh】`. No SFX audio ships; the tags are parsed and stripped.
- **Her body**: `avatar/models/Amika/AmikaV4.model3.json` (Cubism) with 142 motions and 25 expressions;
  `avatar/models/Amika/banks.json` maps each of the 69 keys to expressions and motions (Setup → Avatar edits it);
  keys without an entry use motions named after them (`casual_happy.motion3.json`, `casual_happy.2.motion3.json`).

## 2. Architecture: processes, ports, environments

`launch.py` (run by `Start_OpenAmika.bat` with the brain python) starts everything in one console, in this order:
the services in `settings.json` → `services` (her voice server; the smart-home ones only after the IoT statement is
accepted) and the agents in `config/tools.json` autostart entries (when its top-level `enabled` and the entry's
`enabled` are true), in parallel; then the core one by one, each
waiting for its health URL: embedder → brain → vision (when enabled) → speech → messengers. The browser opens when
speech answers. Ctrl+C (or `Stop_OpenAmika.bat`) stops everything this launcher started. Every process binds
127.0.0.1.

| process | script | environment (Python 3.11) | port (setting) | health | talks to |
|---|---|---|---|---|---|
| launcher | `launch.py` | `envs/brain` | – | – | starts and watches the rest; writes `logs/` |
| speech gateway | `speech/api.py` (FastAPI) | `envs/speech` (torch 2.10.0 + torchaudio, CUDA 12.8) | 53000 (`chat_port`) | `GET /health` | the browser (pages, `WS /ws/dialog`), the brain (Socket.IO client, `HANE_URL`), the voice server (Socket.IO client, `tts_url`), vision (`GET /api/latest`; relays camera frames on `WS /ws/vlm_feed`), messengers (`GET /api/status`) |
| brain | `brain/webui.py` (Flask-SocketIO) | `envs/brain` (no torch) | 5202 (`brain_port`) | `GET /api/evo/health` | OpenRouter / Alibaba Model Studio / Ollama (LLM roles), the embedder (`POST /embed`), MCP servers (tools) |
| embedder | `brain/embed_server.py` | `envs/embed` (torch 2.10.0, sentence-transformers) | 8011 (`embedder.port`) | `GET /health` | – (BAAI/bge-m3, cut to 768 dims) |
| vision (her eyes) | `vision/vlm_service.py` | `envs/vision` (torch 2.10.0 + torchvision 0.25.0, transformers) | 8014 (`vision.port`) | `GET /api/health` | speech (`ws://127.0.0.1:53000/ws/vlm_feed`); only when `vision.enabled` |
| messengers | `messengers/service.py` | `envs/speech` | 5215 (`messengers_port`) | `GET /health` | the brain (Socket.IO), Discord / Telegram |
| her voice server | `vendor/tts-engine/main.py` (aiohttp + Socket.IO, GPT-SoVITS v2ProPlus) | `vendor/tts-engine/.venv` (torch 2.9.1, CUDA 12.8) | 8002 (`tts_url`, `services.tts`) | `GET /ping` (`503 loading` for ~20 s, then `ok`) | – |
| Ollama (optional) | – | – | 11434 (`local_model_url`, Setup → Brain) | – | – |
| mosquitto / zigbee2mqtt (optional, IoT) | `IoT/` | – | 1883 / 8080 | – | the `iot` MCP server (`servers/openamika-iot`) |
| agents bridge (one per enabled helper) | `python -m openamika_agents hermes\|nanomuse`, cwd `servers/openamika-agents` | `envs/brain` (spawned by the brain over stdio; nothing to install) | – (MCP over stdio) | its `health` tool | its helper over HTTP |
| Hermes Agent (optional helper) | `%LOCALAPPDATA%\hermes\bin\hermes.exe gateway` (`config/tools.json` `autostart`) | its own (`%LOCALAPPDATA%\hermes`) | 8642 (`HERMES_URL`) | `GET /health` (no auth), `GET /health/detailed` (bearer `API_SERVER_KEY`) | OpenRouter (its own model) |
| nanoMuse (optional helper) | `Nanomuse\venv\Scripts\nanomuse.exe serve --config Nanomuse\data\config.toml` (`autostart`) | `Nanomuse\venv` (Python 3.11) | 8787 (`NANOMUSE_URL`) | `GET /api/health` (no auth), `GET /api/settings` (`llm_ready`, bearer = `<data_dir>/server_token`) | OpenRouter (its own model) |

Environment variables `launch.py` passes to the core (from `settings.json`): `HANE_HOST`, `HANE_PORT`, `HANE_URL`
(the brain), `AMIKA_CHAT_URL`, `YUMEHA_HOST`, `YUMEHA_PORT` (speech), `YUMEHA_MODEL` (`speech/models`),
`YUMEHA_DEVICE` (`speech_device`), `AMIKA_LOCAL_MODEL_URL`, `AMIKA_TTS_URL`, `AMIKA_TTS_CHARACTER`,
`AMIKA_MESSENGERS_PORT` / `AMIKA_MESSENGERS_URL` (`messengers_port`), `AMIKA_EMBEDDING_URL` (with an `embedder`
block), `AMIKA_VLM_URL` (with vision on).

**A voice turn**: mic PCM (browser) → `WS /ws/dialog` → Silero VAD + SenseVoiceSmall STT (speech) → brain
`message` → surface four-liner (spoken at once) → deep stream → `speech/hane_parser.py`
splits `[emotion]"sentence"` runs → one TTS request per sentence (`tts_request_gateway`, text `【happy2】.sentence`,
voice adapter applied) → `tts_audio {audio_b64, emotion, pause_after}` to the browser → `ui/live2d-engine.js` plays the
audio and fires the expression and motion at playback start → `tts_playing` / `tts_finished` acks drive barge-in →
the memory writer archives the turn; reflection may open the next turn. With tools on, the post-speech clerk runs
once the turn's text is final and hands a helper only what she agreed to (§8.1). Diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
and the interactive map at `/flow`. Socket protocol: [brain/PROTOCOL.md](brain/PROTOCOL.md). Voice contract:
[docs/TTS.md](docs/TTS.md).

## 3. Repository map

| path | role |
|---|---|
| `launch.py` | the launcher: grace cleanup of an earlier instance of this folder, pre-flight (`--check`), start order, the one-console dashboard, Ctrl+C stops all; writes `logs/preflight.json`, `logs/instance.json`, `logs/launcher.log` |
| `Start_OpenAmika.bat` / `Stop_OpenAmika.bat` (`Stop_OpenAmika.ps1`) | start (reads `brain_python` from `settings.json`) / stop exactly what this folder started (by script path, `logs/instance.json`, and a launcher that this folder's brain python runs as a relative `launch.py`). The `.bat` ends with `pause`: from a terminal without a console use `powershell -NoProfile -ExecutionPolicy Bypass -File Stop_OpenAmika.ps1` |
| `Tailscale_Serve_OpenAmika.bat` | maps `https://<machine>.<tailnet>.ts.net:8447/` to the speech port with Tailscale Serve (`off` removes it) |
| `scripts/setup_windows.ps1` | Windows entry point: finds Python 3.11 (`py -3.11`, `python3.11`, `python`) and runs `scripts/setup_core.py` |
| `scripts/setup_core.py` | her brain, ears, memory and eyes: `envs/brain`, `envs/speech`, `envs/embed`, `envs/vision`, SenseVoiceSmall, bge-m3, Qwen3-VL-2B (or the models an existing `settings.json` names), then `settings.json`, `brain/.env`; `--check`, `--cpu`, `--vision auto|on|off`, `--reinstall` |
| `scripts/setup_tts.py` | her voice, her Live2D model and the paper doll from Hugging Face (license prompt first) and the voice server in `vendor/tts-engine`; `--verify`, `--assets-only`, `--from-dir`, `--accept-voice-license`, `--skip-paper-doll`, `--cpu`, `--self-test` |
| `scripts/MekaHime_Amika.manifest.json` | every file of `MekaHime/MekaHime_Amika` at the pinned revision with its size and SHA-256 (250 files, `VOICE_REV`), and in its `paper_doll` block the paper doll's 70 files (`live2d/Host/`, at `PAPER_DOLL_REV`) |
| `scripts/download_sensevoice.py` | SenseVoiceSmall into `speech/models/` (6 files, SHA-256 pinned, ModelScope then Hugging Face; `--verify`) |
| `settings.example.json` | template of `settings.json` (interpreters, ports, devices, TTS, embedder, vision, services) |
| `.env.example` | template of `brain/.env` (secrets only; lists every variable the code reads) |
| `brain/webui.py` | the brain: Socket.IO turn protocol, prompt assembly (cache-first deep prefix), the three tiers, `/api/build_f/*` controls |
| `brain/f_bridge.py` | the live integration: memory store, role transports, usage ledger, settlement |
| `brain/f_memory.py`, `f_method.py`, `f_extract.py`, `methods.py` | evidence memory: quoted facts and original speech (no summaries), hybrid BM25 + embedding recall, extraction schema |
| `brain/f_roles.py`, `config/llm_roles.json` | the four LLM roles, presets, validated selections |
| `brain/f_openrouter.py`, `brain/f_ollama.py` | metered OpenRouter / Alibaba transports; the Ollama transport (`config/ollama.json`) |
| `brain/f_usage.py`, `f_settle.py`, `f_controls.py`, `config/pricing.json` | per-call usage ledger, cost settlement, model and meter controls |
| `brain/f_persona.py` | Setup page persona editing and the OpenRouter key endpoint (writes `brain/.env`) |
| `brain/f_reset.py` | wipe her memory / reset everything (idle-only, with backups) |
| `brain/f_tools.py`, `brain/iot_disclaimer.py` | the tools layer (MCP client, capability block, post-speech clerk, agent jobs, agent health and autostart); the IoT safety statement rules |
| `brain/embed_server.py` | local embedder (bge-m3 → 768 dims); `--check`, `--download` |
| `brain/po_contract.py` = `speech/po_contract.py` | the shared output contract: emotion keys, SFX, parsing, template self-check |
| `brain/表层.txt`, `例子.txt`, `思考.txt`, `scenes.json` (+ `brain/defaults/`) | her persona (§1) |
| `speech/api.py` | the gateway: pages, STT, `WS /ws/dialog`, TTS client, avatar registry, vision relay, control proxies |
| `speech/hane_parser.py`, `sentence_splitter.py`, `tts_emotion.py`, `tts_pause.py`, `tts_sfx.py`, `voice_adapter.py`, `stt_carry.py` | streaming parse, sentence split, emotion tags, pauses, SFX tags, V2→V1 voice tags, carry-over of forced STT splits |
| `speech/model.py`, `speech/utils/` | SenseVoice model code (FunAudioLLM, MIT); `speech/models/` holds the weights (not in git) |
| `vendor/tts-engine/main.py` | her voice server (MIT, derived from the OpenSakura voice server); everything else in that folder is built by `setup_tts.py` |
| `vision/vlm_service.py` | her eyes: a small local VLM describing the camera feed; `--check`, `--download` |
| `messengers/service.py`, `core.py` | "Text her" on Discord / Telegram (`config/messengers.json`) |
| `ui/` | the pages: `use.*` (stage, power, mic, telemetry, first-run wizard), `setup.*`, `tools.*`, `about.*`; `common.js` (nav, the health light), `live2d-engine.js` (Live2D playback, lipsync), `vendor/` (PixiJS, pixi-live2d-display, Live2D Cubism Core) |
| `avatar/models/` | Live2D models, one folder each (`Amika/` and the paper doll `Host/` from `setup_tts.py`); `avatar/retarget_motions.py` reuses motions on another model |
| `servers/openamika-agents`, `servers/openamika-iot` | first-party MCP servers: task agents (Hermes Agent, nanoMuse: `adapters/hermes.py`, `adapters/nanomuse.py`) and the Zigbee smart home |
| `IoT/` | Windows setup for the smart home (CP210x driver, mosquitto, a zigbee2mqtt serial patch) |
| `config/` | non-secret settings: `build_f.json` (identity, her clock, memory budgets), `llm_roles.json`, `openrouter_models.json`, `alibaba_models.json`, `pricing.json`, `word_replace.json` (STT fixes, TTS pronunciations), `tools.example.json`, `messengers.example.json` |
| `docs/` | `ARCHITECTURE.md`, `PERSONALITY.md`, `TTS.md`, `AVATAR.md`, `TOOLS.md`, `MESSENGERS.md`, `IOT_DISCLAIMER.md` |
| `tests/` | the offline suite (§10); `test_shipped_copies.py` (rule 7), `test_ci_filter.py` (rule 8) |
| runtime, git-ignored | `envs/` (environments), `logs/` (`<name>.log` per process, `launcher.log`, `preflight.json`, `instance.json`), `memory_f/` (evidence store, usage ledger, backups), `brain/memories/` (transcript) |

## 4. Models: what, from where, where it lands, how it is checked

| model | used by | source | size | lands in | checked by | installed by |
|---|---|---|---|---|---|---|
| **her voice**: `MekaHime_Amika.ckpt` (GPT), `MekaHime_Amika.pth` (SoVITS, v2ProPlus), `character.json`, `references/emotions.json` + 69 WAV clips, `README.md`, `LICENSE` | voice server | Hugging Face `MekaHime/MekaHime_Amika` @ `a3934e5a83dcb0192901f69536c66b837414e822`, every file outside `live2d/` | 75 files, 385 MB | `vendor/tts-engine/Speakers/MekaHime_Amika/` | size + SHA-256 of every file (`scripts/MekaHime_Amika.manifest.json`); clips for all 69 tags and the 67 adapter tags | `setup_tts.py` |
| **her Live2D model**: `AmikaV4.model3.json`, `AmikaV4.moc3`, `AmikaV4.physics3.json`, `AmikaV4.cdi3.json`, `AmikaV4.8192/texture_00.png`, `texture_01.png`, 142 `*.motion3.json`, 25 `*.exp3.json`, `banks.json` | the Use page | same repository and revision, `live2d/Amika/` | 174 files, 88 MB | `avatar/models/Amika/` | size + SHA-256 of every file; every file the `.model3.json` names exists; an edited `banks.json` is kept (an unedited one follows a newer pin) | `setup_tts.py` |
| **the paper doll** (her helpers' stand-in, MekaHime's folded-paper shrine doll): `host.model3.json`, `host.moc3` (Cubism 5), `host.physics3.json`, `host.cdi3.json`, `host.2048/texture_00.png`, 64 `*.motion3.json` (`idle` 15, `talk` 14, `listen` 7, `think` 6, `happy` 9, `worried` 6, `surprise` 7), `banks.json` (`"role": "agent"`, its place on the stage) | the Use page, beside her while a task agent is switched on (§8.1) | same repository, `live2d/Host/`, at its own revision `PAPER_DOLL_REV` (`b3ffaf59`, the upload of 2026-11-05; an empty one downloads nothing and its row says so) | 70 files, 2.3 MB | `avatar/models/Host/` | size + SHA-256 of every file (the manifest's `paper_doll` block); every file the `.model3.json` names exists; `banks.json` says `"role": "agent"`; an edited `banks.json` is kept | `setup_tts.py` |
| SenseVoiceSmall (`model.pt`, `config.yaml`, `configuration.json`, `am.mvn`, `chn_jpn_yue_eng_ko_spectok.bpe.model`, `tokens.json`) | speech recognition | ModelScope `iic/SenseVoiceSmall`, fallback Hugging Face `FunAudioLLM/SenseVoiceSmall` (not `tokens.json`) | 937 MB | `speech/models/` | size + SHA-256 (`download_sensevoice.py`) | `setup_core.py` |
| Silero VAD | voice activity | inside the `silero-vad` pip package | small | `envs/speech` | – | `setup_core.py` |
| BAAI/bge-m3 | her memory's embeddings | Hugging Face `BAAI/bge-m3` | ~2.3 GB | the Hugging Face cache (`HF_HOME`, default `~/.cache/huggingface/hub`) | loaded once by `embed_server.py --download` | `setup_core.py` (else the first start) |
| Qwen3-VL-2B-Instruct | her eyes | Hugging Face `Qwen/Qwen3-VL-2B-Instruct` | ~4.3 GB | the Hugging Face cache | `vlm_service.py --download` loads its processor | `setup_core.py` with vision |
| GPT-SoVITS code | voice server | `RVC-Boss/GPT-SoVITS` @ `b4570b876f5c` (git, or the zip checked by a tree SHA-256) | tens of MB | `vendor/tts-engine/GPT-SoVITS/` | commit; `TTS_infer_pack/TTS.py` SHA-256 | `setup_tts.py` |
| HuBERT, RoBERTa, ERes2NetV2 speaker model | voice server | Hugging Face `lj1995/GPT-SoVITS` @ `8f88575` | 948 MB | `GPT-SoVITS/GPT_SoVITS/pretrained_models/` | size + SHA-256 | `setup_tts.py` |
| fastText `lid.176.bin`, G2PW (Chinese polyphones), nltk data, ffmpeg (Opus only) | voice server | dl.fbaipublicfiles.com; `XXXXRT/GPT-SoVITS-Pretrained` @ `0c47645`; nltk_data @ `550b662`; gyan.dev 8.0.1 | 131 MB; 589 MB zip; 5 MB; 106 MB | `vendor/tts-engine/...` | size + SHA-256 | `setup_tts.py` |
| LLMs (default preset) | the four roles | OpenRouter (Semi-Local, recommended): `deep` = `z-ai/glm-5` (no reasoning), `surface` and `reflection` = `deepseek/deepseek-v4.1-flash`, `memory_write` = `z-ai/glm-5.3`; or, Fully Local, an Ollama model of 27B or more (§5.4) | – (Ollama: 17 GB for a 27B model, 43–47 GB for a 70B one) | the cloud / Ollama | – | the person (key or `ollama pull`) |

`HF_ENDPOINT` (a Hugging Face mirror) is honored by `setup_tts.py`, as by huggingface_hub; every file is still checked
against its pinned SHA-256. `MekaHime/MekaHime_Amika` is public from OpenAmika's public release on; before that,
`setup_tts.py` needs a read token in `HF_TOKEN` (or a saved `hf auth login`), which it sends to huggingface.co only:
never to the CDN it redirects to, never to a mirror (with `HF_ENDPOINT` set it logs `… your Hugging Face token … is
not sent to a mirror` and downloads anonymously), and it never prints or stores it. License text:
https://huggingface.co/MekaHime/MekaHime_Amika/blob/main/LICENSE

Disk, measured after a full install (with her eyes): about 43 GB. The environments take ~15 GB, the voice server
~8 GB, the Hugging Face cache ~13 GB (more than the models' own size: bge-m3 is fetched in both of its weight formats,
and without symlink support Windows can keep a model's file twice), SenseVoiceSmall ~1 GB and pip's download cache
~6 GB (`pip cache purge`, run with any of the environments' pythons, gives that back once the setup is done).
Two CUDA torch builds are downloaded: 2.10.0 for the core environments, 2.9.1 for the voice server.

## 5. Setup, step by step

Prerequisites: Windows 10/11; an NVIDIA GPU (RTX 20-series or newer; 8 GB VRAM for her ears, memory and voice,
16 GB with her eyes) with a recent driver; for her LLM an OpenRouter key (recommended) or, Fully Local, Ollama with a
model of 27B or more and the GPU memory for it (§5.4); Python 3.11 64-bit (python.org, "Add python.exe to PATH", or
`winget install -e --id Python.Python.3.11`); Git; ~45 GB free disk; 15–20 GB of downloads; 30–60 minutes. Clone
into a short folder that OneDrive does not sync (the environments hold tens of thousands of files): keep the path
under ~100 characters, because Windows cannot load some DLLs deep in the environments from a longer folder even with
long paths enabled (`… DLL load failed while importing _box: The filename or extension is too long.`). Run the
commands in a normal PowerShell window, not an elevated one: processes started elevated cannot be seen or stopped
from a normal window later.

### 5.1 Clone

```powershell
cd C:\
git clone https://github.com/zeekk0/OpenAmika.git
cd OpenAmika
```

All commands below run from that folder (`C:\OpenAmika`). PowerShell runs nothing from the current folder by bare
name: a `.bat` there is `.\Start_OpenAmika.bat`, and a `.ps1` runs as `powershell -ExecutionPolicy Bypass -File …`
(Windows' default execution policy refuses `scripts\setup_windows.ps1` typed alone).

### 5.2 Her brain, ears, memory and eyes

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1        # add --cpu without an NVIDIA GPU
```

It prints `[setup] using py -3.11`, then each step (pip's own output streams through; on a console pip shows its
progress bar), and ends with:

```
==============================================================================
OpenAmika setup summary (909 s)
  OK Python              3.11.9 (...\Python311\python.exe)
  OK GPU                 NVIDIA GeForce RTX 5080, 16 GB: CUDA 12.8 builds
  OK brain               envs\brain\Scripts\python.exe (Python 3.11.9, created; brain/requirements.txt installed)
  OK speech recognition  envs\speech\Scripts\python.exe (Python 3.11.9, created; speech/requirements.txt installed; torch 2.10.0+cu128, CUDA OK on NVIDIA GeForce RTX 5080)
  OK speech model        speech/models/ (SenseVoiceSmall, 6 files verified by SHA-256)
  OK memory (embedder)   envs\embed\Scripts\python.exe (Python 3.11.9, created; brain/requirements-embedder.txt installed; torch 2.10.0+cu128, CUDA OK on NVIDIA GeForce RTX 5080)
  OK memory model        BAAI/bge-m3 in the Hugging Face cache (...\hub\models--BAAI--bge-m3, downloaded)
  OK vision (her eyes)   envs\vision\Scripts\python.exe (Python 3.11.9, created; vision/requirements.txt installed; torch 2.10.0+cu128, CUDA OK on NVIDIA GeForce RTX 5080)
  OK vision model        Qwen/Qwen3-VL-2B-Instruct in the Hugging Face cache (...\hub\models--Qwen--Qwen3-VL-2B-Instruct, downloaded)
  OK settings.json       created from settings.example.json (interpreters in envs\, speech on cuda:0, vision on)
  -- brain/.env          created from .env.example; no OpenRouter key yet: the first-run wizard on the Use page takes it (or put it after OPENROUTER_API_KEY= in brain\.env); fully local with Ollama needs none
==============================================================================
Done. Next:
  1. envs\brain\Scripts\python.exe scripts\setup_tts.py   her voice, her Live2D model and the paper doll (type yes to accept her license)
  2. .\Start_OpenAmika.bat                                start her; the page opens at http://localhost:53000
```

Measured on a fresh clone (Windows 11, RTX 5080, fast connection): about 15 minutes, most of it the CUDA torch wheel
(2.9 GB, downloaded once and reused from pip's cache by the other environments), SenseVoiceSmall (from ModelScope)
and the two Hugging Face models (public models: no token needed; the Hub's "unauthenticated requests" notice is
silenced). A second run with everything in place takes about a minute.

- Marks: `OK` done, `--` skipped or informational, `!!` failed (the text says how to fix it). Exit code 0, 1 with any
  `!!`, 130 after Ctrl+C. Dependent steps of a failed one show `-- … skipped: needs X, which did not finish`.
- Her eyes: `--vision auto` (default) installs them when the GPU has at least ~15 GB (`VISION_MIN_MIB`), `--vision on`
  always, `--vision off` never. Without them the row reads `-- vision (her eyes)  skipped: …` and `settings.json` has
  `"vision": {"enabled": false}`.
- Re-running is the repair: existing environments are reused (a broken one is recreated), pip runs only when a
  requirements file changed (stamp `envs/<name>/openamika-requirements.sha256`) or an import fails, torch is held at
  `2.10.0` by `envs/<name>/openamika-constraints.txt`, finished downloads are kept. A package that is installed but no
  longer imports (damaged files that pip calls satisfied) is put in place again with `pip install --force-reinstall
  --no-deps <name>==<its version>` (torch from the PyTorch index). `--reinstall` runs every pip step again.
- `settings.json` is written last, after the environments: a run stopped half-way leaves none behind, and an existing
  one is never changed. Its interpreters are checked; one under `envs\` that is not made yet (its step failed or was
  interrupted) reads `-- settings.json  existing, not changed; it names envs\speech\Scripts\python.exe (see the speech
  recognition row), which is not made yet`, and the environment's own row says why. The embedder and vision models
  it names are the ones fetched. An `envs/<name>` made with `conda create -p` (the earlier README) is used as it is
  (`envs\<name>\python.exe`).
- A folder longer than 100 characters prints `WARNING: <folder> is N characters long. Windows cannot load some
  program files (DLLs) deep in the environments from a folder that long …` before anything is installed.
- Verify without installing anything: `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 --check`
  (ends with `Her brain, ears, memory and eyes are installed.` or `Not ready: …`).

### 5.3 Her voice, her Live2D model and the paper doll

```powershell
envs\brain\Scripts\python.exe scripts\setup_tts.py
```

First it prints the license summary and asks (only on an interactive console):

```
[voice] Amika's voice, her Live2D model and the paper doll that stands in for her helpers (https://huggingface.co/MekaHime/MekaHime_Amika) are free under MekaHime's personal-use license:
          - personal use only, and you must be 18 or older (social media and fan merch up to US$1,000: see its creator rules)
          - no redistribution, re-hosting or resale of her voice, her Live2D model, the paper doll or their files
          - no training, fine-tuning or distilling other models on them
          - credit MekaHime visibly when you post videos or streams with her
          full text: https://huggingface.co/MekaHime/MekaHime_Amika/blob/main/LICENSE

[voice] type yes to accept and download her voice, her Live2D model and the paper doll (anything else skips them for now):
```

The person types `yes` (`y` works; anything else declines, and the rest still installs). It is asked when her
voice, her model or the paper doll is missing; the paper doll counts only when it can be downloaded in this run (its
revision is pinned, see below), so an install that pulls a newer OpenAmika is asked again for it. Then, in this
order: her voice, her Live2D model and the paper doll, file by file with the standard library only (so they never
depend on torch), then the voice
server: `vendor/tts-engine/.venv` (torch 2.9.1 + `requirements-tts.txt`), GPT-SoVITS at its pinned commit, the
pretrained models, language ID, G2PW, nltk data, ffmpeg, an import probe, the Japanese dictionary,
`vendor/tts-engine/install.json` (records the revision and when the license was accepted), and a check that
`settings.json` starts this server. It ends with (paths shortened):

```
==============================================================================
OpenAmika voice setup summary (402 s)
  OK voice MekaHime_Amika  ...\vendor\tts-engine\Speakers\MekaHime_Amika (downloaded 75 file(s) from MekaHime/MekaHime_Amika@a3934e5a; 75 files verified; 69/69 emotion tags with a clip; all 67 tags OpenAmika's voice adapter can send have one)
  OK Live2D model Amika    ...\avatar\models\Amika (downloaded 174 file(s) from MekaHime/MekaHime_Amika@a3934e5a; 174 files verified; AmikaV4.model3.json, 142 motions, 25 expressions)
  OK paper doll Host       ...\avatar\models\Host (downloaded 70 file(s) from MekaHime/MekaHime_Amika@b3ffaf59; 70 files verified; host.model3.json, 64 motions, 0 expressions; stands in for her helpers on the Use page while a task agent is on)
  OK venv                  ...\vendor\tts-engine\.venv\Scripts\python.exe (Python 3.11.9, created)
  OK torch                 torch 2.9.1+cu128 / torchaudio 2.9.1+cu128 (installed)
  OK packages              requirements-tts.txt (installed)
  OK GPT-SoVITS            ...\vendor\tts-engine\GPT-SoVITS @ b4570b876f (downloaded, git, shallow)
  OK pretrained models     HuBERT, RoBERTa, ERes2NetV2 sv (0.9 GB, downloaded) from lj1995/GPT-SoVITS@8f88575
  OK language ID           fastText lid.176.bin (downloaded)
  OK G2PW                  ...\GPT_SoVITS\text\G2PWModel (downloaded from XXXXRT/GPT-SoVITS-Pretrained@0c47645)
  OK nltk data             ...\vendor\tts-engine\nltk_data (averaged_perceptron_tagger[_eng], cmudict)
  OK ffmpeg                ...\vendor\tts-engine\ffmpeg\ffmpeg.exe (ffmpeg version 8.0.1-essentials_build-www.gyan.dev, downloaded)
  OK imports               25 packages import; torch 2.9.1+cu128; CUDA OK on NVIDIA GeForce RTX 5080
  OK Japanese dictionary   ...\GPT_SoVITS\text\ja_userdic\user.dict (21 MB)
  OK launcher              settings.json starts this server (services.tts, port from tts_url) with voice MekaHime_Amika
==============================================================================
Done. Next:  .\Start_OpenAmika.bat   (or double-click it: her voice server starts with the rest; the page opens at http://localhost:53000)
```

- Unattended (an agent's terminal has no console): ask the person to accept the terms first, then
  `envs\brain\Scripts\python.exe scripts\setup_tts.py --accept-voice-license`. Without a console and without that flag,
  the rows read `!! … not downloaded: her license was not accepted (one license covers her voice, her Live2D model
  and the paper doll) …` and nothing of hers is downloaded. (A
  terminal whose input is the null device, as an agent's often is, counts as a console on Windows: the prompt is
  printed, reads end-of-input at once and counts as no.)
- The paper doll has a revision of its own, `PAPER_DOLL_REV` in `scripts/setup_tts.py` (its 70 pins are in the
  manifest's `paper_doll` block; her voice and model stay at `VOICE_REV`). While that constant is empty (before
  `live2d/Host/` is on Hugging Face) nothing of the doll is requested, the run still succeeds, and its row reads
  `-- paper doll Host  not downloaded: this OpenAmika does not pin the revision of MekaHime/MekaHime_Amika that adds
  live2d/Host/ yet (PAPER_DOLL_REV in scripts/setup_tts.py is empty). …`; a doll already in place is still checked
  against the pins (`OK … existing; 70 files verified against the pinned list`), and `--from-dir <copy>` installs
  it from a local copy laid out like the repository. A copy without `live2d/Host/` (any copy at `VOICE_REV`) still
  installs her voice and model: the doll's row is `-- paper doll Host  not copied: <copy> has no live2d/Host/ …` and
  the run exits 0 (a doll already in place is checked, never touched). Never fill that constant in yourself: it is
  the commit of MekaHime's own upload (§11).
- While `MekaHime/MekaHime_Amika` is private: `$env:HF_TOKEN = "hf_..."` in the same PowerShell window first. Without
  it the rows read `!! voice MekaHime_Amika  Hugging Face refused the download (HTTP 401): MekaHime/MekaHime_Amika is
  not public yet. …`. A refusal or a network failure is also printed the moment it happens (`[setup] !! her voice: …`),
  minutes before the summary repeats it.
- huggingface.co unreachable: `$env:HF_ENDPOINT = "https://<a Hugging Face mirror>"` first; every file is still
  checked against the manifest, and no token is sent to the mirror.
- No NVIDIA GPU: `--cpu`. Only her files, no voice server: `--assets-only`. Offline, from a copy laid out like the
  repository: `--from-dir <folder>` (still checked file by file against the manifest). Skip one: `--skip-voice`,
  `--skip-avatar`, `--skip-paper-doll`. Download again: `--update-voice`, `--update-avatar` (her model and the paper
  doll). Elsewhere: `--avatar-dir`, `--paper-doll-dir` (default `avatar/models/Host`). Run the server once and speak
  a line: `--self-test`.
- `avatar/models/Amika/banks.json` (Setup → Avatar edits it) and `avatar/models/Host/banks.json`: once edited it is
  kept (`your banks.json kept`); while it is still the copy the setup installed, a newer pin replaces it (the
  installed SHA-256 is remembered in `vendor/tts-engine/.openamika-verified.json`). The doll's must keep `"role":
  "agent"` (else `!! paper doll Host  … its banks.json does not say "role": "agent" …`: delete it and run again).
- Verify (downloads and installs nothing; fast, because hashes are cached by size and modification time in
  `vendor/tts-engine/.openamika-verified.json`, which it refreshes):
  `envs\brain\Scripts\python.exe scripts\setup_tts.py --verify` → `Verified: her voice, her Live2D model, the paper
  doll and the voice server are complete.` (without the doll while it is not installed) or one `!!` row per broken
  part, naming the files.
- A re-run of a complete install downloads nothing and takes about half a minute; an interrupted download resumes
  from its `.part` file.

### 5.4 Her brain's LLM

Her ears, voice, memory and eyes always run on the PC; the four LLM roles (§1) run one of two ways. Recommend the
first: in almost all cases it is the better setup, and it costs a few dollars a month.

- **Semi-Local (the default, recommended)**: the roles run on OpenRouter with the person's own key and the models
  she was tested with (§4). The person pastes the key in the first-run wizard on the Use page, or after
  `OPENROUTER_API_KEY=` in `brain/.env` (the brain also accepts it on Setup → Brain).
- **Fully Local**: every role on Ollama, no key. Only with a model of **27B parameters or more**; **70B or more**
  gives the full, reliable experience. A model under 27B may be really bad for her: it loses her character, forgets
  what the person told her and breaks her output contract (the surface's four lines, the deep's
  `[register_emotion]"…"` runs, the writer's JSON). It is also slower: all four roles take turns on the one model,
  so after the person interrupts her, or while she saves a memory or reflects, a reply can take 5–50 s (*How fast*,
  below). Do not set the person up on one because it is what fits their GPU: recommend Semi-Local, or Ollama on a
  second machine, instead.

- **Custom endpoint (opt in)**: any OpenAI-compatible URL, such as OpenCode Go, DeepSeek, Groq, Together, a vLLM or LM Studio server. It is off until the person configures it. Setup → Brain → Custom endpoint asks for the base URL (it must end in `/v1`), the key, and one row per model holding the price the person pays in USD per 1M input and output tokens. Nothing is discovered, and prices cannot be left blank because `f_usage` refuses an unpriced model before a call: zero is the right answer for a local server or a subscription plan. `http://` is accepted only for `localhost`, `127.0.0.1` and `::1`, and a URL carrying credentials, a query string or a fragment is refused. The catalog is `config/custom_models.json` (git-ignored; `config/custom_models.example.json` shows the shape), and the key is `CUSTOM_OPENAI_API_KEY` in `brain/.env`. Saving also writes one option row per model into `config/llm_roles.json` so the roles can pick it; the memory writer only appears for a model whose row declares JSON schema support, because that role needs strict structured output. New models load when she restarts, the key on her next reply.

  Worked example, OpenCode Go: base URL `https://opencode.ai/zen/v1`, the key from the OpenCode dashboard, one row with model id `glm-5.3-flash` and context `131072`. On a subscription plan both prices are `0`; per-token billing uses the plan's listed rates. *Test connection* calls `GET {base}/models` and lists what the endpoint serves, so a `401` there points at the key and not the URL.

Check: `launch.py --check` shows `OK API keys …` or `-- API keys  no OpenRouter key yet …`.

**What Semi-Local costs, and how that was worked out.** The default preset (deep `z-ai/glm-5` without reasoning,
surface and reflection `deepseek/deepseek-v4.1-flash`, memory_write `z-ai/glm-5.3`) cost $0.00165 per turn in
MekaHime's own usage ledger: 172 turns of real conversation on 2026-11-02, all four roles counted, tools excluded.
The heavier twelve-turn benchmark dialogues of October, on the same preset with its earlier GLM-5 writer, came to
$0.037 each, $0.0031 a turn (the Alibaba preset's note in `config/llm_roles.json`). Steady voice chat is roughly 100
turns an hour. Over a 30-day month: 30 turns a day (900) ≈ $1.50, or $2.80 at the benchmark rate; 100 turns a day,
an hour of talking every day (3,000) ≈ $5, or $9.30; her reflection and the openings she starts herself when the
person is quiet add about $0.005 a session. OpenRouter's prices change: the person's own figure is on the Use page,
in the *Session cost* tile (`… / dialogue` is the average per turn).

**Fully Local: which model, which card.** OpenAmika asks Ollama for a 16,384-token context (`num_ctx` in
`config/ollama.json`; her prompts are ~8k tokens), so a loaded model takes its weights, plus the KV cache of that
context (Ollama's default f16: 2 × layers × KV heads × head size × 2 bytes per token; Gemma 3 keeps the whole
context only in 10 of its 62 layers, the other 52 hold a 1,024-token window), plus about 1 GB for the runner and its
compute buffers (Ollama uses flash attention where the GPU supports it). Downloads as ollama.com lists them (checked
2026-11-04); the other columns in the GB graphics cards are sold in (GiB):

| model (Q4_K_M) | download | weights | KV cache at 16k | loaded | languages |
|---|---|---|---|---|---|
| `gemma3:27b` | 17 GB | 16.2 GB | 1.9 GB | **≈ 19 GB** | 140+, Chinese and Japanese among them |
| `qwen3:32b` | 20 GB | 18.8 GB | 4.0 GB | **≈ 24 GB** | 100+, Chinese and Japanese among them |
| `llama3.3:70b` | 43 GB | 39.6 GB | 5.0 GB | **≈ 46 GB** | eight per Meta, not Chinese or Japanese; her prompts are Chinese, so prefer `qwen2.5:72b` |
| `qwen2.5:72b` | 47 GB | 44.2 GB | 5.0 GB | **≈ 50 GB** | 29+, Chinese and Japanese among them |

Newer models of these sizes count the same way (for example `qwen3.6:27b`, an 18 GB download). What a model really
takes: `ollama ps` while she talks (`SIZE` is the total, and `PROCESSOR` must read `100% GPU`). Give all four roles
the same model: a second one must fit as well (both stay loaded for `keep_alive`, 30 min), or Ollama swaps them.

Her own parts on the same card (measured on RTX 50-series cards): ears (SenseVoiceSmall + Silero VAD) ~1.2 GB,
memory (bge-m3, fp16) ~1.4 GB, voice (GPT-SoVITS) ~2.5 GB, Windows and the browser ~1 GB: **~6 GB**; her eyes
(Qwen3-VL-2B) add ~4.3 GB: **~10 GB**.

| her GPU | a 27–32B model next to her | a 70B model next to her |
|---|---|---|
| 16 GB or less | no | no |
| 24 GB (RTX 3090, 3090 Ti, 4090, 5090 Laptop, RTX PRO 4000) | no: ≈ 19 + 6 = 25 GB even with her eyes off | no |
| 32 GB (RTX 5090, RTX PRO 4500) | `gemma3:27b` with all of her, eyes included (≈ 29.5 GB); `qwen3:32b` only with her eyes off (≈ 30 GB) | no |
| 48 GB (RTX PRO 5000 48 GB, RTX 6000 Ada, RTX A6000) | either, with all of her (≤ 34 GB) | no: ≈ 46–50 GB for the model alone |
| 72–96 GB (RTX PRO 5000 72 GB, RTX PRO 6000) | either | yes, with all of her (≤ 61 GB) |

A model that does not fit still runs: Ollama puts the rest of it on the CPU (`ollama ps` shows a CPU/GPU split) and
she becomes slow. When the person's GPU has no "yes" for the model they want:

- **Semi-Local**: the answer in almost every case.
- **Ollama on a second PC**, holding the model alone: 24 GB for `gemma3:27b`, 32 GB for `qwen3:32b`, 48 GB (only
  just) for `llama3.3:70b`, 64 GB or more for `qwen2.5:72b` (two 32 GB cards, a 72–96 GB card, or a Mac with 96 GB
  or more of unified memory). On that PC set the user environment variable `OLLAMA_HOST` to `0.0.0.0:11434`, quit
  Ollama and start it again, and allow TCP port 11434 in its firewall (Ollama has no password: only on a network the
  person trusts, or over Tailscale). Here: Setup → Brain → Ollama server `http://<that-pc>:11434` → Detect models
  (or `base_url` in the detect call below).
- **A second card in this PC**: give one card to Ollama alone (`CUDA_VISIBLE_DEVICES` set to its UUID from
  `nvidia-smi -L`, in Ollama's environment) and keep her parts on the other. They all use CUDA device 0 by default;
  when that is the card Ollama got, set `speech_device`, `embedder.device` and `vision.device` in `settings.json` to
  `cuda:1` and add `--device cuda:1` to `services.tts.command`.

**How fast, and why Fully Local is often slower than that.** mekahime.com/openamika estimates the time before her
voice starts from the card's memory bandwidth: a dense model reads all its weights for every token it writes, at ~70%
of the bandwidth, and takes in prompt tokens ~50 times faster; her voice starts once the model has read the new part
of her prompt and written ~30 tokens (her status, tone and mode lines, then her first clause). From a cached prompt
to reading all ~8k tokens again, that is ≈ 0.7–2.8 s on an RTX 5090 with `gemma3:27b` and ≈ 1.6–7.4 s on an RTX PRO
6000 with `qwen2.5:72b` (Semi-Local: ≈ 0.8 s on either), with no waiting counted. Ollama serves one request per model
at a time (`OLLAMA_NUM_PARALLEL` defaults to 1; each extra slot adds another KV cache to the tables above) and the
four roles share the model, so a call waits for whatever another role is writing, then reads its whole prompt again,
since that role's prompt replaced it in the cache. Three things hold the model between her replies: a cancelled
stream (the person interrupted her, or kept talking after a speculative turn began) is drained to its end, not
aborted (`drain_cancelled_streams` in `config/build_f.json`, up to 30 s for the surface, 90 s for the deep); the
memory writer starts as soon as she stops speaking; a reflection (the deep's 【想想】, or the idle reflection) runs while
the conversation goes on, thinking where the model can (`THINKING_ROLES` in `brain/f_ollama.py`). In MekaHime's
ledger of 2026-11-02 a quarter of the surface and deep calls were drained; the deep wrote 199 tokens (median), the
memory writer 463 (p90 759), the reflection 454 visible (p90 658), 1,296 with its reasoning (p90 2,891). At ~75
tokens/s (`gemma3:27b` on an RTX 5090) that holds the model ~2.7 s, ~6–10 s and ~6–9 s (~17–38 s for a model that
thinks); at ~27 tokens/s (`qwen2.5:72b` on an RTX PRO 6000) ~7 s, ~17–28 s and ~17–24 s. So a reply that lands behind
one of them takes 5–50 s; the page's "Up to" is the reply behind the memory writer (p90) that then reads its prompt
again: ≈ 13 s on an RTX 5090 with a 27B model, ≈ 36 s on an RTX PRO 6000 with a 70B one. Say so before the person
chooses Fully Local.

To switch: `ollama pull gemma3:27b` (or the model chosen above), then on the page: Setup → Brain → Ollama → Detect
models → Use this model. Headless, with her running (the speech gateway checks that `Origin` equals the address you
call and that `X-Build-F-Action` names the action):

```powershell
$page = "http://localhost:53000"                                   # chat_port
Invoke-RestMethod -Method Post -Uri "$page/api/build_f/ollama/detect" -ContentType "application/json" `
  -Headers @{ Origin = $page; "X-Build-F-Action" = "ollama-detect" } -Body '{"base_url": "http://127.0.0.1:11434"}'
Invoke-RestMethod -Method Post -Uri "$page/api/build_f/ollama/use" -ContentType "application/json" `
  -Headers @{ Origin = $page; "X-Build-F-Action" = "ollama-use" } -Body '{"model": "gemma3:27b"}'
```

- `detect` asks Ollama (`/api/version`, `/api/tags`, `/api/show` per model) and writes `config/ollama.json`
  (git-ignored): `{"base_url", "num_ctx": 16384, "keep_alive": "30m", "models": [{"name", "family",
  "parameter_size", "quantization", "size_gb", "context_length", "thinking", "vision"}], "version", "detected_at"}`.
  `use` takes `{"model": …}` for all four roles, or `{"roles": {"surface": …, "deep": …, "reflection": …,
  "memory_write": …}}` (a role left out takes `model`), and writes the selections in `config/llm_roles.json`:
  `"selections": {"<role>": "<role>:ollama:<model>"}`, for example `"deep": "deep:ollama:gemma3:27b"`.
- Refusals: no `Origin` (or the wrong one) → 403 `Use the OpenAmika page for this action.`; Ollama down → 503
  `Ollama is not answering at http://127.0.0.1:11434 — is it running? …`; a model not detected → 400 `gemma3:27b is
  not a detected Ollama model for the <role> role — detect models first.`; `use` while she speaks or writes memory
  → 409 `Wait for Amika to finish speaking, updating memory and applying service controls.` (power her off on the Use
  page, retry).
- Check: `launch.py --check` then reads `OK API keys  no OpenRouter key, none needed: every role runs on Ollama …`,
  `OK Ollama  <version> at http://127.0.0.1:11434` and `OK Ollama model gemma3:27b  pulled · surface, deep,
  reflection, memory_write`, or `!! Ollama  not answering at … — start Ollama …` / `!! Ollama model gemma3:27b  not
  pulled — ollama pull gemma3:27b (…)`. The pre-flight checks that the model is pulled, not its size: the 27B rule
  and the memory table above are yours to apply.
- Back to OpenRouter: Setup → Brain → OpenRouter → "Switch back to the OpenRouter preset".

### 5.5 Check, then start

```powershell
envs\brain\Scripts\python.exe launch.py --check     # starts nothing; one OK / -- / !! line per check
.\Start_OpenAmika.bat                               # opens her console; the browser opens http://localhost:53000
```

`--check` prints `OpenAmika pre-flight  <folder>`, rows such as `OK brain python`, `OK required files  all present`,
`OK her clock  Asia/Tokyo · Wed 14:04 JST`, `OK brain port  5202 free` (or, while she runs, `OK brain port
5202 held by this OpenAmika (running)`), `-- TTS (voice)  not running — will be started
(vendor\tts-engine\.venv\Scripts\python.exe)`, `OK her voice  MekaHime_Amika · 69/69 emotion clips`,
`OK Live2D model  Amika (AmikaV4.model3.json, 142 motions)`, `OK paper doll  Host · 64 motions — shown beside her
while a task agent is switched on (Tools page)`, `OK brain_python imports  all importable`,
`OK embedder check  embed_server check: torch …`, `OK vision check  vision check: all importable (…)`, then a count
line and `result: ready` (exit 0) or `result: fix the !! lines above (-- lines are informational)` (exit 1). Rows for
Ollama appear when a role runs on it (§5.4), rows for her helpers when one is switched on (`agent hermes`, `hermes
key`, `agent nanomuse`, §8.1). The same report is saved to `logs/preflight.json` and shown on the Setup
page and in the health light.

`Start_OpenAmika.bat` opens a console titled OpenAmika with a dashboard that redraws in place: the pre-flight
problems, then one row per service (`Services`: tts; `Core`: embedder, brain, vision, speech, messengers). A spinner
means starting or working; green `ready · pid … · <url>`; yellow `no answer after Ns — still running, see
logs/<name>.log`; red `exited with code N — see logs/<name>.log`. A normal start: her voice ~20 s, the embedder
10–60 s, the brain a few seconds, speech ~10–30 s; the browser opens when speech is up. The first start after the
setup is slower: new environments spend a minute or more on their first imports (on fresh clones speech answered
after 1 to 2 minutes, the longer one on a busy PC), so speech may read yellow `no answer after 90s — still running`
first; it turns green and the browser opens when it answers (`launch.py --no-browser` never opens it).

### 5.6 Other setups

- **CPU only**: `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 --cpu` and
  `envs\brain\Scripts\python.exe scripts\setup_tts.py --cpu`; `settings.json` then has `speech_device: "cpu"`, the
  embedder on `cpu` / `fp32`, vision off. Slow, but complete.
- **Your own environments** (conda or other): create them yourself (Python 3.11; the speech, embed and vision ones
  need a CUDA torch first: `pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128`
  for speech, `torch==2.10.0` for the embedder, `torch==2.10.0 torchvision==0.25.0` for vision; then their
  requirements file). Make the two files the setup would make, `Copy-Item settings.example.json settings.json` and
  `Copy-Item .env.example brain\.env`, point `brain_python`, `speech_python`, `embedder.python` and `vision.python` in
  `settings.json` at your environments, run `python scripts\download_sensevoice.py`, then `setup_tts.py` as above.
- **Linux / macOS**: untested. The scripts have Linux paths (`envs/<name>/bin/python`, CUDA or CPU torch); the
  launcher's process handling and the `.bat` files are Windows-first.

### 5.7 Updating

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Stop_OpenAmika.ps1   # stop her first (or Ctrl+C in her console)
git stash                       # keeps the person's tracked changes (below)
git pull
git stash pop                   # a conflict here: keep the person's side of their files
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
envs\brain\Scripts\python.exe scripts\setup_tts.py
envs\brain\Scripts\python.exe launch.py --check
```

- After onboarding, tracked files hold the person's choices: `config/build_f.json` (name, city, her clock),
  `config/word_replace.json` (the pronunciation rule for their name), `config/llm_roles.json` (Setup → Brain) and
  `brain/表层.txt`, `例子.txt`, `思考.txt`, `scenes.json` (Setup → Personality). A plain `git pull` stops on them
  (`Your local changes to the following files would be overwritten by merge`), hence the stash.
- Both setups fetch only what changed (a requirements file, the pinned revision of her repository). Stop her first:
  pip cannot replace files a running service holds open on Windows (`Access is denied`, and the summary shows only
  `command failed (1)`).

## 6. Configuration

**`settings.json`** (from `settings.example.json`; git-ignored; read at launch):

| key | default | meaning |
|---|---|---|
| `brain_python`, `speech_python` | `envs\brain\Scripts\python.exe`, `envs\speech\Scripts\python.exe` | interpreters (relative to the folder, or absolute) |
| `brain_port`, `chat_port` | 5202, 53000 | the brain; the web pages + speech gateway (the URL she opens) |
| `messengers_port` | 5215 | the Discord / Telegram service (`messengers/service.py`); give a second checkout another one |
| `local_model_url` | `http://127.0.0.1:11434/v1/chat/completions` | Ollama's OpenAI-compatible endpoint |
| `speech_device` | `cuda:0` | STT device (`cpu` works, slower) |
| `tts_url`, `tts_character` | `http://127.0.0.1:8002`, `MekaHime_Amika` | her voice server and the voice id (used when the voice adapter is off) |
| `embedder` | python `envs\embed\Scripts\python.exe`, port 8011, model `BAAI/bge-m3`, tag `bge-m3-d768`, dims 768, dtype fp16, device cuda, load_timeout_s 600 | her memory's embedder |
| `vision` | enabled (per the setup), python `envs\vision\Scripts\python.exe`, port 8014, model `qwen3-vl-2b` (also `qwen3-vl-4b`, `qwen2.5-vl-3b`, `smolvlm2-2.2b`, `smolvlm2-500m`), max_tokens 60, max_dps 1.0, device cuda, load_timeout_s 900 | her eyes |
| `services.tts` | enabled; `vendor\tts-engine\.venv\Scripts\python.exe main.py --port 8002 --host 127.0.0.1 --character MekaHime_Amika` in `vendor\tts-engine`; health `http://127.0.0.1:8002/ping`; timeout 240 s | started by the launcher; keep `--character` equal to `tts_character` |
| `services.mqtt`, `services.zigbee2mqtt` | disabled | the smart home (§8; the person enables them) |

**`brain/.env`** (from `.env.example`; secrets only): `OPENROUTER_API_KEY` (the default provider of all four roles;
not needed when every role is on Ollama); optional `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` (Alibaba Model Studio,
workspace in `config/alibaba_models.json`), `GEMINI_API_KEY` (legacy embedder), `GROQ_API_KEY`, `CEREBRAS_API_KEY`,
`XAI_API_KEY`, `ANTHROPIC_API_KEY` (legacy direct providers, off by default).

**`config/`**: `build_f.json` (identity and clock — §1; `memory_budget_chars`, `recent_turns`, `idle_reflection`,
`auto_open_from_reflection`, `embedding_provider: "local"`, `embedding_url`, budgets), `llm_roles.json` (`options`,
`presets`, `selections` for `surface` / `deep` / `reflection` / `memory_write`; Setup → Brain edits it),
`openrouter_models.json` / `alibaba_models.json` / `pricing.json` (catalogs and prices; Ollama is priced at 0),
`word_replace.json` (`stt_to_llm` fixes, `llm_to_tts` pronunciations incl. the wizard's `role: user_name` rule),
`tools.json` (git-ignored; MCP servers, the helpers' URLs, keys and `autostart`, policies, `clerk_role`,
`iot_disclaimer`; template `tools.example.json`; §8.1),
`messengers.json` (git-ignored; bot tokens and allow-lists; template `messengers.example.json`), `ollama.json`
(git-ignored), `voice_map.json` (git-ignored; voice adapter edits).

## 7. The page

`http://localhost:53000` (`chat_port`) redirects to `/use`. Pages: **Use** (`/use`: the Live2D stage, with the paper
doll beside her while a task agent is on, power, mic, text box, camera button, model picker, the **Her helpers**
card (jobs in flight; Approve / Deny / Answer / Stop for anything waiting on the person), the **Tool dialogue**
card (Professional), cost and latency telemetry, the first-run wizard), **Setup** (`/setup`:
system health, startup checks, brain and models, voice adapter grid, avatar banks, personality, vision, "Text her",
danger zone: wipe her memory / reset everything; Simple and Professional modes), **Tools** (`/tools`: MCP servers
and task agents with their health, Start and Test; per-tool policies `auto` / `confirm` / `deny`, Add a server and
Raw config (tools.json) in Professional mode; the IoT switch and its safety statement), **About**
(`/about`: MekaHime, supporting the project, licenses), and `/flow` (the interactive architecture map).

The light at the top right of every page (`ui/common.js`, `healthCheck`) is green *System Ready*, yellow *Warnings*
or breathing red *System Failures*; click it for the list. It polls: Brain, Memory, OpenRouter key (when a role uses
OpenRouter) or Ollama (when one uses Ollama), Speech / STT, TTS (her voice), Live2D model, every non-green startup
check from `logs/preflight.json`, and configured bots that cannot connect. The first-run wizard (until
`onboarded` is true in `config/build_f.json`) asks the person's name (with a pronunciation guide for her voice), city
and her clock, takes the OpenRouter key if `brain/.env` has none, and runs the same health check. The browser needs
one click on the page before it allows her audio.

## 8. Optional add-ons and their rules

Not installed by the setup; the core runs without them.

- **Tools / MCP** ([docs/TOOLS.md](docs/TOOLS.md)): `config/tools.json`, edited on the Tools page. New servers start
  disabled. She decides in character; a post-speech "clerk" call turns what she agreed to into tool calls, each tied
  to a verbatim quote of the person's request; `confirm` tools wait for the person's approval on the Use page.
- **Task agents** (§8.1, [docs/TOOLS.md](docs/TOOLS.md),
  [servers/openamika-agents/README.md](servers/openamika-agents/README.md)): Hermes Agent and nanoMuse as
  `ask / status / answer / stop` tools, with the paper doll standing in for them on her stage; their URLs, keys and
  start commands live in their entries in `config/tools.json`.
- **Smart home** ([IoT/README.md](IoT/README.md), [docs/IOT_DISCLAIMER.md](docs/IOT_DISCLAIMER.md)): Zigbee plugs via
  mosquitto + zigbee2mqtt + `servers/openamika-iot`. Until the current IoT safety statement is accepted on the Tools
  page, the brain refuses to start or connect any smart-home server, the launcher holds back `services.mqtt` and
  `services.zigbee2mqtt` (pre-flight row `-- IoT safety statement  not accepted (version …)`), and
  `servers/openamika-iot` refuses to start. A new statement version turns IoT off again. Rule 3 of §0 applies: only
  the person turns it on, only harmless devices.
- **Text her** ([docs/MESSENGERS.md](docs/MESSENGERS.md)): Discord (needs `pip install -r messengers\requirements.txt`
  in `envs/speech`) and Telegram bots, set on Setup → Text her; only allow-listed user ids reach her.
- **Ollama** (Fully Local): §5.4. Only a model of 27B or more (70B or more for the full, reliable experience;
  smaller ones break her character and her format). Next to her on one card, a 27B model needs a 32 GB GPU and a 70B
  one 72–96 GB; otherwise Ollama on a second PC (Setup → Brain takes its URL), or, in almost every case the better
  answer, Semi-Local. Fully Local is slower too (§5.4, *How fast*).
- **Phone**: `Tailscale_Serve_OpenAmika.bat` (needs Tailscale with MagicDNS + HTTPS); one conversation tab at a time.
  Every page it serves includes the Tools page, whose raw editor shows `config/tools.json` with the helpers' keys.

### 8.1 Her helpers: Hermes Agent, nanoMuse and the paper doll

Separate programs the person installs (Hermes Agent: MIT, Nous Research; nanoMuse: GPL-3.0-or-later); OpenAmika
talks to them over HTTP through `servers/openamika-agents` and never bundles them. The human tutorial is README
"Her helpers" and [docs/TOOLS.md](docs/TOOLS.md). Rule 4 covers every key here: generate a key into a variable,
never echo it, never pass a literal key on a command line, never print `config/tools.json` or a helper's `.env`
back. Rule 2's spirit covers approvals: only the person answers a helper's approval or question (their spoken yes,
quote-checked, or the Use page's buttons); never call `POST /api/build_f/tools/approve` or `/job` for them.

**Hermes Agent** (tested: 0.22.4 and 0.22.7, stable channel; Windows, a normal PowerShell window):

```powershell
& ([scriptblock]::Create((irm https://hermes-agent.nousresearch.com/install.ps1))) -NonInteractive   # HERMES_HOME = %LOCALAPPDATA%\hermes; hermes on the user PATH
# a new window, then:
hermes model                                                  # interactive: OpenRouter, the person's key, "Enter custom model name": z-ai/glm-5
$key = [guid]::NewGuid().ToString('N')                        # 32 hex characters
hermes config set API_SERVER_ENABLED true                     # both go to %LOCALAPPDATA%\hermes\.env
hermes config set API_SERVER_KEY $key
hermes gateway stop                                           # a gateway already running read .env without the key; "No gateway running for this profile" is fine
hermes config set browser.headed true                         # optional: a visible browser (config.yaml)
hermes config set AGENT_BROWSER_HEADED true                   # optional, needed on Windows for that (.env)
```

- `-NonInteractive` is the installer's own switch (`iex (irm …)` cannot pass it, hence the scriptblock form, which
  the installer supports). It skips the installer's two interactive stages, `hermes setup` and `hermes gateway
  install --if-missing`. Run as `iex (irm …)`, the latter asks "Start the gateway now after install?" and "Start the
  gateway automatically on Windows login with a Scheduled Task?", both default yes (`HERMES_GATEWAY_INSTALL_START_NOW`
  / `HERMES_GATEWAY_INSTALL_START_ON_LOGIN` = `0`/`1` preset them). A gateway started then has no API server (no
  key yet), and a running gateway reads `.env` only at its start; the one OpenAmika launches finds it and leaves at
  once, so the card ends `start failed` (see §9). The `hermes gateway stop` line above covers that case.
- `z-ai/glm-5` is not in `hermes model`'s OpenRouter list (0.22.4 and 0.22.7 list `z-ai/glm-5.2` and the `5.3`
  family): the person picks **Enter custom model name** and types `z-ai/glm-5`, so that it matches `HERMES_MODEL`.
- Hermes opens its API server (`127.0.0.1:8642`; `API_SERVER_HOST`, `API_SERVER_PORT`) when `API_SERVER_KEY` is
  usable: 16 characters or more and not a placeholder (`hermes_cli.auth.has_usable_secret`, min_length 16).
  Otherwise the gateway runs without it and logs nothing about it. Every route but `GET /health` needs
  `Authorization: Bearer <API_SERVER_KEY>`; a wrong one gets 401 `Invalid gateway API key (API_SERVER_KEY)`.
- Check by hand: `hermes gateway` (foreground), then `Invoke-RestMethod http://127.0.0.1:8642/health` → status `ok`,
  and Hermes's `%LOCALAPPDATA%\hermes\logs\gateway.log` has `[Api_Server] API server listening on
  http://127.0.0.1:8642 (model: hermes-agent)`. Ctrl+C; OpenAmika starts it from then on. Diagnostics: `hermes doctor`.
- Its data (`.env`, `config.yaml`, `auth.json`, `state.db`, `memories/`, `sessions/`, `logs/`) stays in its home.
  `Auxiliary Nous client unavailable` warnings are harmless with OpenRouter (the launcher hides them).

**nanoMuse** (tested: 1.0.9; its Python runtime `nanomuse serve`, not the nanoMuse Desktop app; not on PyPI), from the
OpenAmika folder (`Nanomuse/` is git-ignored):

```powershell
git clone --depth 1 https://github.com/nano-muse/nanoMuse.git Nanomuse\repo
py -3.11 -m venv Nanomuse\venv
Nanomuse\venv\Scripts\python -m pip install -e "Nanomuse\repo[browser]"   # ~1 min, venv ~310 MB
Nanomuse\venv\Scripts\python -m playwright install chromium               # ~440 MB in %LOCALAPPDATA%\ms-playwright
mkdir Nanomuse\data                                                       # then write Nanomuse\data\config.toml
Nanomuse\venv\Scripts\nanomuse vault set OPENROUTER_API_KEY --config Nanomuse\data\config.toml   # hidden prompt: the person types the key
```

- `config.toml` (TOML: forward slashes in paths): `data_dir = "C:/OpenAmika/Nanomuse/data"` (absolute: nanoMuse
  resolves a relative one from its working folder, and `vault set` writes `vault.enc` / `vault.key` there);
  `[llm] provider = "openai"`, `base_url = "https://openrouter.ai/api/v1"`, `model = "z-ai/glm-5"`,
  `api_key = "{{vault:OPENROUTER_API_KEY}}"`; `[agent] instructions` (who it works for; the bridge has no
  per-run instructions for nanoMuse); `[sandbox] mode = "off"`; `[browser] enabled = true`, `headless = false`;
  `[cloud] required = false`, `sync = false`, `models = false`; `[hub] enabled = false`; `[server] host =
  "127.0.0.1"`, `port = 8787`, `approval_timeout = 600`. The full file is in docs/TOOLS.md.
- Not `api_key = "${OPENROUTER_API_KEY}"`: `launch.py` starts nanoMuse before the brain and never passes
  `brain/.env` on to it (it reads that file only for its own key check), so nanoMuse would start with no key ("No
  model yet"), answer every route, and fail every task.
  `vault set --value` exists but puts the key on a command line: do not use it.
- Check by hand: `Nanomuse\venv\Scripts\nanomuse serve --config Nanomuse\data\config.toml --no-qr --host 127.0.0.1
  --port 8787` → `nanoMuse … is ready.`, and `Nanomuse\data\server_token` exists (written on the first start; the
  printed link carries it: never repeat it). Exit code 98 = port 8787 taken; 78 = security software intercepting
  loopback. A model chosen in nanoMuse's own web app (`<data_dir>\app-settings.json`) overrides `config.toml`.

**`config/tools.json`** (git-ignored; `Copy-Item config\tools.example.json config\tools.json` when it does not exist,
else merge the two entries into its `mcpServers`; the Tools page's Raw config, Professional mode, edits the same
file; the brain re-reads it within 30 s, a Tools-page save at once; the launcher reads it at start):

| key | example value | meaning |
|---|---|---|
| `enabled` (top level) | `true` | the master switch, "Tools enabled" on the Tools page |
| `mcpServers.<name>.command` / `args` / `cwd` | `python` / `["-m", "openamika_agents", "hermes"]` (or `"nanomuse"`) / `../servers/openamika-agents` | the bridge: `python` = the brain's interpreter, `cwd` relative to `brain/`; nothing to `pip install` |
| `….enabled`, `….kind` | `true`, `"agent"` | the card's switch; `kind: agent` makes it a helper and seats the paper doll |
| `….autostart` (hermes) | `{"command": "%LOCALAPPDATA%/hermes/bin/hermes.exe", "args": ["gateway"], "cwd": "%LOCALAPPDATA%/hermes"}` | the helper's own start command; `%VARIABLES%` are expanded in `command`, `args` and `cwd`, and a relative `command` or `cwd` is taken from the OpenAmika folder (launcher and brain alike; `env` values are passed as written); a string with quotes around a spaced path works too |
| `….autostart` (nanomuse) | `{"command": "C:/OpenAmika/Nanomuse/venv/Scripts/nanomuse.exe", "args": ["serve", "--config", "C:/OpenAmika/Nanomuse/data/config.toml", "--no-qr", "--host", "127.0.0.1", "--port", "8787"], "cwd": "C:/OpenAmika/Nanomuse/data", "env": {"PYTHONUTF8": "1"}}` | change `C:/OpenAmika` when the folder is elsewhere |
| `env.HERMES_URL`, `env.HERMES_API_KEY` | `http://127.0.0.1:8642`, = `API_SERVER_KEY` | the key is sent as a bearer on every call |
| `env.HERMES_SESSION` | `openamika` | one continuing conversation; a session a copied Hermes home refuses (409 `profile_mismatch`) is replaced once by `openamika-<pc>` |
| `env.HERMES_PROVIDER`, `env.HERMES_MODEL` | `openrouter`, `z-ai/glm-5` | must match `hermes model`; they price the token totals Hermes reports (ledger phase `agent:hermes`, an estimate) |
| `env.HERMES_INSTRUCTIONS`, `env.HERMES_SURFACE_BROWSER` | the example's text; unset (= on) | sent with every run; `0` stops raising Hermes's browser window |
| `env.NANOMUSE_URL`, `env.NANOMUSE_TOKEN_FILE` (or `NANOMUSE_TOKEN`) | `http://127.0.0.1:8787`, `C:/OpenAmika/Nanomuse/data/server_token` | the token file is read again until it exists |
| `env.NANOMUSE_LANGUAGE`, `env.NANOMUSE_THREAD` | `en` (`ja`, `zh-CN`), unset (= `OpenAmika`) | its reply language; the one thread the bridge uses |
| `….persona_note` | Chinese, `{user_name}` | what she is told about the helper |

**What runs.** `launch.py` starts each enabled agent's `autostart` with the services (hidden, output to
`logs/agent-<name>.log`, health-gated up to 90 s on `HERMES_URL/health` or `NANOMUSE_URL/api/health`, recorded in
`logs/instance.json` so `Stop_OpenAmika` stops it; one already running is used as is and left running). The brain
spawns one bridge per enabled agent over stdio, probes its `health` every 30 s and before every `ask`, relaunches a
helper that is down (detached, so it outlives a brain restart; 90 s grace, at least 120 s apart, at most 3 tries
until the Tools page's **Start**), and polls jobs every 2.5 s. `Stop_OpenAmika` and Ctrl+C kill the brain's process
tree, which includes a helper that brain relaunched; only one relaunched before a brain restart survives them. A job
ends after 900 s at most (`--timeout N` in the entry's `args`), an unanswered approval included (nanoMuse itself
denies one after its `approval_timeout`, 600 s in the tutorial's `config.toml`); a question still open after the
helper's run ended (Hermes's final clarify) is dropped after 30 minutes.

**Verify** (read, never print keys):
- `envs\brain\Scripts\python.exe launch.py --check`: `OK agent hermes  online at http://127.0.0.1:8642` (or `--
  agent hermes  not running — will be started (…)`), `OK hermes key  HERMES_API_KEY is the API_SERVER_KEY in
  …\.env`, `OK agent nanomuse  online at http://127.0.0.1:8787`, `OK paper doll  Host · 64 motions — stands in for her
  helpers on the Use page`.
- While she runs: `GET http://127.0.0.1:53000/api/build_f/tools` → `servers.hermes.kind == "agent"`,
  `servers.hermes.health.ok == true` (`reason`, `detail`, `version`). Its `config` field is the raw `tools.json`
  with the keys: read `servers` only. `GET http://127.0.0.1:53000/api/avatar/models` lists `Host` with `"role":
  "agent"` and `"stage": {"scale": 0.42, "nx": 0.84, "ny": 0.7, "head_y": 0.26}`; the Use page's browser console
  logs `[Live2D] agent avatar Host ready (64 motions)`.

**The paper doll on the Use page** (`ui/use.js` polls `/api/build_f/tools` every 2 s while connected, 10 s
otherwise; `ui/live2d-engine.js` draws it): seated bottom-right when the top-level `enabled` is true and any
enabled server is `kind: agent` (healthy or not), after her own model has loaded; gone when none is. Its base motion
is `think*` while any job is not finished, else `idle*`; its own 250 ms state loop alone picks its motions (the
gateway puts every loose motion of a model3.json without `Motions` into one `Idle` group, so `loadAgent` points the
library's idle group, `motionManager.groups.idle`, at a group that does not exist: the library never starts a random
one on the doll). Each new line of the tool dialogue becomes a bubble: hers
(who `amika`: the command she typed, her answers) out of her while the doll plays `listen*`; the helper's (who
`agent`) out of the doll with `talk*`, or the line's mood: `happy*` on a result, `worried*` on an error or a stop,
`surprise*` on a question or an approval. A line marked `"internal": true` (a step of the bridge's own: `sent`,
`started`, `continuing`, `stop requested`, `approval granted`, a session note) gets no bubble. Without a `role:
agent` model there is no doll and no helper bubbles; the **Her helpers** card and (Professional) the **Tool
dialogue** card still show everything.

## 9. Troubleshooting

Start with `envs\brain\Scripts\python.exe launch.py --check`, then `logs\<name>.log` of the red row.

**Setup (`setup_windows.ps1` / `setup_core.py`)**

- `[setup] Python 3.11 was not found.` → install Python 3.11 64-bit, open a new window, run again.
- `!! brain|speech recognition|memory (embedder)|vision (her eyes)  …: these do not import: X (…)` → a pip install
  failed or was interrupted: run again (a package that is installed but damaged is installed again by itself), then
  with `--reinstall`; read the pip error above the summary (disk full, network, a proxy).
- `… The filename or extension is too long …` in any import row (also the voice server's `!! imports`, and
  `launch.py --check`'s embedder or vision check) → the folder path is too long for Windows' DLL loader, long paths
  on or off: move OpenAmika to a short folder such as `C:\OpenAmika` and run both setups there. Re-running in place
  (or `--reinstall`) cannot fix it.
- `!! … does not see the GPU (CUDA)` → NVIDIA driver too old or missing (`nvidia-smi` must work): update it and run
  again, or use `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 --cpu`.
- `!! speech model  speech/models/ is not complete …` → ModelScope or Hugging Face unreachable: run again (finished
  files are kept), or download the six files by hand from https://www.modelscope.cn/models/iic/SenseVoiceSmall into
  `speech/models/` and run `python scripts\download_sensevoice.py --verify`.
- `!! memory model|vision model  … could not be downloaded` → Hugging Face unreachable, or the disk is full: run again.
- `!! settings.json  settings.json names X as Y, which does not exist: point Y at envs\…` → set it to the environment
  the row names, or delete `settings.json` and run the setup again. `-- settings.json  existing, not changed; it
  names envs\… (see the … row), which is not made yet` → fix that environment's row; settings.json is fine.
- `WARNING: this folder is synced by OneDrive` / `… is N characters long. Windows cannot load some program files
  (DLLs) …` / `Windows long paths are off` → move the folder (for example to `C:\OpenAmika`) and run again.

**Her voice, her Live2D model and the paper doll (`setup_tts.py`)**

- `!! voice MekaHime_Amika  not downloaded: her license was not accepted …` (also `!! Live2D model Amika …`,
  `!! paper doll Host …`) → the person must type `yes` (§0 rule 2), or `--accept-voice-license` after they accepted.
- `… Hugging Face refused the download (HTTP 401): MekaHime/MekaHime_Amika is not public yet …` → set `HF_TOKEN` to a
  read token of an account with access; `… did not accept your Hugging Face token (from HF_TOKEN) (HTTP 401) …` → the
  token is wrong or expired; `… the account of your Hugging Face token … cannot read MekaHime/MekaHime_Amika (HTTP 403|404)`
  → that account has no access.
- `… Hugging Face has no <file> at revision a3934e5a of MekaHime/MekaHime_Amika (HTTP 404 …): update OpenAmika (git pull)`
  → the pinned revision changed upstream; update the checkout (§5.7). For the paper doll the revision named is
  `PAPER_DOLL_REV`'s.
- `… download failed after 4 tries: <url>: <error>. Check the internet connection …` → run again; it resumes. If
  huggingface.co is blocked where the person is, set `HF_ENDPOINT` to a mirror (§5.3).
- `… SHA-256 mismatch … The file at that address is not the pinned one. Files already in place are kept …` → the
  bytes differ from the manifest: run again; if it repeats, update OpenAmika; never edit the manifest to make it pass.
- `!! venv|torch|packages|imports …` → the voice server's environment: run again (`--reinstall` runs the pip steps
  again, torch with `--force-reinstall`). An `!! imports` row ending `installed but damaged: <python> -m pip install
  --force-reinstall --no-deps <name>==<version> …` → run exactly that command, then the setup; one ending `the folder
  path is too long for Windows …` → move the folder (above). `the voice engine needs Python 3.11 or 3.12` → run it
  with `envs\brain\Scripts\python.exe` or `--python <path>`.
- `-- launcher  services.tts runs …, not this server` / `tts_character is …` → `settings.json` does not start this
  voice server: copy `services.tts` and `tts_character` from `settings.example.json`.

**Launcher and services**

- `OpenAmika could not start: settings.json not found — run the setup first: …` → §5.2.
- `!! brain python  missing: … — run powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 …` (also
  speech, embedder, vision) → §5.2, or fix the path.
- `!! required files  speech/models/model.pt (speech recognition model …)` → §5.2 (it downloads SenseVoiceSmall).
- `!! TTS (voice)  her voice server is not installed — run … scripts\setup_tts.py` / `… is incomplete (GPT-SoVITS
  missing) …` → §5.3.
- `!! her voice  vendor\tts-engine\Speakers\MekaHime_Amika is not installed (no character.json) — run …` → §5.3.
- `!! Live2D model  no Live2D model in avatar/models/: the stage stays empty — run …` → §5.3 (or put another Cubism
  model folder in `avatar/models/`).
- `!! TTS (voice)  port 8002 answers but has no voice MekaHime_Amika (another voice server, OpenSakura's for one?) — …`
  → something else answers `/ping` on her voice port, and `GET /api/characters` does not list her voice (OpenSakura's
  voice server also defaults to 8002). Stop it, or move hers: the port in `tts_url` and in `services.tts` (`--port`,
  `health`) of `settings.json`.
- `-- API keys  no OpenRouter key yet …` → §5.4. `!! Ollama  not answering at …` → start Ollama (or fix its address
  on Setup → Brain); `!! Ollama model X  not pulled — ollama pull X …` → run that.
- On Ollama she is slow → `ollama ps`: a CPU/GPU split under `PROCESSOR` instead of `100% GPU` means the model does
  not fit next to her (the tables in §5.4); with `100% GPU`, slow replies after an interruption, or while she saves a
  memory or reflects, are her roles waiting for one another on the one model (§5.4, *How fast*), not a fault. On
  Ollama she is out of character, forgets, or her lines come out broken → the model is under 27B: §5.4.
- `!! her clock  <python> has no time-zone database (the tzdata package), so the brain cannot start — …` → Windows
  has none of its own and the brain builds its zone at import: run the setup again, or `<python> -m pip install
  tzdata` (it is in `brain/requirements.txt`). `-- her clock  build_f.json timezone 'X' is not a time zone (…) — the
  brain falls back to Asia/Tokyo` → only the name is wrong: the wizard on the Use page sets it again.
- `OK brain port|web/speech port|…  N held by this OpenAmika (running)` → she is running from this folder: fine.
  `!! brain port|…|messengers port  N is in use by pid P (name)` → another program (or another OpenAmika checkout,
  which uses the same default ports) holds the port: stop it, or give one of them other ports in `settings.json`
  (`brain_port`, `chat_port`, `messengers_port`, `embedder.port`, `vision.port`, and the TTS port above). `… held by
  pid P (python.exe) started from an administrator window …` → close that window or run `Stop_OpenAmika.bat` as
  administrator.
- Dashboard `tts  exited with code 2 — see logs/tts.log` and in the log `no speakers in …\Speakers (each needs
  character.json, the .ckpt/.pth weights and references/emotions.json; run scripts/setup_tts.py)` → her voice is
  missing: §5.3. `--character MekaHime_Amika not found` → the voice folder name differs from `tts_character`.
- `embedder  no answer after 600s — still running, see logs/embedder.log` → usually the first start downloading
  bge-m3 (`… is not in the Hugging Face cache yet: downloading it once`): wait; `setup_core.py` avoids this.
- `speech  exited with code N — see logs/speech.log` (the browser never opens) → SenseVoice files missing or CUDA
  torch broken in `envs/speech`: `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 --check`, then
  the setup without `--check`.
- `vision  exited …` → out of VRAM is the usual cause: `"vision": {"enabled": false}` or a smaller model.

**Her helpers (§8.1)** — the pre-flight rows, the Tools card's tag and detail (also in `logs/brain.log` as
`[tools] <name>: UNREACHABLE — <detail>`), and what she relays:

- `-- hermes key  API_SERVER_KEY in <home>\.env is missing, shorter than 16 characters or a placeholder, so Hermes
  never opens its API server and she cannot reach it — …` → the person sets a key (§8.1); a gateway already running
  reads it only at its start, so stop it (`hermes gateway stop`, or end `hermes.exe`), then **Start** on the card.
  The card offers **Start** only while nothing answers (not for a helper that refuses tasks, rejects the key or has
  no model: that one is running already).
  The Hermes home is found from the autostart's env `HERMES_HOME`, its `cwd`, the folder above `bin\hermes.exe`,
  `HERMES_HOME`, then `%LOCALAPPDATA%\hermes`; a key set in `config.yaml` (`gateway.api_server`) is not checked.
- `-- hermes key  HERMES_API_KEY in config/tools.json is not set yet: …` (the example's placeholder) or `-- hermes key
  tools.json HERMES_API_KEY differs from API_SERVER_KEY in <home>\.env — Hermes will reject her` → make them equal,
  without printing either; the card says `Hermes is running but rejects the API key (HERMES_API_KEY in
  config/tools.json must equal API_SERVER_KEY in the .env of the Hermes home, %LOCALAPPDATA%\hermes\.env by
  default)`.
- `!! agent hermes|nanomuse  not running, and its start command does not exist: <path> — install it, or fix
  "autostart" in config/tools.json` → install it (§8.1) or correct the path. `!! agent …  not running at <url> and no
  autostart configured (Tools page)` → add `autostart` or start it by hand. `-- agent …  not running — will be started
  (…)` is informational and turns green when it answers.
- Card `agent unreachable`: ``Hermes: nothing answers at http://127.0.0.1:8642/health (…) — is `hermes gateway`
  running, with an API_SERVER_KEY of 16 or more characters in its .env? …`` → not running, or running without its
  API server (no `[Api_Server] API server listening …` in its `logs\gateway.log`): the key rule above.
  ``nanoMuse: nothing answers at http://127.0.0.1:8787/api/health (…) — is `nanomuse serve` running?`` → read
  `logs/agent-nanomuse.log` (exit code 98: port 8787 taken; `config.toml is not valid TOML`: backslashes).
- Card `agent starting…` (`starting <name> (Ns): <command>`) → wait, up to 90 s. Card `start failed`: `could not start
  <name>: FileNotFoundError …`, `<command> exited with code N — see logs/agent-<name>.log`, or `<name> did not come up
  after 3 start attempts (…) — see logs/agent-<name>.log; press Start to try again` → read that log.
- Hermes `… exited with code 0` with `The host gateway already serves profile 'default' — nothing to start` in
  `logs/agent-hermes.log` (0.22.4), or `exited with code 1` with `A gateway is already running` (0.22.7), while
  nothing answers on 8642 and `launch.py --check` says `OK hermes key` → a gateway started before `API_SERVER_KEY`
  was set (the interactive installer's "Start the gateway now?", or the person's own) is still running without its
  API server: `hermes gateway stop`, then **Start** on the card.
- Card `agent refusing tasks`: `Hermes <v> is running but refused the last task (Hermes refused POST /v1/runs: HTTP
  429|409|503 …); check the Hermes gateway and HERMES_SESSION — a task it accepts clears this` → wait (it also clears
  after 10 minutes). A card note `Hermes refused the configured session 'openamika' (profile_mismatch: it was created
  by another Hermes install); using session 'openamika-<pc>' on this install instead` needs nothing.
- Card green, yet every Hermes task fails (she relays Hermes's error) → its model: `hermes model`, `hermes doctor`,
  `%LOCALAPPDATA%\hermes\logs\errors.log`.
- Card `agent has no model`: `nanoMuse <v> is running but has no working model: its [llm] api_key is empty or
  unresolved (…)`, or a nanoMuse job failed with `The model provider refused the API key. Check it under Connections.
  (nanoMuse's model key is missing or wrong: see docs/TOOLS.md)` → the person runs `vault set` again (§8.1) and
  `api_key = "{{vault:OPENROUTER_API_KEY}}"` stays in `config.toml`. `nanoMuse is running but rejects the token
  (NANOMUSE_TOKEN / NANOMUSE_TOKEN_FILE should hold <data_dir>/server_token)` → point the entry at that file.
- A job failed `timed out waiting for Hermes` / `… nanoMuse` → 900 s passed (often an unanswered approval).
  She says she could not reach a helper ("联系不上") only for a connection error; a helper's own refusal is relayed
  with its reason.
- `-- paper doll  a task agent is switched on, but avatar/models/ has no paper doll (…)` → `setup_tts.py` installs it
  (§5.3). `-- paper doll Host  not downloaded: this OpenAmika does not pin the revision …` (setup summary) → expected
  until `PAPER_DOLL_REV` is set by MekaHime's release; update OpenAmika later. `-- paper doll Host  not copied: <folder>
  has no live2d/Host/ …` (with `--from-dir`) → that copy predates the doll; her voice and model installed anyway
  (exit 0). `OK paper doll` but no doll on stage →
  the top-level `enabled` or the helper's `enabled` is false, or her own model did not load (the doll is seated
  after it).

**The page**

- Stage shows `No Live2D model yet` → §5.3, then reload. `Model failed to load` → `setup_tts.py --verify` names the
  broken files; a re-run repairs them.
- Health light `TTS (her voice)  unreachable …` → her voice server is down or still warming up (~20 s): see the
  `tts` row and `logs/tts.log`.
- Health light `OpenRouter key  no key saved` → §5.4. `Ollama (local brain)  not answering …` → start Ollama.
- Text but no sound → click the page once (autoplay); TTS must be green.
- Wizard says `Some part is missing — she can't fully work yet.` → fix the red rows on the Setup page.
- Wipe / reset answers 409 → power her off on the Use page (she must be idle), wait a few seconds, retry.

## 10. Tests and CI

```powershell
envs\brain\Scripts\python.exe -m pip install -r requirements-dev.txt
envs\brain\Scripts\python.exe -B -m pytest tests -k "not live" -p no:cacheprovider -q
```

About 490 offline tests: the output contract and both streaming parsers (parity), prompt assembly, memory, the usage
ledger, the control proxies, Fully Local (every role, her surface turn included, on the Ollama transport), the tools
clerk (with agent autostart and health), the agents bridge against in-process
fakes of Hermes and nanoMuse (`tests/test_agents_bridge.py`: jobs, approvals, questions, stop, health with
`no_model` and `refused`, the copied-home session fallback, the bridge's own steps marked `internal`, a real MCP
stdio round trip), the paper doll on the Use page (by source: only its state loop picks its motions, no bubble for
the bridge's own steps), the IoT safety statement,
the launcher pre-flight (`tests/test_launch_preflight.py`: a fresh checkout, Ollama, a foreign voice server on her
port, her own running ports, the time-zone database, the helpers' start commands, Hermes's key, the paper doll, the
shipped `tools.example.json`), `setup_core.py` (`tests/test_setup_core.py`: a re-run after an interrupted first run,
damaged packages), the SenseVoice downloader, and `setup_tts.py` against a local stand-in for Hugging Face
(`tests/test_setup_tts.py`: public, private with and without a token, the CDN and a mirror never seeing the token,
resume, verify, repair, `--from-dir` (also a copy without the doll), banks.json, the paper doll at its own revision
and before it is pinned).
Replay tests that need MekaHime's archived corpora skip
themselves. GitHub Actions runs the same command on Windows with Python 3.11 (`.github/workflows/tests.yml`). The
tests resolve their temp folders, so an 8.3 short `TEMP` (a long Windows user name) is fine. On an install in use the
suite passes too: the tests that guard what ships (the neutral identity in `config/build_f.json`, the OpenRouter
preset in `config/llm_roles.json`, her persona copies) read git's index (`tests/_shipped.py`,
`tests/test_shipped_copies.py`), not the working-tree files that onboarding and the Setup page change. A failure
there is never a reason to touch the person's files.

## 11. Changing her safely

- **Persona**: edit the prose in Setup → Personality (or `brain/表层.txt`, `例子.txt`, `思考.txt`, `scenes.json`);
  the template self-check runs at boot and on save (`/api/build_f/status` → `template_check`). Keep `brain/defaults/`
  in sync only when you change the shipped persona on purpose.
- **Emotion vocabulary**: change only together with both `po_contract.py` copies, the parsers, `voice_adapter.py`,
  `speech/tts_emotion.py`, the Live2D banks and the tests.
- **A new revision of her Hugging Face repository** (maintainers): from a local copy of the new revision,
  `python scripts\setup_tts.py --write-manifest <folder> --voice-revision <commit>` rewrites
  `scripts/MekaHime_Amika.manifest.json` (`live2d/Host/` goes into its `paper_doll` block, kept as it is when the
  copy has none); set `VOICE_REV` in `scripts/setup_tts.py` to the same commit (the script refuses a mismatch), and
  `PAPER_DOLL_REV` too when the copy holds `live2d/Host/`; run the tests.
- **The paper doll's first upload** (maintainers, with MekaHime's token): its 70 files are already pinned (the
  manifest's `paper_doll` block). After uploading exactly those files to `live2d/Host/` of `MekaHime/MekaHime_Amika`,
  set `PAPER_DOLL_REV` to the full 40-character commit of that upload, and run
  `setup_tts.py --assets-only --skip-voice --skip-avatar --accept-voice-license` (with `HF_TOKEN` while the
  repository is private): `OK paper doll Host  … downloaded 70 file(s) … 70 files verified`. Optionally move
  everything to that one revision with `--write-manifest` (above).
- **Another agent avatar**: a model folder whose `banks.json` says `"role": "agent"` (docs/AVATAR.md); only the first
  such folder by name is used, so to use your own, delete `avatar/models/Host/` and run `setup_tts.py` with
  `--skip-paper-doll` from then on.
- **Another voice or server**: a speaker folder in `vendor/tts-engine/Speakers/` (`character.json`, weights,
  `references/emotions.json`) or any server speaking the contract in [docs/TTS.md](docs/TTS.md); point `tts_url` /
  `services.tts` at it.
- **Another Live2D model**: a folder in `avatar/models/` ([avatar/models/README.md](avatar/models/README.md),
  [docs/AVATAR.md](docs/AVATAR.md)).

Support: GitHub issues on https://github.com/zeekk0/OpenAmika, hi@mekahime.com, Discord https://discord.gg/mekahime.
