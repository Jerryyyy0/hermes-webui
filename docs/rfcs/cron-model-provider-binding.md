# Cron 推理路由完整绑定

- **Status:** Proposed
- **Author:** Hermes WebUI maintainers
- **Created:** 2026-08-12

## 问题

定时任务是脱离浏览器和交互式会话的持久化执行单元。它不能只保存模型名，再在未来某次触发时用当时的全局默认 Provider 补齐路由；模型名和 Provider 是同一个用户意图的一部分。

本次事故的任务在执行时携带：

```text
job.model    = deepseek-v4-flash
job.provider = null
config / HERMES_INFERENCE_PROVIDER = custom
custom base_url = http://192.168.1.137:10251/v1
```

调度器先采用 `job.model`，随后将空 `job.provider` 交给 runtime provider resolver。resolver 按其正常优先级选中了全局 `custom`，于是向本地一体机网关发送了 `deepseek-v4-flash` 请求。这不是 DeepSeek API Key、新闻任务、网页搜索或输出写入的失败。

当前的 `provider_snapshot` 与 `model_snapshot` 是创建/更新时的审计快照，不参与出站路由；它们也可能来自旧配置，不能被当作可执行的绑定。因而如下状态是破损且危险的：

```text
provider_snapshot = custom
model_snapshot    = qwen3.8-max-preview
model             = deepseek-v4-flash
provider          = null
```

它会把一个看似明确的 DeepSeek 模型与后来恰好处于默认位置的任意 Provider 拼接。此前可能表现为模型不存在（404），本次则因本地网关连接失败表现为 `RuntimeError: Connection error.`。

## 目标

1. Cron 的每次出站 LLM 调用都有一个完整、可审计、不可拆开的推理绑定：`provider + model + resolved base_url + resolved api_mode`。
2. 已显式选择模型的 Cron 任务绝不因 `provider` 缺失而静默落到全局 `custom`、`HERMES_INFERENCE_PROVIDER` 或任何其他 Provider。
3. 自动调度、上游 Tasks 的手动运行、Cron Hub 的手动运行、重试和 fallback 使用同一条解析规则。
4. 新任务和更新任务在写入 `jobs.json` 前拒绝不完整的固定绑定；历史不完整任务在发网络请求前 fail closed，并给出可操作的修复信息。
5. 保留“未固定任务跟随当前 Profile 默认推理配置”的能力，但它必须始终把当前配置作为完整路由解析，而不是混合历史模型和当前 Provider。
6. 不打印 API Key、Authorization header 或完整 provider 配置；诊断记录只包含已脱敏的路由元数据。

## 非目标

- 不根据模型名称对所有历史任务进行盲目批量改写。相同模型名可能由自定义网关、转售商或原生厂商提供，字符串本身不是充分授权。
- 不改变普通聊天会话的模型选择语义。
- 不把 `provider_snapshot` / `model_snapshot` 升格为运行时真相。
- 不将 `api_mode` 作为用户可自由填写的 Cron 字段持久化。它是 Provider 与目标模型共同决定的运行时派生值。
- 不通过修改全局 `HERMES_INFERENCE_PROVIDER` 修复单个任务；那会改变无关任务和交互式会话。

## 术语与权威来源

| 术语 | 含义 | 权威来源 |
| --- | --- | --- |
| 未固定任务（unpinned） | 没有任何 per-job 推理覆盖，跟随任务所属 Profile 当前默认配置 | Profile `config.yaml` 与 runtime provider resolver |
| 固定任务（pinned） | 任务明确选择了一组 Provider 和 model；可选自定义 base URL 是该组的一部分 | `cron/jobs.json` 的 job inference binding |
| 推理绑定 | 任务意图的 `provider`、`model`、可选 `base_url`，以及执行时解析得到的 `api_mode` | job 定义；执行期补齐派生字段 |
| 审计快照 | 创建或更新时观察到的 Provider/model，仅用于解释历史 | `provider_snapshot`、`model_snapshot` |
| 实际路由 | 实际传入 `AIAgent` 的 provider、model、base_url、api_mode | Agent scheduler 的一次 execution record |

`provider_snapshot` 和 `model_snapshot` 永远不能覆盖 `provider`、`model`、`base_url`，也不能在这三者不完整时填洞。任务意图与审计历史必须分离。

## 目标状态与不变量

Cron job 的推理字段只允许以下两种有效状态：

| job 定义 | 含义 | 调度时行为 |
| --- | --- | --- |
| `provider`、`model`、`base_url` 均为空 | 未固定 | 从当前 Profile 的配置整体解析 provider、model、base_url、api_mode |
| `provider` 和 `model` 均非空；`base_url` 可为空 | 固定 | 以该 Provider + model 解析 runtime；若有 base_url，必须验证它属于该 Provider |

以下状态无效：只写 model、只写 provider、只写 base_url，或 base_url 与 provider 不匹配。它们不能发起网络请求。

执行期的单一权威值是 `ResolvedCronInferenceBinding`：

```python
@dataclass(frozen=True)
class ResolvedCronInferenceBinding:
    origin: Literal[
        "job_pin",
        "profile_default",
        "profile_default_fallback",
    ]
    provider: str
    model: str
    base_url: str
    api_mode: str
```

从解析到 `AIAgent(...)` 构造、重试、execution ledger、日志和 WebUI 历史展示，都必须使用这一对象；不得在任一步重新以 `job.provider`、环境变量或全局默认拼接路由。

### DeepSeek 的正确绑定

当前 Hermes Agent Provider registry 将 DeepSeek 定义为 OpenAI Chat 兼容 Provider。该任务应保存为：

```text
provider = deepseek
model    = deepseek-v4-flash
base_url = https://api.deepseek.com     # 可由 deepseek runtime 默认解析
```

执行期应由 runtime provider resolver 得到 `api_mode=chat_completions`，即调用 `/v1/chat/completions`。不能把 `api_mode=responses` 写进任务来“修复”该问题：当前 DeepSeek provider 并不以 Responses API 作为它的默认 transport。

## 方案

### 1. 在 Agent scheduler 建立唯一解析入口

在 Hermes Agent 新增一个 Cron 专用的纯函数/模块，例如 `cron/inference_binding.py`：

```text
job definition + active Profile config
        -> validate job axes
        -> resolve a ResolvedCronInferenceBinding
        -> AIAgent and execution record
```

建议入口：

```python
resolve_cron_inference_binding(job, config) -> ResolvedCronInferenceBinding
```

行为如下：

1. 标准化 `provider`、`model` 与 `base_url`（去首尾空白；base URL 去末尾 `/`）。
2. 三者全空时，读取当前 execution Profile 的 `config.yaml`，用 `resolve_runtime_provider(requested=None, target_model=<current default>)` 解析完整默认绑定。此路径保留现有“任务跟随当前配置”的语义。
3. `provider` 和 `model` 均存在时，用 `resolve_runtime_provider(requested=provider, target_model=model, explicit_base_url=base_url)` 解析固定绑定，并复用现有 provider/base_url 成对安全校验。
4. 任一固定轴缺失时，在调用 resolver 之前抛出专门的 `CronInferenceBindingError`。错误应指出 job ID、缺失字段和修复方式；不能尝试全局 Provider、环境变量或 snapshot。
5. 返回值成为 `AIAgent(model=..., provider=..., base_url=..., api_mode=...)` 的唯一输入。

现有 `cron/scheduler.py:run_job()` 中“先用 `job.model` 解析模型，再以 `job.provider` 调 runtime resolver”的两步逻辑应被替换。特别是禁止这类组合：

```text
model = job.model
runtime = resolve_runtime_provider(requested=job.provider)
```

因为它允许两个不同配置世代的值被混用。

### 2. 固定任务默认回退到当前 Profile 默认绑定

固定绑定发生异常时，先重试同一 `ResolvedCronInferenceBinding`。主路由的重试耗尽后，任务默认按当前 Profile 的完整默认推理配置额外执行一次，无需任务级开关。

该策略适合“优先使用指定模型，但任务可用性优先”的场景。例如任务主路由为 DeepSeek，Profile 默认路由为本地 custom + Qwen；DeepSeek 网络失败后，任务可用 Profile 当前完整默认路由继续执行。

这不是模型级降级。fallback 必须重新解析当前 Profile 的完整 `provider + model + base_url + api_mode`，不能保留主路由的 `deepseek-v4-flash` 后只替换 Provider。

执行顺序固定如下：

```text
固定主绑定的同路由重试
  -> 主路由耗尽且属于可恢复异常
  -> 解析当前 Profile 默认完整绑定
  -> 默认绑定只执行一次
```

触发 Profile 默认 fallback 的可恢复错误限于连接失败、超时、429/限流和上游 5xx。认证失败、模型不存在、任务绑定不完整、Provider/base URL 校验失败和本地脚本/工具失败都不得触发。

若解析出的默认绑定与主绑定规范化后完全相同，则不做重复 fallback。若默认配置本身无法解析为完整 binding，记录 fallback 失败并保留主错误，不允许使用半成品配置出网。

当前方案只定义一个固定的 fallback：当前 Profile 的完整默认 binding。自定义多级 fallback 链不属于本次实现范围，避免多条回退路线造成成本、数据路径和排障语义不明确。

### 3. 写入时保存完整意图，不保存猜测

`cron.jobs.create_job()`、`update_job()`、`tools.cronjob_tools`、上游 `/api/crons/create|update` 和 Cron Hub `/api/integration/crons/create|update` 都必须共用 job schema normalizer。

- 表单/模型选择器必须提交 `provider` 与 `model`；如果前端已知自定义 endpoint，同时提交 base URL 或可反查的命名 custom Provider identity。
- API 请求中的不完整固定绑定返回 400，错误文案使用中文。例如：`已选择模型“deepseek-v4-flash”，但未指定 Provider；请重新选择完整的 Provider 和模型。`
- “跟随当前 Profile 默认模型”明确发送三项均空，而不是只清除其中一个字段。
- 更新模型时，服务端将它视为一次完整的模型选择：要求同一请求包含对应 Provider，或由一个受版本控制、可验证的选择 token 解析后原子写入二者。
- `base_url` 是可选覆盖，只有存在 `provider + model` 时合法；它继续接受既有的 anti-exfiltration 校验。

`provider_snapshot` / `model_snapshot` 可继续为未固定任务保存创建时审计信息，但不得被写入逻辑、恢复逻辑或 UI 误展示为“当前绑定”。建议在 job JSON 的注释/文档中明确其 `audit_only` 角色。

### 4. WebUI 只传递完整绑定，不重复实现运行时路由

WebUI 的自动 Cron 调度最终由 Agent 执行，根修复必须在 Agent。WebUI 仍需处理两个入口：

- 上游 Tasks 的 create/update/run；
- `integration/crons/` 的 Cron Hub create/update/manual run。

Cron Hub 的 `prepare_cron_hub_execution_job()` 目前只在 model 和 provider 都为空时补齐手工执行副本。它必须额外识别 `model` 非空而 provider 为空的历史破损状态：

1. 不能原样放行给 scheduler；
2. 不能从全局 custom 静默补齐；
3. 若请求明确提供完整修复绑定，可用该绑定创建内存执行副本；
4. 否则拒绝手工运行并返回与自动调度一致的修复错误。

WebUI 不维护第二套“根据模型名猜 Provider”的常规规则。模型目录可能重叠：例如自定义网关、OpenCode 类转售商和原生厂商可同时提供相同字符串。只有 Agent 的版本化 resolver 才能将 Provider catalog、direct alias、Profile 配置和 base URL 验证合成一个可执行结论。

### 5. 历史任务采用 fail-closed 的修复流程

部署前已写入的任务不能假定其 model 名属于某个原生 Provider。迁移分为扫描、修复、验证三步。

#### 扫描与分类

在 Agent 提供只读命令/API，例如 `hermes cron audit-inference`，逐个 Profile 扫描 `jobs.json`，输出：

| 分类 | 条件 | 自动调度行为 |
| --- | --- | --- |
| `valid_unpinned` | 三项均空 | 允许按当前 Profile 默认配置运行 |
| `valid_pinned` | provider + model 完整，base URL 合法 | 允许按保存绑定运行 |
| `incomplete_binding` | 任一固定轴存在但 provider/model 不完整 | 拒绝运行，不发网络请求 |
| `unsafe_base_url` | provider/base URL 未通过校验 | 拒绝运行，不发网络请求 |
| `legacy_snapshot_conflict` | snapshot 与 job 意图冲突 | 仅告警；按前两类判断，不以 snapshot 路由 |

扫描结果必须只显示 Provider、model、已规范化 endpoint host/path 和 job ID，不显示凭据。

#### 修复

对已确认应使用 DeepSeek 的任务，运维人员显式更新：

```text
provider = deepseek
model    = deepseek-v4-flash
base_url = https://api.deepseek.com
```

对于本次任务 `5c6baaaf679c`，更新后应清理或重新计算 audit snapshot，但这只是为了消除误导性历史；真正的修复是 `provider`、`model`、`base_url` 三项一致。

不允许迁移器仅因名字包含 `deepseek` 就批量修改任务。只有存在可验证的旧选择 provenance（例如旧版本保存的完整 Provider、可信的 UI selection record，或管理员提供的映射文件）时，迁移才能自动写回；其余任务生成待人工确认清单。

#### 运行时保护

在历史修复完成前，scheduler 对 `incomplete_binding` 写入本次 execution 的明确失败状态，例如：

```text
CronInferenceBindingError: 任务 5c6baaaf679c 的模型为 deepseek-v4-flash，
但没有保存 Provider。为防止请求被路由到默认 custom Provider，任务未执行。
请在任务设置中重新选择 DeepSeek / deepseek-v4-flash。
```

该失败不触发 Profile 默认 fallback，也不应消耗网络重试次数。只有完整主绑定发生可恢复的上游异常，且任务明确开启 fallback 时，才可重新解析当前 Profile 默认 binding。

### 6. 记录实际路由，便于审计

Agent 在每次 execution 的结构化日志和 execution ledger 写入以下非敏感字段：

```json
{
  "inference_origin": "job_pin",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "api_mode": "chat_completions",
  "binding_validation": "valid"
}
```

日志不得包含 API Key、请求 body、Authorization header 或完整 `.env`/`auth.json`。WebUI 的 Cron history 可展示 provider、model、endpoint host 与 binding origin；完整 base URL 是否展示应遵循已有敏感配置展示策略。

## 数据模型与兼容性

推荐继续使用当前字段，避免无必要 schema 扩张：

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "provider_snapshot": null,
  "model_snapshot": null
}
```

`api_mode` 不写入 `jobs.json`：Provider registry、Provider 配置和 `target_model` 变化时，旧的持久化 API mode 反而可能再次造成协议错配。每次主路由或 Profile 默认 fallback 都由 `resolve_runtime_provider(..., target_model=model)` 派生它，并写入 execution ledger 供审计。

兼容策略：

- 新版本读取旧任务不立即改写文件。
- 有效未固定旧任务继续按当前 Profile 全局配置运行。
- 有效固定旧任务继续按保存的 provider/model 运行。
- 不完整旧任务从“可能错误路由”改为“本次明确拒绝”；这是有意的 fail-closed 行为。
- 旧 WebUI 客户端如果只提交 `model`，服务端明确拒绝并提示刷新/重新选择，而不是猜测 Provider。

## 代码改动地图

### Hermes Agent（根修复，单独 Agent PR）

| 优先级 | 位置 | 改动 |
| --- | --- | --- |
| P0 | `cron/inference_binding.py`（新增） | Job axes schema 校验、主/默认 fallback 的完整 binding 解析、错误类型、日志 payload 生成；成为唯一 choke point。 |
| P0 | `cron/scheduler.py:run_job()` | 在任何网络调用和 retry 前解析 binding；主路由耗尽后对可恢复错误执行一次当前 Profile 默认 fallback。 |
| P0 | `cron/jobs.py:create_job()` / `update_job()` | 原子校验和保存 provider+model；禁止部分 pin；保留 snapshots 为 audit-only。 |
| P0 | `tools/cronjob_tools.py` | 工具 schema 与 update 语义要求完整绑定或完整清空；错误信息与 scheduler 一致。 |
| P1 | `cron/executions.py` / state-db 写入路径 | 保存每次实际 provider、model、base_url、api_mode、origin、fallback 原因与 validation outcome，供历史查询。 |
| P1 | Agent CLI | 增加 `cron audit-inference` 和受确认的 `cron repair-inference`；默认 dry-run。 |

### Hermes WebUI（同一逻辑的输入/展示适配）

| 优先级 | 位置 | 改动 |
| --- | --- | --- |
| P0 | `integration/crons/execution_model.py` | 历史 model-only job 不再原样放行到手工执行；调用 Agent binding contract 或以其返回的 validation result 拒绝。 |
| P0 | `integration/crons/handlers.py` | create/update/run 传递完整绑定；错误响应通过 `bad()`/`j()` 且中文文案。 |
| P0 | `api/routes.py` 的 `/api/crons/*` 接缝 | 上游 Tasks 与 Cron Hub 共享 schema normalizer；保持路由文件为薄接线。 |
| P1 | `integration/assets/hermes_integration_crons.js` 与上游 Tasks UI | 模型选择持有 provider identity；编辑旧破损任务时显示“需重新选择模型来源”，提交完整绑定后才允许保存/运行。 |
| P1 | `integration/swagger/openapi.json`、`integration/README.md`、`docs/api/cron-hub-api.md` | 同步 request/response schema、迁移状态和错误码。 |

Agent 代码是实际自动调度的所有者；WebUI 不应通过写一套局部推断器来替代它。若部署的 Agent 版本尚未提供 binding contract，WebUI 对 model-only job 只可拒绝手工运行并提示升级，不得继续触发可能的错误路由。

## 测试计划

测试必须先在未修复版本验证失败，再在修复后通过。核心断言是传给 runtime resolver/AIAgent 的实际 provider、base URL 和 model，而不是源代码中是否存在某个字符串。

### Agent 测试

| 场景 | 预期 |
| --- | --- |
| `model=deepseek-v4-flash`、`provider=null`、当前默认 `custom` | 在创建/更新时被拒绝；对历史任务 scheduler 不构造 AIAgent、不发网络请求。 |
| `provider=deepseek`、`model=deepseek-v4-flash`、全局默认 `custom` | runtime/AIAgent 一律使用 DeepSeek 与 `https://api.deepseek.com`。 |
| 三项均空，Profile 默认是 `custom + local-model` | 使用当前 Profile 完整默认配置，保持未固定任务语义。 |
| 三项均空，Profile 默认切换到 DeepSeek | 下一次运行整体切换为 DeepSeek + 默认模型，不混用旧 snapshot。 |
| `provider=custom:local`、`model=deepseek-v4-flash`、本地 URL | 保持命名 custom Provider；不凭模型名称擅自切换到 DeepSeek。 |
| 固定 DeepSeek 连接失败 | 主路由耗尽后，以当前 Profile 的完整默认 binding 额外执行一次；若默认是 custom + Qwen，则两项一起切换。 |
| 固定 DeepSeek 认证失败 | 不执行 Profile 默认 fallback；保留认证错误，防止掩盖凭据配置问题。 |
| Profile 默认 binding 不可解析 | 不执行 fallback；保留主错误和默认配置解析错误，不发出半成品请求。 |
| `provider/base_url` 不匹配 | 在 resolver 前 fail closed；不会泄露已保存凭据。 |
| 并行不同 Profile | 两个 execution 使用各自 Profile 的绑定，日志/ledger 不交叉。 |

### WebUI 测试

| 场景 | 预期 |
| --- | --- |
| Cron Hub 创建/更新只提交 model | 返回中文 400，不写入部分 pin。 |
| Cron Hub 手动执行历史 model-only job | 不启动线程/子进程；显示修复提示。 |
| Cron Hub 完整 DeepSeek binding | 传递 provider、model、base URL 到执行副本，且原任务快照不被错误覆盖。 |
| 上游 Tasks 与 Cron Hub | 两个入口得到相同 schema 验证结果。 |
| Cron history | 显示实际 execution binding，而不是创建时 snapshot。 |
| 旧客户端 | 得到明确升级/重新选择提示，不静默退回 custom。 |

建议在 WebUI 运行：

```bash
./scripts/test.sh integration/tests/crons/ tests/test_cron_materialized_model.py
```

在 Agent 仓库运行相应的 `tests/cron/test_cron_provider_pin.py`、scheduler、jobs 与 cron tool 测试。真实验证使用隔离 `HERMES_HOME` 与 `HERMES_WEBUI_STATE_DIR`，并用本地 stub endpoint 断言请求 host，不能使用真实 API Key。

## 发布与回滚

1. **紧急修复当前任务。** 在任务 UI 或官方 update 路径中明确保存 `deepseek + deepseek-v4-flash`；在配置允许时保存 DeepSeek base URL。暂停任务直到手工运行确认出站 host 是 `api.deepseek.com`。
2. **先发布 Agent P0。** scheduler 在网络调用前阻止 incomplete binding，并写入可诊断错误。此步骤先于任何自动迁移。
3. **发布审计。** 以 dry-run 扫描所有 Profile，导出待确认任务清单；不要自动修改不明确记录。
4. **人工/有 provenance 的修复。** 每个候选保存完整 binding，随后重新扫描直到没有 scheduled 的 `incomplete_binding`。
5. **发布 WebUI P0/P1。** UI 禁止再次创建部分 pin，并显示历史任务修复入口；同步 OpenAPI 和用户文档。
6. **观察。** 监控 `binding_validation=incomplete_binding`、实际 provider/base_url、fallback origin、触发原因和拒绝次数；确认一天完整调度周期无错误路由后，才考虑删除临时兼容提示。

回滚仅能回滚 UI 展示或新增写入校验，不能恢复“部分 binding 可以出网”的 scheduler 行为。若遇到未知历史格式，正确行为是保留任务和审计记录、暂停该次执行并请求人工选择，而不是把它重新路由到默认 Provider。

## 验收标准

- 对任务 `5c6baaaf679c` 的下一次手工和自动执行，记录的实际路由均为 `provider=deepseek`、`model=deepseek-v4-flash`、`base_url=https://api.deepseek.com`，并且当前 Agent registry 下 `api_mode=chat_completions`。
- 任意 `model != null && provider == null` 的 Cron job 在新版本中都不能向默认 custom endpoint 发出请求。
- 未固定任务仍可跟随同一 Profile 的当前完整默认配置；固定任务的主路由不受全局 default/custom 变化影响。
- 固定任务主路由发生可恢复错误并耗尽同路由重试后，默认按当前 Profile 的完整默认 binding 额外重试一次。
- Profile 默认 fallback 不会混用主模型与默认 Provider；execution record 必须同时记录主路由、实际 fallback 路由、origin 和触发原因。
- 历史扫描不泄露密钥，并且不基于模型名对 custom/转售商任务做未经确认的重写。
- 自动调度、两个 WebUI 入口、重试、历史记录和诊断日志使用同一个 `ResolvedCronInferenceBinding`。

## 开放问题

1. `base_url` 是否应在所有原生 Provider 的固定任务中显式持久化，还是仅在用户覆盖时保存、其余由 Provider registry 在运行时解析？本 RFC 建议后者：Provider 身份和 model 是用户意图，标准 endpoint 是可升级的 Provider 配置；但 execution ledger 必须记录最终 URL。
2. 是否为历史任务提供 Cron Hub 内的批量修复 UI？建议先提供只读 audit 和逐项确认，待生产数据确认后再设计批量操作。
3. 自定义多级 fallback 是否值得后续支持？如支持，应在独立 RFC 中定义固定默认 fallback 与显式链的优先级、成本提示和审计语义。
