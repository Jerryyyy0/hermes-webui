# Hermes WebUI integration layer

Fork-specific features live here so upstream rebases stay predictable.

## Enable

```bash
export HERMES_INTEGRATION=1
export SKILLHUB_URL=http://127.0.0.1:8000   # optional; SkillHub market only (see docs/后端接口文档约束.md)
```

`HERMES_INTEGRATION=1` enables:

- **Profile enrich** — `GET /api/profiles` adds nested `info` from `info.json`. UI via `hermes_profiles.js` (logo picker, edit, create).
- **Profile assistant bubbles** — `GET /api/integration/assistant_bubbles?profile=<name>` returns fixed-order short assistant avatar bubbles from independent `<profile.path>/assistant_bubbles.json`; scheduled-task copy is computed live.
- **WebUI appearance** — `GET /api/integration/webui_appearance` reads `{HERMES_HOME}/webui-appearance/webui-appearance.json` as-is; `GET /api/integration/webui_appearance/file?path=` streams assets under that directory.
- **Cross-profile cron** — Cron Hub and grouped cron APIs across profiles.
- **SkillHub** — UI and `/api/skillhub/*` routes are active only when `SKILLHUB_URL` is also set.
- **Egress policy (iptables)** — gated API to apply iptables open/whitelist policies (see below). **Off by default**; requires `HERMES_EGRESS_POLICY_ENABLED=1`.
- **Knowledge base BFF** — `POST /api/integration/knowledge_base/*` routes are active only when `KNOWLEDGE_BASE_URL` is also set.
- **Notifications** — `/api/integration/notifications/*` for local notification storage (`notifications.db`) and knowledge base notification aggregation (from downstream `get_user_messages`).
- **Fixed Chinese session titles** — WebUI automatic and manual title generation always instruct the title model to return Simplified Chinese. This Fork policy does not read `auxiliary.title_generation.language`; model/provider/timeout routing remains unchanged. If the title model fails, WebUI keeps its existing topic-first local fallback behavior.
- **Chinese approval and clarify display copy** — WebUI approval cards prefer a fork-owned Chinese display description (`display_description_zh`) while keeping the Agent's canonical English `description` and `pattern_key(s)` unchanged for Smart Approval, hooks, and allowlists. When the Agent includes optional structured `tirith_findings` (`rule_id` / `severity` / `title` / `description` / `remediation` / `command_summary`), WebUI localizes known Tirith rules by `rule_id` (including `pipe-to-interpreter` command summaries and separate remediation hints); unknown rules keep severity plus original evidence. Legacy security-scan prose remains supported with English or Chinese envelopes (`Security scan — …` / `安全扫描：…`), `[HIGH]`/`[高]` severity, and inline `Safer:` remediation text. Non-Tirith pattern keys in `pattern_keys` are appended as localized reason text. Browser notifications prefer the same Chinese display copy. Agent and WebUI may land independently; structured findings require a matching Agent build.

If you use a local HTTP proxy (`HTTP_PROXY`, e.g. Clash), add the SkillHub host to `NO_PROXY` (or rely on `ensure_skillhub_no_proxy()` at server startup, which appends the hostname from `SKILLHUB_URL`). Without this, `/api/skillhub/*` may return 502 while `curl` to the same upstream works.

### API error logging

All JSON API responses with `status >= 400` (via `j()` / `bad()`) emit a timestamped, human-readable `[webui][api_error]` log line through the unified project logger. Unhandled handler exceptions use the same format with `source=unhandled`. The per-request access log may include an `error=...` summary when an API error was recorded.

Implementation: [`integration/project_logging/`](project_logging/) (`request.py` for access/API error/slow-request lines).

### Unified project logging

WebUI runtime logs use a single environment variable:

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERMES_WEBUI_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

Behavior notes:

- `INFO` enables per-request access logs, API error logs, startup messages, and stream diagnostics. Access lines use a compact `METHOD path -> status` format without the legacy `[webui][request]` tag.
- `DEBUG` additionally enables stream diagnostic debug fields and slow-request thread stacks.
- `WARNING` and above suppress informational request and stream diagnostics.

Log destinations are chosen by launch method, not by extra env vars:

- Direct `python server.py` with an interactive terminal: tee stdout/stderr to `{HERMES_WEBUI_STATE_DIR}/server-<port>.log` (size-rotated) plus `{HERMES_WEBUI_STATE_DIR}/server-<port>-crash.log` for native crash diagnostics.
- `bootstrap.py`, `ctl.sh`, or another supervisor that redirects stdout/stderr: use that captured log file only; WebUI skips its own tee when stderr is not interactive.

Implementation: [`integration/project_logging/`](project_logging/), [`integration/runtime_logging/`](runtime_logging/).

### Direct `server.py` runtime logs

When you run `python server.py` directly in an interactive terminal, WebUI persists stdout and stderr to a size-rotated log file while still teeing output to the terminal. The default paths are:

- Main log: `{HERMES_WEBUI_STATE_DIR}/server-<port>.log`
- Crash diagnostics: `{HERMES_WEBUI_STATE_DIR}/server-<port>-crash.log`

The main log includes startup messages, structured request logs, API error logs, and Python traceback output. Crash diagnostics use a separate append-only stream for `faulthandler` and crash-visibility hooks so native crash output remains stable even when the main log rotates.

Implementation: [`integration/runtime_logging/`](runtime_logging/).

### All-profile Gateway startup

WebUI startup ensures every visible Hermes Profile Gateway is running by default. The coordinator runs asynchronously, uses Hermes Agent's idempotent service lifecycle (`gateway start`), and never stops gateways when WebUI exits, so scheduled jobs remain independent of the WebUI process.

- Root/default uses `hermes gateway start`; named profiles use `hermes -p <name> gateway start`.
- Already-running gateways are skipped. Per-profile failures and timeouts are logged but never block the HTTP server.
- When the default profile enables `gateway.multiplex_profiles`, only the default Gateway is started because it serves all profiles.
- Isolated-profile deployments only enumerate and start their pinned profile.
- Native hosts and s6 containers enable the WebUI coordinator by default. Plain containers without s6 disable it automatically because Hermes `gateway start` is a successful no-op there; `run_container_services.sh` owns those Gateway processes instead. `HERMES_WEBUI_START_PROFILE_GATEWAYS=0` / `1` remains an explicit override when needed. Starting gateways can activate scheduled model calls and configured messaging/API platforms.
- Gateway lifecycle commands resolve a dependency-complete Hermes runtime: optional absolute `HERMES_WEBUI_HERMES_EXECUTABLE`, then the discovered Agent installation's own `venv` launcher/Python, then the running WebUI Python from the discovered Agent source root or WebUI repository root, and finally a verified `hermes` on `PATH`.
- The source-root runtimes support custom containers that already launch `python -m hermes_cli.main` from `/home/hermeswebui/.hermes/hermes-agent` or `/app`. A candidate is accepted only when a clean subprocess can import `hermes_cli.main`, `rich`, and `yaml` and run `--version`; lifecycle commands retain that verified working directory.
- Do not combine an arbitrary Python with an Agent checkout through `PYTHONPATH`; runtime probes remove `PYTHONPATH` and `PYTHONHOME` before validation.

Plain containers without s6/systemd must not use `gateway start`: Hermes treats that command as a successful no-op because the container runtime is expected to own the long-lived process. The project-owned [`run_container_services.sh`](gateway_startup/run_container_services.sh) handles **Profile Gateways only**: it discovers Profiles through `hermes_cli.profiles.list_profiles()`, runs each required `gateway run` process, writes per-Profile `logs/gateway.log`, honors multiplex mode, forwards termination, and exits if a managed Gateway exits. It does not locate, configure, or start WebUI.

Start it as a dedicated supervised process alongside WebUI. The outer container entrypoint or supervisor starts both processes; WebUI automatically skips its service-lifecycle coordinator in a plain non-s6 container:

```bash
"${HERMES_HOME%/}/hermes-webui/integration/gateway_startup/run_container_services.sh" &
cd "${HERMES_HOME%/}/hermes-webui"
exec /usr/local/bin/python3 server.py
```

The script defaults to `${HERMES_HOME}/hermes-agent`; `HERMES_WEBUI_AGENT_DIR` and `HERMES_PYTHON_PATH` override that runtime. Do not separately launch the default Gateway when using it.

Implementation: [`integration/gateway_startup/`](gateway_startup/), including the runtime boundary in [`integration/gateway_startup/runtime.py`](gateway_startup/runtime.py). `api/agent_cli_runtime.py` only preserves compatibility for existing core callers. The WebUI code root is derived from module locations, so local checkouts and containers can run `server.py` directly without a `~/.hermes/hermes-webui` symlink. Agent discovery prefers `HERMES_WEBUI_AGENT_DIR`, then `${HERMES_HOME}/hermes-agent`, then the existing `api.config` discovery fallbacks. The only startup seam is the asynchronous hook in `server.py`.

### Profile enrich (`info.json`)

When integration is enabled, `GET /api/profiles` enriches each entry with profile presentation metadata. Skill lists and memory contents are intentionally excluded; callers use the dedicated skills and memory APIs instead.

| Response field | Source |
|----------------|--------|
| `info` | `{profile.path}/info.json` (missing file → `{}`) |
| `info.welcome` | `info.json` → Profile 欢迎语；缺省或缺失时返回 `""` |
| `info.logo` | Data URI base64 in info.json; PNG/JPEG/GIF/WebP/SVG, invalid/over 10MB omitted from response |
| `info.pinned` | `info.json` → `pinned: true`（仅置顶时返回） |
| `info.pin_order` | `info.json` → 置顶组内排序（越小越靠前；仅置顶时返回） |

`GET /api/profiles` 列表顺序：置顶 profiles（按 `pin_order`，含 `default`）→ 未置顶 `default` → 其余字母序。

Write / update via UI or API:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/profile/logo-presets` | Built-in logo library (`?category=` optional) |
| POST | `/api/profile/info` | Update `info.json` (`display_name`, `description`, `welcome`, `logo_preset`, `logo_base64`, `remove_logo`) |
| POST | `/api/profile/pin` | Pin/unpin profile (`name`, `pinned`); writes `pinned` + `pin_order` to `info.json` (max 5, includes `default`) |

Example `info.json` — copy [`profiles/info.json.example`](profiles/info.json.example):

```json
{
  "display_name": "My Profile",
  "description": "Optional short description",
  "welcome": "哈喽，我是 My Profile，随时帮你处理日常工作。",
  "logo": "data:image/png;base64,..."
}
```

Optional pin fields (usually set via `POST /api/profile/pin`, not hand-edited): `pinned` (`true`), `pin_order` (integer; `1` = topmost among pinned; each new pin becomes `1` and shifts older pins down).

Regenerate built-in logo PNGs (network required): `python3 integration/scripts/fetch_profile_logos.py` (writes `assets/profile-logos/` from DiceBear + Noto Emoji; see `assets/profile-logos/LICENSES.md`).

Cron and Kanban profile pickers still show profile `name` only (by design).

### Profile assistant bubbles (`HERMES_INTEGRATION=1`)

`GET /api/integration/assistant_bubbles?profile=<name>` returns 8 short avatar bubble items in this fixed order: `assistant_intro → emotion → scheduled_task → emotion → memory → emotion → skill → emotion`. `profile` is required; the handler resolves the Profile only through `list_profiles_api()` and returns Chinese errors for missing or unknown values.

Bubble cache is stored only in `{profile.path}/assistant_bubbles.json`; it does not read or extend `info.json`. The file stores model-generated text and generation metadata (`fingerprint`, `generated_at`, `last_attempt_at`, `retry_after`) for `assistant_intro`, `memory`, `skill`, and `emotion`. Invalid, missing, or old-schema files are treated as cache misses: the API returns deterministic fallback copy immediately and queues self-healing generation.

The `skill` bubble’s `skills_count` and skill list use the same aggregation as SkillHub `scope=local_all` for that Profile: enabled **installed** hub skills ∪ **custom** skills under the Profile skills dir, excluding names in that Profile’s `skills.disabled`. When the SkillHub catalog is unavailable, generation falls back to local `.hub_installed` markers plus custom scans with the same merge/disable rules.

Generation is process-global and serial (`integration/assistant_bubbles/generation.py`). First fills for missing categories run continuously in priority order without a 5-minute gap; later fingerprint-driven regenerations are rate-limited to at least 5 minutes after the last attempt, while failures retry after 30 seconds. `emotion` also refreshes every 5 minutes even when its input fingerprint is unchanged. `scheduled_task` never calls a model and is never written back: each GET reads the target Profile cron state and replaces the dynamic slot in the response.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/assistant_bubbles?profile=<name>` | Return fixed-order assistant bubbles for one Profile; `profile` is required |

Example:

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/assistant_bubbles?profile=default'
```

Implementation: [`integration/assistant_bubbles/`](assistant_bubbles/). Route seam: `api/routes.py` only imports and delegates the GET handler.

### Cross-profile cron (`HERMES_INTEGRATION=1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/crons` | Active profile jobs; optional `?profile=` for a single named profile |
| GET | `/api/crons?all_profiles=1` | Grouped jobs: `{ profiles: [{ profile, jobs }] }` |
| GET | `/api/crons/recent?all_profiles=1&since=` | Cross-profile completions + session materialize |
| GET | `/api/crons/history`, `/run`, `/output` | Optional `?profile=` (storage and execution profile); history is `state.db` session-primary with optional output artifacts |
| GET | `/api/integration/crons/unread` | Cron Hub unread run counts across profiles |
| POST | `/api/integration/crons/create` | Create in the selected `profile` store and run under that same Profile (body: required `profile`, optional `skills`) |
| POST | `/api/integration/crons/update\|delete\|run\|pause\|resume` | Same; all require `profile` + `job_id`; delete also clears that job's materialized sessions, state rows, and `cron/output/<job_id>/` history |
| POST | `/api/integration/crons/unread/read` | Mark one Cron Hub job's current runs as read (`profile` + `job_id`) |

UI: **Cron Hub** rail/sidebar (`integrationCrons`) via `hermes_integration_crons.js`. Upstream **Tasks** panel unchanged (single active profile). Cron Hub creation requires one explicit Profile, stores the task there, runs it there, and can attach skills from that Profile. Agent tasks normally defer model resolution to Hermes Agent: job override, runtime environment, then the selected Profile's `config.yaml` default. Cron Hub's `POST /api/integration/crons/run` adds one execution-only fallback: when all three are empty, it uses the first model in that Profile's `/api/models` catalog order; if a named Profile still has no candidate, it falls back to the root/default Profile's current inference configuration or first catalog model. For an unpinned job, the current Profile model/provider (or the catalog fallback) is injected into the manual-run copy and its creation-time inference snapshots are cleared on that copy, so an explicit Cron Hub run accepts the current configuration without triggering Agent drift protection. Nothing is written to `jobs.json`; upstream `/api/crons/run` and automatic schedulers are unchanged. If discovery is empty or fails, the Agent records the existing asynchronous model-resolution failure. Global cron polling uses `all_profiles=1` when the flag is on.

When integration is enabled, one-shot schedules (`30m`, absolute datetimes, etc.) are kept in `jobs.json` after they finish (`enabled=false`, `state=completed`) instead of being auto-removed by Hermes Agent. Output history and Cron Hub listing remain available until you explicitly delete the job via `POST /api/integration/crons/delete` or upstream `POST /api/crons/delete`.

Cron execution history is database-primary: `GET /api/crons/history` lists matching `source=cron` sessions from the task execution Profile's `state.db`, including only fields persisted on the run's `sessions` row: timing, outcome, model, counters, and usage/cost aggregates when the schema provides them. A materialized cron session may continue chatting under the same `cron_*` session ID; `/api/session` shows the full growing conversation, while history deliberately does not derive preview or last activity from the mutable `messages` rows. Markdown files under `cron/output/<job_id>/` are attached as optional output artifacts using the matching Agent `cron/executions.db` terminal record. Every parseable output filename with no matching state row is automatically and idempotently backfilled as a minimal `source=cron` transcript and materialized for session-step viewing: Agent runs use the task prompt, while script-only (`no_agent`) runs use a safe task-name summary plus the script output. A zero-byte script artifact always receives a stable `cron_*` session; a matching execution supplies `cron_complete` or `cron_error`, and WebUI manual script runs may use `jobs.json` only when `last_run_at` exactly matches that one artifact. Scheduled and manual `no_agent` failures also reserve and persist a `cron_*` session as soon as `run_job` returns, before output saving, so an output-write failure cannot erase the run. When no terminal result is verifiable, the session is still persisted but `end_reason` remains empty and its text states that the outcome is unverified; no newer task state is applied to older artifacts. Failed script records return `end_reason=cron_error` and optional `error`, and their sidecars persist `cron_script_error` with Chinese script-execution copy and `脚本错误详情`; they never use model/Provider error categories. If `state.db` is temporarily unavailable, WebUI still materializes every valid output to a same-ID sidecar. For legacy jobs whose stored `profile` is empty, the request's owner `profile` selects the execution database instead of falling back to default.

Cron session materialization also persists stable Session Manifest artifact decisions. Hermes Agent's exact max-iteration summary request may be stored as a `role=user` message, but it continues the current cron invocation rather than opening a new Manifest turn. Before a same-ID WebUI follow-up starts, a cron-only prepare gate reconciles the immutable execution prefix, removes that internal boundary, stamps only real cron requests with contiguous keys (`turn:1`, `turn:2`, ...), saves those keys without changing session recency, and writes only missing prefix decisions. The gate runs before pending state, journal, SSE channel, or worker creation; a failed save/key check rejects the reply rather than creating a misaligned turn. A successful follow-up then uses the normal streaming artifact path and its next stable key; the final transcript is saved before that new turn's artifact or empty decision. Ordinary WebUI sessions and historical non-empty cron decisions are unchanged.

### 会话状态与已读游标（`HERMES_INTEGRATION=1`）

`GET /api/sessions` 的每个会话行增加：

- `status`：`error` / `in_progress` / `has_new_messages` / `ready`，优先级依次降低。
- `is_unread`：最新消息或当前异常是否晚于服务端已读游标。异常已读后仍为 `status=error`，同时 `is_unread=false`。

状态不直接持久化：运行状态来自 Session 的 stream/pending 字段，异常事实来自 Session sidecar 的 `last_error_at`，已读游标集中存放在 `{HERMES_WEBUI_STATE_DIR}/session_status.db`。全局表使用 `(profile, session_id)` 联合主键，避免 profile 间同名会话串读。老会话没有游标时默认已读；列表 GET 不创建或写入数据库。

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/integration/sessions/mark_read` | 将当前 Profile 的指定会话标记为已读；body 只接受必填 `session_id`，游标由服务端根据当前会话计算 |

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/sessions/mark_read' \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"abc-123"}'
```

### Egress policy (iptables)

This is an **operator feature** that can modify the host firewall. It is **disabled by default**.

Enable explicitly:

```bash
export HERMES_INTEGRATION=1
export HERMES_EGRESS_POLICY_ENABLED=1
```

Optional overrides:

| Env | Default | Meaning |
|-----|---------|---------|
| `HERMES_EGRESS_POLICY_RULES_PATH` | `/etc/iptables/rules.v4` | Where to write the iptables-restore rules file before applying |

API:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/egress/policy` | Return `iptables -L -n` output |
| POST | `/api/integration/egress/policy` | Apply `open` or `whitelist` policy via `iptables-restore` |

POST body:

- `{"policy_type":"open"}`
- `{"policy_type":"whitelist","allowed_ips":["1.2.3.4","10.0.0.0/24"],"include_request_ip":true}`

Notes:

- `include_request_ip` is **opt-in**. The server will **not** silently add the request IP to the allowlist.
- `allowed_ips` accepts IP or CIDR; invalid entries are rejected.

### Zhiling 身份（Control Plane 代理）

在用户容器内，调用方从 zhiling 登录回调取得 `access_token`（`#/auth/callback#token=...`）后，可经 WebUI 转发查询当前用户平台身份与 i智库同步信息。首次带 Bearer 成功查询后，身份缓存在 WebUI 进程内存，后续可无 Bearer 读取；`access_token` 仅存服务端，读缓存响应不返回 token。完整 HTTP 契约见 [`docs/integration-login-api.md`](../docs/integration-login-api.md)。

启用：

```bash
export HERMES_INTEGRATION=1
export ZHILING_CONTROL_PLANE_URL=http://zhiling-control-plane:13001
# 可选：非 JWT 或 JWT 无 exp 时的缓存 TTL（秒，默认 1800）
# export ZHILING_IDENTITY_CACHE_TTL_SECONDS=1800
```

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/webui_login` | 代理 `GET {ZHILING_CONTROL_PLANE_URL}/api/identity/lookup`；带 Bearer 时刷新内存缓存，无 Bearer 时读缓存 |

请求头：

| Header | 必填 | 说明 |
|--------|------|------|
| `Authorization` | 条件必填 | 刷新缓存时：`Bearer <access_token>`；读缓存时可省略 |

若 WebUI 启用了密码/Passkey 鉴权，调用方还需携带有效的 `hermes_session` Cookie（`credentials: include`）；`Authorization` 仅用于 zhiling token，不与 WebUI Cookie 混用。

示例（经 WebUI 代理）：

```bash
TOKEN='前端拿到的 access_token'

# 首次 / 刷新
curl -sS \
  -H "Authorization: Bearer ${TOKEN}" \
  http://127.0.0.1:8787/api/integration/webui_login

# 后续读缓存
curl -sS http://127.0.0.1:8787/api/integration/webui_login
```

成功时响应体与 Control Plane 一致（原样透传，含 `username`、`organization`、`ithinktank` 等字段）。无缓存时为 HTTP `401` 且 `error` 为 `not_registered` 或 `session_expired`；无效 Bearer token 通常为 HTTP `401` 且 body 含 `detail`；Control Plane 不可达时 WebUI 返回 `502` 且 `error` 为 `identity_lookup_failed`。`POST /api/integration/webui_logout` 会清空身份缓存。

#### 修改密码（Control Plane 代理）

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/integration/auth/password/change` | 代理 `{ZHILING_CONTROL_PLANE_URL}/api/auth/password/change`；凭 `username` + `old_password` 校验，不需要 Bearer |

请求体：`username`、`old_password`、`new_password`（均必填）。成功时原样透传 Control Plane JSON（含 `reauth_required: true`），并清理 WebUI `hermes_session` 与 Zhiling 身份缓存。Control Plane 不可达时 WebUI 返回 `502` 且 `error` 为 `password_change_failed`。

```bash
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -d '{"username":"test721","old_password":"Sgitg@2026","new_password":"NewPassword2027"}' \
  http://127.0.0.1:8787/api/integration/auth/password/change
```

### Zhiling 用户容器登出（auth-proxy 代理）

在用户容器 compose 网络内，WebUI 后端通过 `ZHILING_LOGOUT_API_URL`（auth-proxy 根地址，不含路径）调用固定接口 `POST /api/logout`，识别当前用户实例（`EXPECTED_USERNAME`），不要求转发浏览器 Cookie。

启用：

```bash
export HERMES_INTEGRATION=1
export ZHILING_LOGOUT_API_URL=http://auth-proxy:8080
```

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/integration/webui_logout` | 清理 WebUI `hermes_session`，POST `{}` 至 auth-proxy，原样返回 JSON（含 `casdoor_logout_url`、`login_url` 等）；`BACKEND=local` 时跳过 auth-proxy，直接返回 `200` + `{"status":"ok","login_url":"/"}` |
| GET | `/api/integration/webui_logout` | 返回 `405` + `method_not_allowed` |

示例（容器内或经 WebUI 代理）：

```bash
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8787/api/integration/webui_logout
```

推荐流程：调用方收到 JSON 后由前端或门户跳转 `casdoor_logout_url`；WebUI 内置 Sign Out（`POST /api/auth/logout`）不修改，Zhiling 部署可单独调用本接口或浏览器同源 `POST /api/logout`（auth-proxy）。

auth-proxy 不可达时 WebUI 返回 `502` 且 `error` 为 `zhiling_logout_failed`。

### Workspace 无会话文件（`HERMES_INTEGRATION=1`）

根目录固定为 **`HERMES_WEBUI_DEFAULT_WORKSPACE`**（`api.config.DEFAULT_WORKSPACE`），**不需要 `session_id`**。相对路径均相对该根目录。

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/workspace/files` | 平铺文件索引；`page`（默认 1）、`page_size`（默认 500，上限 5000）；可选子树 `path`（默认 `.`）；`q`（basename 包含搜索）、`type`（扩展名过滤，如 `.md`）、`sort`（`path`/`size`/`mtime`/`ctime`，默认 `path`）、`order`（`asc`/`desc`，默认 `desc`）；可选 `profile`（传入时仅返回该 profile 的 manifest 成果文件；不传则返回全部文件并对成果附加 `profile`）；可选 `refresh=1`（跳过服务端内存索引，强制重扫磁盘） |
| GET | `/api/integration/workspace/file` | 原始文件字节流（`path` 必填）；`Content-Type` 按扩展名；不设 `Content-Disposition` |
| POST | `/api/integration/workspace/file/delete` | 删除文件（`paths` 必填，字符串数组，至少 1 项）；仅删文件；响应 `deleted` / `failed`；全部失败 404 |

翻页：递增 `page` 直到响应 `has_more` 为 `false`。条目含 `ext`、`mime`、`mtime_ns`、`ctime_ns`（优先 birthtime，否则为 `st_ctime` 纳秒；`stat` 失败时为 `null`）。manifest 成果文件（会话 write 工具产出）附加可选 `profile`（`session.profile`）；非成果文件无该字段。

索引与读取默认排除系统/缓存垃圾文件（如 `.DS_Store`、`Thumbs.db`、`._*`），且不进入 `.git`、`node_modules`、`__pycache__` 等目录（与右侧 Workspace 文件树 #1793 规则一致）。

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?page=1&page_size=100'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?refresh=1'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?profile=ops'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?q=report&type=.md&sort=mtime&order=desc'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/file?path=README.md'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/file?path=assets/logo.png' -o logo.png
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/workspace/file/delete' \
  -H 'Content-Type: application/json' \
  -d '{"paths":["tmp/report.md","old.txt"]}'
```

UI（`HERMES_INTEGRATION=1`）：左侧 Rail / 移动顶栏 **Workspace 文件**（`integrationWorkspace`），`hermes_integration_workspace.js` + `hermes_integration_workspace.css`。左栏为平铺列表（服务端搜索/类型过滤/排序、分页「加载更多」、单行删除与多选批量删除、刷新），中间主区只读预览（文本 / Markdown / 图片 / PDF / HTML / 媒体）。删除后会刷新 session manifest，成果 chip 可标为已过期。与会话绑定的右侧 Workspace 面板（`/api/list` + `session_id`）并存。

### WebUI appearance（`HERMES_INTEGRATION=1`）

只读接口，根目录为进程级 **`{HERMES_HOME}/webui-appearance/`**（默认 `~/.hermes/webui-appearance/`）。配置 JSON 原样返回，不做 schema 校验或字段改写。文件预览 `path` 必须为该目录下的相对路径（如 `src/ly.jpg`）；禁止绝对路径与 `..` 逃逸。

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/webui_appearance` | 读取 `webui-appearance.json` 并原样返回 JSON |
| GET | `/api/integration/webui_appearance/file?path=` | 原始文件字节流（`path` 必填）；`Content-Type` 按扩展名；不设 `Content-Disposition` |

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/webui_appearance'
curl -sS 'http://127.0.0.1:8787/api/integration/webui_appearance/file?path=src/ly.jpg' -o ly.jpg
```

Implementation: [`integration/webui_appearance/`](webui_appearance/). Route seam: `api/routes.py` only imports and delegates the GET handler.

### 知识库 BFF 代理（`KNOWLEDGE_BASE_URL`）

在用户容器或门户内，前端将 `account` / `uuid` 放入请求体，经 WebUI 转发至下游知识库服务（`POST {KNOWLEDGE_BASE_URL}/knowledge_base/*`）。后端不调用 Zhiling identity lookup。

启用：

```bash
export HERMES_INTEGRATION=1
export KNOWLEDGE_BASE_URL=http://192.168.1.132:17861
```

| Method | Path | 下游 | 调用方必填 |
|--------|------|------|-----------|
| POST | `/api/integration/knowledge_base/list` | `list_ps_knowledge_bases` | `account`, `uuid`, `isPersonal` |
| POST | `/api/integration/knowledge_base/joined` | `user_joined_shkbs` | `account`, `uuid` |
| POST | `/api/integration/knowledge_base/create` | `create_ps_kb` | `account`, `uuid`, `showName`, `isPersonal` |
| POST | `/api/integration/knowledge_base/info` | `show_ps_kb_info` | `kbName` |
| POST | `/api/integration/knowledge_base/edit` | `edit_kb_information` | `kbName`, `showName` |
| POST | `/api/integration/knowledge_base/delete` | `delete_ps_kb` | `account`, `kbName` |
| POST | `/api/integration/knowledge_base/available` | `available_shkbs` | `account`, `uuid`, `page`, `size` |
| POST | `/api/integration/knowledge_base/apply_join` | `apply_join_shkb` | `account`, `uuid`, `kbName` |
| POST | `/api/integration/knowledge_base/members` | `get_user_inshkb` | `uuid`, `kbName`, `page`, `size` |
| POST | `/api/integration/knowledge_base/documents` | `list_knowledge_bases_details` | `kbName`, `page`, `size` |
| POST | `/api/integration/knowledge_base/upload_docs` | `upload_docs` | multipart 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/upload_artifacts` | `upload_docs`（编排；单文件最大 50 MiB、最多 20 个文件，不限制单次同步总大小） | `uuid`, `kbName`, `fileProperties`, `paths` |
| POST | `/api/integration/knowledge_base/update_docs` | `update_docs` | `kbName`, `fileNames`, `fileProperties` |
| POST | `/api/integration/knowledge_base/delete_docs` | `delete_docs` | `kbName`, `fileNames` |
| POST | `/api/integration/knowledge_base/show_pdf` | `show_pdf` | `kbName`, `fileName`（可选 `flag`） |
| POST | `/api/integration/knowledge_base/search_docs` | `search_docs` | `query`, `kbName`（可选 `topK`, `scoreThreshold`） |
| POST | `/api/integration/knowledge_base/search_docs_xcore` | `search_docs_xcore` | `query`, `kbNames`（非空数组；可选 `topK`, `scoreThreshold`） |
| POST | `/api/integration/knowledge_base/creater_handle_application` | `creater_handle_application` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/get_joinkb_applications` | `get_joinkb_applications` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/get_user_messages` | `get_user_messages` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/mark_message_read` | `mark_message_read` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/user_exit_shkb` | `user_exit_shkb` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/remove_from_myshkb` | `remove_from_myshkb` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/delete_readed_message` | `delete_readed_message` | 透传，无字段校验 |
| POST | `/api/integration/knowledge_base/download_doc` | `download_doc` | 透传，无字段校验（二进制或 JSON） |

成功时 HTTP 状态码与 JSON body **原样透传**下游响应（含 `code` / `msg` / `data` 包装）。`show_pdf` 与 `download_doc` 的下游请求超时为 180 秒；二者在下游返回文件时透传二进制。文档列表筛选请使用 `/documents`（下游同为 `list_knowledge_bases_details`）。BFF 自身错误：请求校验失败 HTTP 400；下游不可达 HTTP 502。

文档上传须两步串联：`upload_docs` 成功后再 `update_docs`。

```bash
curl -sS -X POST http://127.0.0.1:8787/api/integration/knowledge_base/list \
  -H "Content-Type: application/json" \
  -d '{"account":"admin","uuid":"aaaaaaaa0000aaaa0000aaaaaaaaaaaa","isPersonal":1}'
```

### 录制脚本（`HERMES_INTEGRATION=1`）

录制脚本使用前端生成的 `relate_name` 作为稳定资源 id。一个 `relate_name` 对应一份脚本 JSON 字符串和一份 CSV，落盘到 `{HERMES_WEBUI_STATE_DIR}/attachments/record_scripts/<relate_name>/`。

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/integration/record_scripts/save` | 首次保存脚本 JSON 字符串到 `script.json` |
| GET | `/api/integration/record_scripts/list` | 查询所有录制脚本，`script_json` 按字符串返回 |
| POST | `/api/integration/record_scripts/update` | 按 `relate_name` 整体替换 `script.json` |
| POST | `/api/integration/record_scripts/delete` | 删除 `script.json`，不清理关联 CSV |
| POST | `/api/integration/record_scripts_csv/upload` | multipart 上传并覆盖 `data.csv` |
| GET | `/api/integration/record_scripts_csv/download?relate_name=...` | 下载 `data.csv` 文件 |
| POST | `/api/integration/record_scripts_csv/delete` | 删除 `data.csv`，保留脚本 JSON |

```bash
curl -sS -X POST http://127.0.0.1:8787/api/integration/record_scripts/save \
  -H 'Content-Type: application/json' \
  -d '{"relate_name":"demo_flow","script_json":"{\"steps\":[]}"}'

curl -sS -F 'relate_name=demo_flow' -F 'file=@data.csv' \
  http://127.0.0.1:8787/api/integration/record_scripts_csv/upload
```

### 通知系统（`HERMES_INTEGRATION=1` + `KNOWLEDGE_BASE_URL`）

完整 API 文档：[`docs/integration-notifications-api.md`](../docs/integration-notifications-api.md)。

HTTP 接口仅服务知识库通知（`kb_apply`），从下游 `get_user_messages` 实时聚合。`{HERMES_WEBUI_STATE_DIR}/notifications.db` 表结构与 `store.py` 预留，当前 HTTP 层不读写。

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/notifications` | 知识库通知列表（`read_type`/`action_status` 过滤、游标分页；永久排除 `massType=3, state=2` 结果待处理） |

### 记忆统计（`HERMES_INTEGRATION=1`）

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/memory/stats` | 聚合统计：会话天数和列表、定时任务及列表、记忆文件名；可选 `?profile=` 指定 profile（默认当前活跃 profile） |
| GET | `/api/integration/notifications/summary` | 未读计数（铃铛角标；计数规则与列表排除一致） |
| POST | `/api/integration/notifications/read` | 标记已读（`kb:` ID 批量转发下游 `mark_message_read`） |
| POST | `/api/integration/notifications/delete` | 删除（`kb:` ID 批量转发下游 `delete_readed_message`） |

列表查询参数：
- `account` / `uuid` — 必填
- `read_type` — 下游 `readType`：`all`/`unread`/`seen`（默认 `all`）
- `action_status` — 业务状态过滤，逗号分隔（如 `pending`）
- `limit` — 每页条数（默认 20）
- `cursor` — 游标分页

审批（`approve`/`reject`/`ignore`）请调用 `POST /api/integration/knowledge_base/creater_handle_application`。

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/notifications?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa&limit=20'
curl -sS 'http://127.0.0.1:8787/api/integration/notifications?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa&read_type=unread&action_status=pending'
curl -sS 'http://127.0.0.1:8787/api/integration/notifications/summary?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa'
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/notifications/read' \
  -H 'Content-Type: application/json' \
  -d '{"ids":["kb:97"],"account":"admin","uuid":"aaaaaaaa0000aaaa0000aaaaaaaaaaaa"}'
```

## 维护约束

1. **不要改根目录 `CHANGELOG.md`** — 集成外部接口、接缝文件或本目录代码时，发布说明写在 [`CHANGELOG.md`](CHANGELOG.md)（本文件）。根目录 `CHANGELOG.md` 留给上游同步，除非 Maintainer 明确要求。
2. **改接口必更 Swagger** — 变更 `/api/skillhub/*` 或其它 integration 路由时，同步更新 [`swagger/openapi.json`](swagger/openapi.json)（`GET /docs`、`GET /api/openapi.json`）。参数、响应 envelope、错误码须与 `skills/handlers.py` 等实现一致。

## API routes

### Local skills (unchanged — `api/routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/skills` | Installed skills for active profile |
| GET | `/api/skills/content` | Local SKILL.md / linked files |
| POST | `/api/skills/save`, `/delete`, `/toggle` | Local CRUD |

### SkillHub proxy (`integration/skills/handlers.py` → `SKILLHUB_URL`)

| WebUI | Upstream |
|-------|----------|
| `GET /api/skillhub/skills` | `GET /api/skills` (`scope`, `q`, `category`, `page`, `page_size` — no `profile` upstream；`scope=local_all` 用上游目录 + 本地安装状态聚合，并支持 `profile` 按该 Profile 的 skills 目录过滤) |
| `GET /api/skillhub/categories` | `GET /api/skills/categories` |
| `GET /api/skillhub/detail?name=` | `GET /api/skills/{name}` |
| `GET /api/skillhub/content?name=` | 默认 `scope=auto`：仅本地 `{HERMES_HOME}/skills`（无则 404）；`scope=hub` 本地优先否则 `GET /api/skills/{name}/doc` |
| `GET /api/skillhub/structure?name=` | 同上（`structure`） |
| `GET /api/skillhub/file?name=&path=` | 同上（`file`） |
| `POST /api/skillhub/install` | download/doc → `shared_skills_dir`；有 `category` 时 `skills/<category>/<name>/`，否则平铺 `skills/<name>/` |
| `POST /api/skillhub/delete` | remove local skill from `shared_skills_dir`（市场安装与 custom；仅需 `HERMES_INTEGRATION=1`） |
| `GET /api/skillhub/download` | **仅本地**：zip 内 `{leaf}/` 目录 + `.skill-origin.json` sidecar；排除 `.hub_installed` 等元数据；`name` + 可选 `dir_name`；仅需 `HERMES_INTEGRATION=1` |
| `POST /api/skillhub/edit` | **仅本地** custom：更新已有技能的 `SKILL.md`（`name` + `content`，可选 `dir_name`）；市场安装不可编辑；仅需 `HERMES_INTEGRATION=1` |
| `POST /api/skillhub/upload` | **仅本地** custom：`.md` / 多技能 `.zip` 或 JSON → `shared_skills_dir`；可选 `category`、`dir_name`、`overwrite`（按 frontmatter `name` 覆盖全部 custom 副本）；响应 `{ skill_count, file_count, skills[] }` |
| `GET /api/skillhub/skills/no_self_improve` | 读取 `config.yaml` → `skills.no_self_improve` 名单（仅需 `HERMES_INTEGRATION=1`） |
| `POST /api/skillhub/skills/no_self_improve/toggle` | Custom 技能加锁/解锁 `{ name, locked, dir_name? }`；Hub 技能返回 403（仅需 `HERMES_INTEGRATION=1`） |
| `PUT /api/skillhub/skills/no_self_improve` | 全量替换 `skills.no_self_improve`（`{ names: string[] }`；Hub 名会在启动/install sync 补回） |

`GET /api/skillhub/skills` annotates `installed` from local skills dirs (hub/installed/not_installed still scan across profiles for stats/tab counts). Only `scope=local_all` honors the `profile` query (default `default`); other scopes ignore it. SkillHub routes do not use WebUI profile cookies for this list.

Query parameters:

| Param | Default | Meaning |
|-------|---------|---------|
| `scope` | `hub` | `hub` (market), `installed`, `not_installed` (`shared_skills_dir` / all-profiles annotate), `custom` (all-profiles custom scan), `local_all` (聚合指定 `profile` 下已启用的 installed hub + custom；自建优先按 `name` 去重，并排除该 profile `skills.disabled`) |
| `profile` | `default` | **仅 `scope=local_all` 生效**：只扫描该 Profile 的 skills 目录与其 `config.yaml` 的 `skills.disabled` |
| `category` | `""` (all) | Hub category filter; empty/`all` = all categories |
| `q` | — | Search (list only; tab stats ignore `q`) |
| `all` | — | Only `all=1` returns the full filtered list (ignores `page`/`page_size`; response `page=1`, `page_size=total`) |
| `page` / `page_size` | `1` / `20` | Pagination (ignored when `all=1`) |
| `sort` | `name` | `name` or `mtime` |
| `order` | `asc` | `asc` or `desc` |

List items always include aligned string fields (empty string when unset) and `mtime` (`null` when unset). Hub upstream `updated_at` is normalized into `mtime`. `hub` / `installed` / `not_installed` fetch the full catalog locally, apply `q` as substring match, sort, then paginate. `local_all` merges installed hub with custom under the requested `profile`, dedupes by `name` (custom wins), excludes that profile's `skills.disabled`, then applies the same filter/sort/paginate.

Response includes global `stats`: `{ hub, installed, not_installed, custom }` across **all** categories (unaffected by list `category` or `q`; only `skills`/`total` follow those filters).

## Layout

| Path | Role |
|------|------|
| `config.py` | `HERMES_INTEGRATION`, `SKILLHUB_URL`, `KNOWLEDGE_BASE_URL`, `ZHILING_CONTROL_PLANE_URL`, `ZHILING_LOGOUT_API_URL`, `ZHILING_IDENTITY_CACHE_TTL_SECONDS`, `skillhub_enabled()`, `knowledge_base_enabled()`, `identity_lookup_enabled()`, `zhiling_identity_cache_ttl_seconds()`, `zhiling_logout_enabled()` |
| `knowledge_base/` | `/api/integration/knowledge_base/*` → `{KNOWLEDGE_BASE_URL}/knowledge_base/*` |
| `notifications/` | `/api/integration/notifications/*` — 通知存储（`notifications.db`）与知识库消息聚合 |
| `webui_appearance/` | `GET /api/integration/webui_appearance` + `/file` — 读 `{HERMES_HOME}/webui-appearance/` 配置与资源 |
| `identity/` | `GET /api/integration/webui_login` → Control Plane `/api/identity/lookup`；进程内身份缓存（`session_store.py`） |
| `logout/` | `POST /api/integration/webui_logout` → `{ZHILING_LOGOUT_API_URL}/api/logout` |
| `skills/skillhub.py` | Upstream httpx client |
| `skills/handlers.py` | `/api/skillhub/*` HTTP handlers |
| `profiles/` | `GET /api/profiles` enrich; `POST /api/profile/info`; `GET /api/profile/logo-presets` |
| `scripts/fetch_profile_logos.py` | Generate built-in logo library |
| `assets/profile-logos/` | Logo preset PNGs + manifest |
| `assets/hermes_skillhub.js` | SkillHub sidebar panel |
| `assets/hermes_profiles.js` | Profiles panel enrich |
| `swagger/openapi.json` | Integration API 规范（`GET /api/openapi.json` 动态 `servers`） |
| `swagger/swagger_handler.py` | `GET /docs`、`GET /api/openapi.json` |
| `assets/swagger-ui/` | 离线 Swagger UI（`swagger-ui-dist@5.18.2`，经 `/static/integration/swagger-ui/*` 提供） |
| `frontend/` | Nginx 反代配置：用 `x_frontend/dist` 替代上游 `static/` UI（见 `frontend/README.md`） |
| `CHANGELOG.md` | Fork 集成层发布说明（勿写入根目录 CHANGELOG） |

## Upstream seam files (only these should conflict on rebase)

- `server.py` — starts the fork-owned all-profile Gateway coordinator asynchronously; lifecycle logic remains in `integration/gateway_startup/`
- `api/routes.py` — integration GET/POST dispatch, profiles enrich, static mapping, `__INTEGRATION_SKILLS__`, `__SKILLHUB_ENABLED__`; `GET /api/sessions` filters cron execution rows and injects session status fields when `HERMES_INTEGRATION=1`; chat start clears old error state and advances the user's own-message read cursor
- `api/streaming.py` — provider-error persistence records the current session error timestamp used by `integration/session_status/`
- `api/models.py` — Session sidecar persists the nullable `last_error_at` fact used to derive list status
- `api/profiles.py` — profile deletion best-effort removes that profile's global session read cursors
- `api/session_manifest.py` — after sidecar/state.db merge, cron-only GET normalization delegates to `integration.crons.hooks.normalize_cron_manifest_messages`; ordinary session turn extraction is unchanged
- `static/index.html` — integration scripts + SkillHub panel markup
- `static/panels.js` — `HermesProfiles` guard (`loadProfilesPanel`, `toggleProfileDropdown`, `renderProfileDetail`, `renderProfileForm`, `saveProfileForm`)
- `requirements.txt` — `httpx`
- `.env.example`

## Tests

```bash
pytest integration/tests/ tests/test_integration_seam.py -v
```
