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

## 引导与重装支持

若任务涉及安装、重装、bootstrap、首次运行引导、Provider 配置、本地模型服务器搭建、Docker 引导、WSL 引导或首次运行失败的修复，请在执行命令或查看日志前，先阅读 `docs/onboarding-agent-checklist.md`。

遵守该清单的安全规则：

- 除非用户明确要求使用真实状态，否则试验时使用隔离的 `HERMES_HOME` 和 `HERMES_WEBUI_STATE_DIR`
- 未经明确授权，不得删除或覆盖真实的 `~/.hermes` 目录
- 不得打印 API 密钥、OAuth 令牌、Cookie、完整 `.env` 文件、完整 `auth.json` 文件或密码哈希
- 在推荐修复方案前，先收集非敏感的状态和日志证据

## Fork 集成边界（上游同步）

本仓库是上游 Hermes WebUI 的 Fork。自定义行为优先放在仓库内的 `integration/` 层，以保持 rebase 和上游拉取的低摩擦。

- 将 Fork 特有逻辑放在 `integration/` 下（处理器、配置、静态资源、`integration/tests/` 下的测试）。当 integration 层可以承载时，不要将自定义代码分散到核心模块中。
- 尽量减少对 `integration/` 之外的修改。只有在必须新增钩子或引入时才触碰上游**接缝文件**，且每次改动尽可能小：
  - `api/routes.py` — integration 的 GET/POST 分发、profiles 增强、静态映射、功能开关
  - `static/index.html` — integration 的脚本和 CSS
  - `static/panels.js` — 带守卫的 `HermesSkills` / `HermesProfiles` 调用
  - `server.py`、`requirements.txt`、`.env.example` — 仅在 integration 需要注册路由、新增依赖或新增环境变量时修改
- 实现 Fork 功能时，不要顺手重构、重新风格化或"清理"无关的上游代码。
- 若必须修改接缝文件，在 `integration/README.md` 和 `docs/hermes-external-integration.md` 中记录，以便合并冲突可预测。

在 `integration/` 之外编辑前，请先阅读 `integration/README.md` 和 `docs/hermes-external-integration.md`。

### Integration 维护约束

- **根目录 `CHANGELOG.md`**：以**上游 Hermes WebUI** 发布说明为主。集成外部服务、新增 `integration/` 内代码或改接缝文件时，**默认不要修改**根目录 `CHANGELOG.md`。Fork 侧说明写在 [`integration/CHANGELOG.md`](integration/CHANGELOG.md)；仅当用户明确要求、或该变更将并入上游 release 时再动根目录文件。
- **API 与 Swagger 同步**：凡新增或变更 **integration 暴露的 HTTP 接口**（含 `/api/skillhub/*`、integration 注册的其它路由、查询参数、请求/响应体、状态码），须在同一变更中更新 [`integration/swagger/openapi.json`](integration/swagger/openapi.json)，并与 [`integration/README.md`](integration/README.md) 路由表一致。可在本地打开 `/docs` 核对。上游原生 `/api/*` 若未纳入 integration Swagger，按上游惯例处理，不强行写入 integration 规范。

## 贡献风格

- 每个 PR 保持一个逻辑变更；将无关的重构或清理拆分到单独 PR。
- 编辑前阅读 `docs/CONTRACTS.md` 及所涉及子系统的契约/RFC。
- 优先使用现有的 Python + 原生 JavaScript 结构。未经充分论证并提供回滚方案，不得引入新依赖、构建工具、框架或长期运行的进程。
- 修改安装配置、引导流程、运行时行为、架构、测试指南或用户可见工作流时，同步更新文档。
- 新增或大幅改写 Markdown 文档时，默认尽量使用中文说明；若编辑既有英文上游文档、外部规范、API 字段说明或需要保持原文风格的段落，可沿用原语言并避免中英风格混杂。
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
