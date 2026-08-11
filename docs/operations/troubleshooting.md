# Troubleshooting

Concrete diagnostic flows for the most common failure modes when running Hermes WebUI. Each entry has the symptom, the diagnostic commands you should run *before* opening an issue, and the fix that has worked for past reporters.

If your symptom isn't listed and the diagnostics don't narrow it down, file a bug at https://github.com/nesquena/hermes-webui/issues — include the relevant command output after redacting secrets, private paths, full `.env` files, full `auth.json` files, cookies, tokens, and password hashes.

---

## A Profile's scheduled jobs do not fire

**Symptom.** A Cron job remains enabled, but `next_run_at` is in the past and `last_run_at` does not advance.

WebUI starts every visible Profile Gateway by default. Verify Gateway and ticker state from an accessible directory:

```bash
cd ~
hermes profile list
hermes -p <profile> gateway status
hermes -p <profile> cron status
```

For the root/default Profile, omit `-p default` and use `hermes gateway status` / `hermes cron status`. Gateway logs are stored below the corresponding Profile home, typically `~/.hermes/logs/` for default and `~/.hermes/profiles/<profile>/logs/` for named Profiles.

If startup reports `No module named hermes_cli`, `rich`, or `yaml`, the command is using a Python environment that does not belong to the Hermes Agent installation. Do not install Agent dependencies into Anaconda or the WebUI venv as a workaround. Check the official launcher and its pinned interpreter:

```bash
command -v hermes
hermes --version
head -n 5 "$(command -v hermes)"
```

WebUI normally resolves the discovered Agent installation's own launcher/venv. Custom containers that launch Hermes modules from either the discovered Agent source root or the WebUI repository root can also use the running WebUI Python when both the dependency import probe and `python -m hermes_cli.main --version` succeed from that root. The verified working directory is retained for later Gateway lifecycle commands, while `PYTHONPATH` and `PYTHONHOME` remain cleared. WebUI and Agent locations are independent: run the actual WebUI checkout's `server.py` directly (for example `/app/server.py`). Agent discovery uses `HERMES_WEBUI_AGENT_DIR` first and `${HERMES_HOME}/hermes-agent` second, so a container with `HERMES_HOME=/home/hermeswebui/.hermes` needs no `~/.hermes/hermes-webui` symlink.

When multiple Hermes installations exist, set an absolute trusted launcher explicitly:

```bash
HERMES_WEBUI_HERMES_EXECUTABLE="$HOME/.local/bin/hermes"
```

If automatic startup was intentionally disabled, remove the override or set:

```bash
HERMES_WEBUI_START_PROFILE_GATEWAYS=1
```

Then restart WebUI. Set it to `0` when an external supervisor owns Gateway startup. If the default config enables `gateway.multiplex_profiles`, only the default Gateway should run; it serves the named Profiles too.

---

## "AIAgent not available -- check that hermes-agent is on sys.path"

**Symptom.** WebUI starts, shows the chat interface, but every chat request fails immediately with this error in the response or the server log. As of v0.51.6 the error includes a diagnostic block with the running Python interpreter, the relevant `sys.path` entries, and the most-common fix; on older versions the message is bare.

**Why it happens.** The WebUI imports the agent class at chat time via `from run_agent import AIAgent`. That import only succeeds if the running Python's `sys.path` contains either the hermes-agent checkout or a pip-installed copy of the agent. Three common failure modes:

1. **Agent installed but not on `sys.path`.** Most common. The agent is checked out somewhere (e.g. `~/Programmes/hermes-agent`), the WebUI was launched with a Python that doesn't know about it, and there's no `pip install -e .` linking the two.
2. **Symlink with a typo or wrong target.** A symlink to the agent looks correct on `ls`, but `readlink` resolves to a path that doesn't exist or doesn't contain `agent/__init__.py`.
3. **`HERMES_WEBUI_AGENT_DIR` set to the wrong directory.** Override env var beats auto-discovery and points at a directory that has no agent code.
4. **Agent installed as root, under the FHS layout.** When the Hermes Agent installer runs as root on Linux it places the agent at `/usr/local/lib/hermes-agent` (CLI linked into `/usr/local/bin`), not `~/.hermes/hermes-agent`. Older `bootstrap.py` didn't probe that path, so it built a WebUI-only `.venv` and failed at launch with **"Python environment cannot import both WebUI dependencies and Hermes Agent."** `git pull` to update the WebUI (current `bootstrap.py` auto-discovers the FHS layout and follows the `hermes` launcher to the agent), or set `HERMES_WEBUI_PYTHON=/usr/local/lib/hermes-agent/venv/bin/python` and relaunch.

### Step 1 — confirm the agent location

```bash
# If you have ~/hermes-agent (the default location):
ls -la ~/hermes-agent
readlink ~/hermes-agent          # if it's a symlink, where does it resolve?
ls ~/hermes-agent/agent/__init__.py 2>&1
```

The third command must succeed (the file must exist). If it fails, your symlink is broken or pointing at a directory that's missing the agent module — fix that first.

### Step 2 — confirm the WebUI is using the right Python

```bash
cd ~/hermes-webui && ./start.sh 2>&1 | grep -iE 'agent|python|hermes_webui_python' | head -20
```

The startup banner prints which Python and agent dir it resolved. If the agent dir is empty or the Python is the wrong one, set the override:

```bash
export HERMES_WEBUI_AGENT_DIR=/absolute/path/to/hermes-agent
export HERMES_WEBUI_PYTHON=/absolute/path/to/agent/venv/bin/python
./start.sh
```

### Step 3 — install the agent in editable mode

This is the most common fix and resolves the original issue #1695:

```bash
cd /path/to/hermes-agent          # the directory holding pyproject.toml + the agent/ module
pip install -e .                  # use the same python that runs the WebUI
```

Then restart the WebUI:

```bash
cd ~/hermes-webui
./start.sh
```

### Step 4 — verify by importing manually

If steps 1-3 still don't work, check whether the WebUI's Python can import the agent at all:

```bash
$HERMES_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')" 2>&1
```

(Replace `$HERMES_WEBUI_PYTHON` with the actual Python path from step 2 if the env var isn't set.) If this prints `ok`, the agent IS on `sys.path` for that Python — and the WebUI should work.

If this fails, `import run_agent` itself is broken — check that the agent's pyproject.toml lists `run_agent` as a top-level module or that the agent dir is on PYTHONPATH:

```bash
PYTHONPATH=/path/to/hermes-agent $HERMES_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')"
```

If adding PYTHONPATH fixes it, persist the path either via `pip install -e .` (preferred) or by setting `HERMES_WEBUI_AGENT_DIR` to that directory.

### When to file a bug

If after running steps 1-4 the import still fails *and* `pip install -e .` succeeded *and* `PYTHONPATH=... python -c "from run_agent import AIAgent"` succeeds — that's a real WebUI bug. File at https://github.com/nesquena/hermes-webui/issues with:

- The output of every command in steps 1-4
- The full diagnostic block printed by the WebUI's `ImportError` (v0.51.6+)
- Your OS, Python version, and how the agent was installed

---

## "Response interrupted." marker keeps saying "no agent output was recovered"

**Symptom.** After a live response stream stops before a turn completes (manual restart, OOM, crash, browser/SSE disconnect, lost worker bookkeeping, …), the affected chat shows an `**Response interrupted.**` marker. If the run-journal for that turn is already visible on disk, the marker says the partial output was recovered; if not, it preserves the user turn and says no agent output was recovered yet.

**Why.** Sidecar repair re-checks the run-journal after it detects a stale stream and uses the result as a one-shot signal. On WSL2 (9p / DrvFs) and on some network-backed setups, the run-journal `.jsonl` is written by the stopped worker but the WebUI process reads it through a page-cache state that has not yet seen those writes — recovery returns "empty" and the marker would otherwise be baked permanently. The fix introduces a *lazy* retry path: when sidecar repair cannot read visible output but knows the stream id, it stores a `_pending_journal_recovery` flag on the marker and re-attempts recovery from `get_session()` until the journal becomes readable (or the retry budget is exhausted).

**Interruption classes.** The WebUI now keeps the user-facing cases separate instead of implying every stale stream was a restart:

- **Browser/SSE connection interrupted** — the live browser `EventSource` transport dropped. The UI reports `Connection interrupted` and tries status/replay/session restore before showing the final browser-side notice. Chat and gateway SSE errors also POST a small sanitized diagnostic event to `/api/client-events/log` (source, session id, stream id, readyState, visibility, online state, path without query string) so server logs can distinguish browser transport loss from backend worker loss.
- **Lost worker bookkeeping** — the stream id is gone and the worker registry no longer has an active run. Recovery markers carry `interruption_cause: "lost_worker_bookkeeping"` and `/api/chat/stream/status` reports `terminal_state: "lost-worker-bookkeeping"` for non-terminal journals that are no longer active.
- **Stream/run split-brain** — the stream is gone but `ACTIVE_RUNS` still lists the worker. Recovery markers carry `interruption_cause: "stream_run_split_brain"` so the transcript says this is a bookkeeping split-brain rather than a restart.
- **Process crash/restart** — `SERVER_START_TIME` is newer than `pending_started_at`, meaning the WebUI process started after the turn began. Recovery markers carry `interruption_cause: "process_restart"` and explicitly say the process-start evidence points to a crash or restart.

**Diagnostic.**

The on-disk locations below assume the default `~/.hermes/webui` state directory. If you override it via `HERMES_WEBUI_STATE_DIR`, substitute that path for `~/.hermes/webui` in every step.

1. Identify the affected session id and stream id from the marker. The marker JSON lives at `~/.hermes/webui/sessions/<sid>.json`; after the fix it shows them on the `_journal_retry_stream_id` key. Pre-fix sessions only carry the legacy wording, with no retry meta.
2. Check whether the run-journal contains real events:
   ```bash
   ls -la ~/.hermes/webui/sessions/_run_journal/<sid>/<stream_id>.jsonl
   head -2 ~/.hermes/webui/sessions/_run_journal/<sid>/<stream_id>.jsonl
   ```
   If the file exists and contains `token` / `tool` events, the lazy-retry path will pick them up the next time the session is opened.

**Fix.** Reload the session in the browser. On the next `get_session()` call the marker is re-evaluated; if the journaled events are visible on disk the marker promotes to *"The partial output above was recovered from the run journal …"* wording and the journaled assistant text + tool cards land above the marker in chronological order. No manual sidecar editing is required.

**Trigger.** Sidebar metadata polling is intentionally not enough to run this self-heal. Requests such as `/api/session?messages=0&resolve_model=0` load the session with `metadata_only=True`, skip the full messages array, and therefore skip the lazy journal retry helper. Click/open the affected conversation so the message panel performs a full `messages=1` load; that full render is what re-checks the journal and can promote the marker.

**Caps.** The lazy retry path gives up after 12 failed attempts or 24h of wall-clock age, at which point the marker is demoted to a neutral *"Partial output may have been lost."* wording so the "reload to retry" prompt doesn't linger forever for genuinely lost journals.

**When to file a bug.** If, after the fix, you see the lazy-retry wording (*"Recovering the partial output from the run journal — reload this session to retry."*) but reloading the session never promotes it to the recovered wording even though the `.jsonl` clearly contains `token` events, capture the marker JSON and the run-journal file and file a bug.

---

## "Context compression exhausted" after a long-running turn

**Symptom.** A long-running session, often with many tool calls or a small
context-window model, ends with a `Context compression exhausted` error instead
of a final answer. The message includes a recovery action labeled `Start focused
continuation`.

**Why.** Automatic compression could not shrink the current conversation enough
to continue safely in the same model-facing context. The exhausted session is
terminal: sending a bare "continue", "go on", or "继续" would usually replay the
same oversized state and fail again, so the WebUI points the user to a focused
linked continuation instead.

**Diagnostic.**

1. Open the session JSON under your WebUI state directory, for example:
   ```bash
   jq '.recommended_recovery_action, .compression_recovery' \
     ~/.hermes/webui/sessions/<session_id>.json
   ```
2. A recoverable exhausted turn should report:
   - `recommended_recovery_action: "start_focused_continuation"`
   - `compression_recovery.terminal_state: "compression_exhausted"`
   - the final assistant error message carrying `_compressionRecovery`

**Fix.** Use the `Start focused continuation` action in the exhausted message.
The new linked session preserves the workspace, model, profile, project, and
toolset lane, but intentionally starts with an empty model-facing transcript so
the oversized exhausted tail is not replayed. After the new session opens,
describe the next narrow task explicitly instead of sending a bare continuation.

**When to file a bug.** File a bug if the exhausted message has no recovery
action, the action creates a session with the old oversized context/messages
replayed into the model-facing transcript, or a bare "continue" starts another
turn in the exhausted session instead of being blocked with recovery guidance.

---

## Session detail API occasionally loads slowly

**Symptom.** `GET /api/session?session_id=...&messages=1` occasionally takes
hundreds of milliseconds or several seconds, while the session list remains
responsive.

**Diagnostic.** Direct `python server.py` keeps detailed timing disabled by
default. Restart WebUI with the following temporary environment variable:

```bash
HERMES_DEBUG_SESSION_TIMING=1 ./start.sh
```

Each successful full session response emits a `[SESSION_TIMING]` record with
`session_resolve`, `message_source`, `model_resolve`, `compact`, `redact`, and
`json_write` durations. The `stages` field splits the request into
`session.resolve`, `session.message_source`, `session.model_resolve`,
`session.message_projection`, `session.redact`, and `session.response_write`.
Requests taking at least two seconds keep the existing `[SLOW]` prefix even
when this flag is enabled.

**Interpretation.** `session_resolve` covers full session lookup and any
read-side recovery; `message_source` covers CLI/state.db message retrieval;
`compact` and `session.message_projection` cover sidecar lineage loading,
message merge/filtering, display-window construction, and response payload
assembly. `resolve_model=0` reads only persisted context metadata; a missing
`context_length` returns `0` (unknown) without probing the model endpoint.
The deferred `resolve_model=1` request may refresh that metadata. To disable
per-request timing after collecting the affected request, restart with
`HERMES_DEBUG_SESSION_TIMING=0`.

---

## Installed PWA opens to a blank screen after an update

**Symptom.** The installed PWA or home-screen app opens to a blank screen after a WebUI update, while the same URL often works again in a normal browser tab.

**Why.** Reverse proxies are supported, but proxy basic auth can challenge the same-origin `sw.js`, manifest, or versioned `static/*` fetches the installed app needs while its service worker updates the shell.

**Diagnostic.**

1. Open the same WebUI URL in a regular browser tab and confirm whether it loads there.
2. Check reverse-proxy logs for `401` responses on `/sw.js`, `/manifest.json`, or versioned `/static/*` assets during the update.
3. Temporarily remove proxy basic auth and use WebUI's built-in password. If the blank screen stops after the next update, the proxy auth challenge was the trigger.

**Fix.** Prefer WebUI's own password for installed PWAs. If you keep proxy basic auth, configure it so the same-origin service-worker and shell update fetches can complete. If the installed shell is already blank, clear site data for the Hermes origin, then reopen or reinstall the PWA after that site-scoped cleanup.

**When to file a bug.** File a WebUI bug if the blank screen still reproduces without proxy basic auth, or after the proxy allows the same-origin service-worker and shell update fetches through.

---

## "Hermes Agent was updated while Hermes WebUI was running"

**Symptom.** An action that uses the in-process Agent runtime stops with a message telling you to restart Hermes WebUI. This can happen after `hermes update`, a Git checkout/pull in the Agent source tree, or another tool updates Hermes Agent without restarting the already-running WebUI backend.

**Why.** WebUI currently imports `run_agent.AIAgent` into its long-lived Python process. Python keeps imported modules in memory. Continuing after a known Agent Git revision changes could combine cached modules from the old revision with source read from the new revision, producing misleading `ImportError`s or inconsistent runtime state. For local Agent-backed chat, WebUI therefore returns a retryable `409 agent_runtime_stale` before claiming or mutating session state instead of attempting a partial in-process reload. Gateway-backed chat runs in the gateway process and is not blocked by this WebUI-local check. Non-Git Agent installs preserve their existing behavior because there is no revision identity to compare.

**Diagnostic.** Compare the running WebUI process start time with the Agent checkout revision and recent update history. If the Agent was updated after WebUI started, restart WebUI before investigating individual missing-symbol errors.

**Fix.** Restart using the same launch method that started WebUI:

```bash
./ctl.sh restart
# Or, for a user systemd service:
systemctl --user restart hermes-webui.service
```

If you launched `python3 bootstrap.py` in the foreground, stop it with Ctrl-C and start it again. Restarting the whole computer or WSL is not required when restarting the WebUI backend succeeds.

**When to file a bug.** File a WebUI bug if the restart-required message appears even though the Agent revision did not change, or if a clean WebUI restart still produces the same import error. Include the WebUI launch method, WebUI revision, Agent revision, and the sanitized error text.

---

## Other troubleshooting

This document grows over time. If a recurring failure mode isn't covered here yet, add it via PR. The format for each entry: **Symptom → Why → Diagnostic commands → Fix → When to file a bug**.

Related references:

- [`docs/operations/supervisor.md`](supervisor.md) — process-supervisor setup (launchd, systemd, supervisord, runit/s6) including the bootstrap supervisor-foreground flag.
- [`docs/guides/docker.md`](docker.md) — Docker compose setup, common failure modes, bind-mount migration.
- [`docs/guides/wsl-autostart.md`](wsl-autostart.md) — WSL2 auto-start at login on Windows.
- [`docs/EXTENSIONS.md`](EXTENSIONS.md) — WebUI extension injection, security model, examples.
