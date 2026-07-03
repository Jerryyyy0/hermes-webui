# Hermes WebUI AI 助手指引

本文件是 AI 助手在本仓库工作时的统一入口。内容须保持项目相关且可安全公开。请勿在此记录个人机器配置、私有网络信息、凭据、令牌或仅限本地的工作流备注。

## 动手前必读

进行任何修改前，请先阅读：

1. `README.md`
2. `CONTRIBUTING.md`
3. `docs/CONTRACTS.md`
4. `CHANGELOG.md`

涉及架构、测试或环境搭建时，还需阅读对应参考文档：

- `ARCHITECTURE.md` — 设计约束与当前模块布局
- `TESTING.md` — 本地验证命令与手动测试指导
- `docs/onboarding.md` — 首次运行的引导行为
- `docs/troubleshooting.md` — 诊断流程
- `docs/rfcs/README.md` — 较大的 RFC 及状态/持久性契约

涉及 UI 或 UX 工作时，请在修改布局、交互流程、主题、聊天渲染或编辑器外壳前，先阅读 `docs/UIUX-GUIDE.md` 和 `DESIGN.md`。

## 关联代码库（Hermes Agent）

Hermes WebUI 依赖并与 **Hermes Agent** 协同运行。当任务涉及 Agent 运行时、CLI 行为、工具调用协议、状态目录结构、流式事件格式，或需在 Agent 侧排查/实现时，到本机关联仓库查阅：

- 路径：`/Users/wzq/Downloads/NLP-PyProject/hermes-agent`
- 默认以只读方式参考其实现与契约；除非用户明确要求，否则不在该仓库内提交变更。
- 跨仓库改动须分别说明 WebUI 与 Agent 两侧的影响与验证步骤。

## 引导与重装支持

若任务涉及安装、重装、bootstrap、首次运行引导、Provider 配置、本地模型服务器搭建、Docker 引导、WSL 引导或首次运行失败的修复，请在执行命令或查看日志前，先阅读 `docs/onboarding-agent-checklist.md`。

遵守该清单的安全规则：

- 除非用户明确要求使用真实状态，否则试验时使用隔离的 `HERMES_HOME` 和 `HERMES_WEBUI_STATE_DIR`
- 未经明确授权，不得删除或覆盖真实的 `~/.hermes` 目录
- 不得打印 API 密钥、OAuth 令牌、Cookie、完整 `.env` 文件、完整 `auth.json` 文件或密码哈希
- 在推荐修复方案前，先收集非敏感的状态和日志证据

## 贡献风格

- Keep one logical change per PR; split unrelated refactors or cleanup.
- Read `docs/CONTRACTS.md` and the linked contract/RFC for the touched
  subsystem before editing.
- For local pytest runs, use `./scripts/test.sh` instead of bare `python3`,
  `python -m pytest`, or `pytest`. The script creates/uses the repo `.venv`,
  pins execution to Python 3.11-3.13, and installs missing dev test dependencies.
  `HERMES_WEBUI_TEST_PYTHON` selects the supported base interpreter used to
  create or rebuild `.venv`; it must not install test dependencies into a
  system/Homebrew interpreter directly.
  If a direct pytest invocation reports an unsupported interpreter, rerun through
  `./scripts/test.sh` before debugging product code.
- Prefer the existing Python + vanilla JavaScript structure. Do not add
  dependencies, build tools, frameworks, or long-lived processes without clear
  justification and a rollback story.
- Update docs when changing setup, onboarding, runtime behavior, architecture,
  testing guidance, or user-facing workflows.
- Do not edit `CHANGELOG.md` in ordinary contributor PRs. The release workflow
  owns changelog updates through release commits. If a change is release-note
  worthy, include concise release-note wording in the PR body instead.
- For UI or UX changes, include before/after evidence and test relevant
  desktop, narrow, and mobile states.
- For behavior changes, add or update automated tests where practical and list
  the manual verification performed.
- For runtime, streaming, recovery, replay, compression, or sidebar metadata
  changes, name the state layer being mutated and prove the relevant invariant.
- For Docker build changes in `docker_init.bash`, mirror directory exclusions
  in both the `rsync` and `cp -a` paths — `/opt/hermes` may contain subdirectories
  with restricted permissions (e.g. `.playwright/`).

## Fork 集成边界（上游同步）

本仓库是上游 Hermes WebUI 的 Fork。自定义行为优先放在仓库内的 `integration/` 层，以保持 rebase 和上游拉取的低摩擦。

- 将 Fork 特有逻辑放在 `integration/` 下（处理器、配置、静态资源、`integration/tests/` 下的测试）。当 integration 层可以承载时，不要将自定义代码分散到核心模块中。
- **大文件改动须解耦到 `integration/`**：若目标文件本身已较大（如 `api/streaming.py`、`api/routes.py`、`static/messages.js` 等上游核心/接缝脚本），且本次改动会新增成块、可独立维护的逻辑（分类器、文案表、专用 helper、Fork 特有 UX 等），**默认把新增实现放在 `integration/`**（可建子包目录，如 `integration/chat_provider_errors/`），上游文件只保留必要的 import、薄封装或单行钩子。不要在大型上游文件里继续堆叠 Fork 专有实现。
- 尽量减少对 `integration/` 之外的修改。只有在必须新增钩子或引入时才触碰上游**接缝文件**，且每次改动尽可能小：
  - `api/routes.py` — integration 的 GET/POST 分发、profiles 增强、静态映射、功能开关
  - `static/index.html` — integration 的脚本和 CSS
  - `static/panels.js` — 带守卫的 `HermesSkills` / `HermesProfiles` 调用
  - `server.py`、`requirements.txt`、`.env.example` — 仅在 integration 需要注册路由、新增依赖或新增环境变量时修改
- 实现 Fork 功能时，不要顺手重构、重新风格化或"清理"无关的上游代码。
- 若必须修改接缝文件，在 `integration/README.md` 和 `docs/hermes-external-integration.md` 中记录，以便合并冲突可预测。

在 `integration/` 之外编辑前，请先阅读 `integration/README.md` 和 `docs/hermes-external-integration.md`。

### Integration 维护约束

- **大文件解耦检查清单**（与上文「大文件改动须解耦」配合使用）：
  - 新增模块放在 `integration/<feature>/`（含 `__init__.py` 与子模块拆分），测试放在 `integration/tests/` 或既有 `tests/` 中针对 integration 的用例。
  - 接缝文件中的改动应可一眼看出边界：import + 调用，而非复制业务逻辑。
  - 参考先例：聊天流中文 `apperror` 文案与分类在 [`integration/chat_provider_errors/`](integration/chat_provider_errors/)，`api/streaming.py` 仅 re-export。
- **根目录 `CHANGELOG.md`**：以**上游 Hermes WebUI** 发布说明为主。集成外部服务、新增 `integration/` 内代码或改接缝文件时，**默认不要修改**根目录 `CHANGELOG.md`。Fork 侧说明写在 [`integration/CHANGELOG.md`](integration/CHANGELOG.md)；仅当用户明确要求、或该变更将并入上游 release 时再动根目录文件。
- **API 与 Swagger 同步**：凡新增或变更 **integration 暴露的 HTTP 接口**（含 `/api/skillhub/*`、integration 注册的其它路由、查询参数、请求/响应体、状态码），须在同一变更中更新 [`integration/swagger/openapi.json`](integration/swagger/openapi.json)，并与 [`integration/README.md`](integration/README.md) 路由表一致。可在本地打开 `/docs` 核对。上游原生 `/api/*` 若未纳入 integration Swagger，按上游惯例处理，不强行写入 integration 规范。
- **Integration API 路径命名**：`integration/` 新增或变更的 HTTP **路径段**使用 **snake_case（下划线 `_`）**，不使用 kebab-case（连字符 `-`）。示例：`/api/skillhub/skills/no_self_improve`，而非 `no-self-improve`。JSON 字段名、YAML config 键、Python 模块名沿用各自惯例（可与路径不同）。
- **异常返回文案使用中文**：`integration/` **新增** HTTP 接口通过 `bad()`、`j(..., status=4xx/5xx)` 或其它方式返回给调用方的**错误/异常可读文案**（JSON 中的 `error`、`msg` 等字段）**须使用中文**。不要求改动既有上游 `/api/*` 的英文错误文案；下游外部服务原始错误仅用于日志或结构化透传字段时除外，但面向 WebUI 调用方展示或调试的错误说明仍应为中文。对应测试断言中的错误文案应与实现一致。

### Integration HTTP 处理器注意点

以下结论来自通知模块（`integration/notifications/`）与知识库 BFF 联调时的真实故障排查，适用于 `integration/` 下所有新增 HTTP handler。

**JSON 响应必须走 `j()` / `bad()`**

- `integration/` 新增 handler 写 JSON 响应时，**禁止**手动 `send_response` + `send_header("Content-Type")` + `end_headers()` + `wfile.write()`。
- WebUI 使用 HTTP/1.1 keep-alive（`ThreadingHTTPServer`）。响应若无 `Content-Length` 且无 `Transfer-Encoding: chunked`，客户端（curl、浏览器）无法判断 body 是否结束，会挂起直到读超时（常见约 30 秒）。
- 服务端 access log 中 `ms` 可能只有几十毫秒，但客户端仍长时间无响应——**不要据此误判为下游慢或 handler 逻辑慢**，先检查响应头是否缺少 `Content-Length`。
- 统一使用 [`api/helpers.py`](api/helpers.py) 的 `j()`（成功 JSON）和 `bad()`（错误 JSON）；知识库 BFF 的 `_respond()` 也是薄封装到 `j()`。
- 传给 `bad()` 的 `msg` 及错误 JSON 中的可读字段须为**中文**（见上文「异常返回文案使用中文」）。

**知识库下游契约（调用 `integration/knowledge_base/client.py` 时）**

- 下游标准响应信封为 `{code, msg, data}`，**消息列表在 `data` 字段**，不是 `messages`。
- `get_user_messages` **必填** `account`、`uuid`、`readType`（`all` / `unread` / `seen`）；缺 `readType` 时下游快速返回 500，但字段名错误会导致永远取不到数据。
- 用户标识由**调用方**在 query params（GET）或 request body（POST）传入；WebUI **不**从 Zhiling identity session 隐式推断 `account`/`uuid`（与知识库 BFF 透传模式一致）。
- 下游消息字段名与常见假设不同，须按实际 schema 映射，**禁止**用相似字段兜底（例如下游是 `massage`、`createTime`、`showName`、`state`，不是 `message`、`createdAt`、`kbName`、`isRead`）。当前字段无明确来源时默认为空值。

**认证与 curl 调试**

- WebUI 认证是 **cookie session**（`hermes_session`），不是 Bearer token。`Authorization` header 不参与普通 `/api/integration/*` 认证（仅 `/api/integration/webui_login` 登录代理会提取 Bearer 做 identity lookup）。
- 未设 `HERMES_WEBUI_PASSWORD` 时，`check_auth` 直接放行；curl 调 integration 接口**不需要** `Authorization`。
- 浏览器 POST 在开启密码认证时需带 `X-Hermes-CSRF-Token`；curl 无 `Origin`/`Referer` 时不走 CSRF 校验。

**排查「响应慢 + 空数据」时的顺序**

1. 用 `curl -D -` 看响应头是否有 `Content-Length`。
2. 对比同进程内知识库 BFF 透传（如 `POST /api/integration/knowledge_base/get_user_messages`）的耗时；BFF 快而 aggregation handler 慢，优先怀疑响应写法而非下游。
3. 直连 `KNOWLEDGE_BASE_URL` 核对请求体字段（尤其 `readType`）与响应 `data` 结构。
4. 看 server access log 的 `ms`：若 handler 已完成但客户端仍超时，几乎一定是 HTTP 响应格式问题。

### 简单接口文档

当用户要求「简单的接口文档」或表达类似意图时，回复**仅**包含：

1. **输入输出参数** — 路径、方法、请求体/query 字段、响应字段（含类型与是否必填）。
2. **调用示例** — 可直接复制的 `curl` 或等价示例。

不要额外展开架构背景、实现细节、排障步骤、变更历史或与调用无关的说明；用户未明确要求时不写长文档或新建 Markdown 文件。
- 用户可见的行为、配置、工作流或文档变更（应出现在发布说明中的）：上游/Core 变更更新根目录 `CHANGELOG.md`；**仅 integration/Fork 侧**变更更新 `integration/CHANGELOG.md`（见上文 Integration 维护约束）。
- UI 或 UX 变更须提供变更前后的对比截图，并测试桌面、窄屏和移动端状态。
- 行为变更须在可行时新增或更新自动化测试，并列出已执行的手动验证。
- 涉及运行时、流式传输、恢复、回放、压缩或侧边栏元数据变更时，需指明被修改的状态层并证明相关不变量成立。

### 会话 Inspector Manifest 约束

涉及会话待办、成果、参考（Session Inspector / manifest）时，请先阅读 [`docs/session-inspector-manifest.md`](docs/session-inspector-manifest.md)。

- Manifest 是会话活动的派生索引，不是 transcript、执行 journal 或 workspace 全量文件列表。
- References 只表示实际读取/打开的内容来源；搜索命中、目录列表和助手正文里提到的路径不默认算参考。

## 本地状态与敏感信息

Hermes WebUI 可读写真实的 Agent 状态、会话、工作区、凭据和定时任务数据。除非已确认当前活跃的状态目录，否则应将本地验证操作视为潜在破坏性操作。

试验时优先使用隔离的临时状态：

```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

请勿在此受版本控制的文件中记录私有机器配置。个人工作流细节请使用 git ignore 的本地笔记文件。
