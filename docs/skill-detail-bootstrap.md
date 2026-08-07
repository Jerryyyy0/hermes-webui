# 启动时补全用户技能 .detail.json 实现方案

## Context

用户技能有三种来源，对应不同的标记文件约定（参见 `integration/skills/local_skills.py:381-386` 的现有过滤逻辑）：

| 来源 | 标记文件 | `.detail.json` 正常状态 | 是否本次处理 |
|---|---|---|---|
| 技能市场安装（SkillHub） | `.hub_installed` | 安装时由 `fetch_skill_detail` 写入 | 否 -- 缺失属于上游问题 |
| 用户上传（`POST /api/skillhub/upload`） | `.user_created` | 上传后前端串调 `ai-meta` + `detail` 接口写入 | 是 -- 仅在链路异常导致缺失/不全时 |
| 对话/会话创建（`save_skill()` 等） | 无标记 | 创建路径不写 `.detail.json` | 是 |

用户上传技能的正常流程会生成 `.detail.json`，但若上传后前端串调 `ai-meta` 或 `detail` 接口时发生异常（网络中断、上游 LLM 不可用、用户关闭页面等），`.detail.json` 会缺失或字段不全。对话创建的技能则天然没有 `.detail.json`。这两类异常/缺失情况历史上只能由用户手动重新触发 `ai-meta` + `detail` 修复，导致前端无法展示 `display_name` / `display_description` / 结构化 `detail_json`。

本方案是一个**自愈机制**：服务启动时扫描所有 profile 下的用户技能（排除 `.hub_installed` 市场技能），对缺失或字段不全的 `.detail.json` 优先从已完整的 profile 同步，无完整源时才调用上游 LLM 抽取（`extract_ai_meta`），成功后写入所有缺失的 profile。

## 决策对齐

| 决策项 | 选择 |
|---|---|
| 缺失判定 | 文件不存在，或解析失败，或缺少必填字段（`name` / `description` / `detail_json`）任一为空 |
| 扫描范围 | `SKILL.md` 存在且不含 `.hub_installed` 标记的目录（含 `.user_created` 上传和无标记的对话创建） |
| 同步优先 | 同一技能在任一 profile 已完整时，直接复制到缺失 profile，不调上游 |
| 提取方式 | `extract_ai_meta`（本地 `SKILL.md` -> 上游 `/api/admin/meta/extract` 三步 LLM 编排），description 从 SKILL.md frontmatter 解析传入 |
| 重试策略 | 指数退避 + ±50% 抖动，`base_delay=1.0`，最多 3 次（sleep 范围 0.5-1.0s、1.0-2.0s） |
| 启动抖动 | 守护线程启动前随机 sleep 0-30 秒（`HERMES_SKILL_BOOTSTRAP_JITTER` 可调，设 0 禁用），错开多实例 thundering herd |
| 启动行为 | 守护线程非阻塞，失败仅日志不影响服务就绪 |
| 并发保护 | 写入前 re-check `.detail.json`，若期间已被前端/别处填完整则跳过不覆盖 |

## 实现概览

新增模块 `integration/skills/detail_bootstrap.py` 承载扫描 + 同步 + 提取逻辑；在 `server.py` 启动钩子区按现有 `_bootstrap_no_self_improve_hub_sync` 模式新增一个守护线程调用它。复用现有的 `read_detail_json` / `extract_ai_meta` / `save_skill_detail` / `skills_dir_for_profile` / `list_profiles_api` / `parse_logical_name_from_skill_md` / `iter_skill_index_files`，不重写已有逻辑。

## 关键文件

### 新增：`integration/skills/detail_bootstrap.py`

模块级函数：

```python
REQUIRED_DETAIL_FIELDS = ("name", "description", "detail_json")

def bootstrap_missing_skill_details() -> dict:
    """扫描所有 profile 的用户技能，对缺失 .detail.json 的技能优先从
    已完整 profile 同步，无完整源时调上游 LLM 抽取。
    返回 {"scanned": int, "missing": int, "extracted": int,
          "synced": int, "failed": list[str]}
    """

def _is_detail_complete(detail: dict | None) -> bool:
    """文件不存在 / 解析失败 / None -> False；
    缺少任一必填字段或字段为空（空串/空 dict/空 list）-> False；
    其余 -> True。"""

def _extract_with_retry(
    skill_md_content: str,
    name: str,
    description: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> dict | None:
    """调用 extract_ai_meta，指数退避 + ±50% 抖动重试。
    delay = base_delay * 2^(attempt-1) * (0.5 + random.random())
    成功且 result.detailJson 非空时返回结果；否则返回 None。"""

def _build_detail_payload(skill_name: str, ai_meta: dict, category: str) -> dict:
    """将 extract_ai_meta 返回值映射到 .detail.json 文件 schema：
        name                <- ai_meta.name or skill_name
        description         <- ai_meta.description or ""
        display_name        <- ai_meta.skillName or skill_name
        display_description <- ai_meta.displayDescription or ""
        detail_json         <- ai_meta.detailJson or {}
        category            <- 从 .category 文件读取（如有），否则 ""
    """

def _save_to_all_profiles(
    skill_name: str,
    detail: dict,
    locations: list[tuple[str, str, Path]],  # [(profile, dir_name, skill_dir), ...]
) -> list[str]:
    """对每个 (profile, dir_name, skill_dir) 写入前 re-check .detail.json：
    若已完整则跳过（不覆盖前端/别处的并发保存），否则调 save_skill_detail。
    返回".detail.json 现已完整"的 profile 列表（含跳过和实际写入的）。"""

def _parse_skill_description(content: str) -> str:
    """从 SKILL.md 解析 description：先读 frontmatter description 字段，
    没有则取正文第一行非标题行。镜像 local_skills._scan_custom_skill_dicts 的模式。"""

def _iter_skill_dirs(skills_dir: Path):
    """用 agent.skill_utils.iter_skill_index_files 递归查找所有 SKILL.md，
    支持 skills/<分类>/<技能>/SKILL.md 二层结构。"""

def _read_category_marker(skill_dir: Path) -> str
def _relative_dir_name(skill_dir: Path, skills_dir: Path) -> str
```

`bootstrap_missing_skill_details` 主流程（两阶段）：

1. `integration_enabled()` 门控，未启用直接返回零值结果。
2. `profiles = list_profiles_api()`，对每个 profile 取 `skills_dir_for_profile(name)`。
3. 用 `iter_skill_index_files` 递归扫描每个 profile 的 skills 目录，过滤掉 `.hub_installed` 标记的目录，按 frontmatter `name` 分组：`logical_name -> [(profile, dir_name, skill_dir), ...]`。
4. **Phase 1 - 扫描所有 location**：对每个技能遍历所有 profile 的 location，找出已完整的（作为同步源 `complete_detail` + `source_profile`）和缺失的（`incomplete_locs`）。全部已完整则跳过。
5. **Phase 2a - 同步（不调上游）**：若存在完整源，调 `_save_to_all_profiles(complete_detail, incomplete_locs)` 复制到缺失 profile。至少一个成功则 `synced += 1`，日志 `synced <name> from profile <source> to <N> profile(s)`，继续下一个技能。同步全部失败则 fallback 到 2b。
6. **Phase 2b - 抽取（fallback）**：无完整源或同步失败时，从 canonical location 读 `SKILL.md`，用 `_parse_skill_description` 解析 description，调 `_extract_with_retry` 指数退避重试。成功则 `_build_detail_payload` + `_save_to_all_profiles` 写入 `incomplete_locs`。
7. 返回 `{"scanned", "missing", "extracted", "synced", "failed"}`。

### 修改：`server.py`

在 `main()` 的 `_bootstrap_no_self_improve_hub_sync` 线程后紧接插入对称的启动钩子：

```python
def _bootstrap_skill_details_safe() -> None:
    try:
        from integration.config import integration_enabled
        if not integration_enabled():
            log_info("[--] skill detail bootstrap skipped: HERMES_INTEGRATION not enabled")
            return
        # Jitter to spread concurrent bootstrap scans across multi-instance
        # deployments pointing at the same SkillHub upstream (thundering herd).
        # Set HERMES_SKILL_BOOTSTRAP_JITTER=0 to disable.
        jitter_max = float(os.getenv("HERMES_SKILL_BOOTSTRAP_JITTER", "30") or "30")
        if jitter_max > 0:
            jitter = random.uniform(0, jitter_max)
            log_info(
                f"[--] skill detail bootstrap: waiting {jitter:.1f}s before scan (jitter, max {jitter_max:.0f}s)"
            )
            time.sleep(jitter)
        from integration.skills.detail_bootstrap import bootstrap_missing_skill_details
        result = bootstrap_missing_skill_details()
        log_info(
            f"[ok] skill detail bootstrap: scanned={result['scanned']}, "
            f"missing={result['missing']}, extracted={result['extracted']}, "
            f"synced={result['synced']}, failed={len(result['failed'])}"
        )
        if result["failed"]:
            logger.warning(
                "skill detail bootstrap failed for: %s", result["failed"]
            )
    except Exception:
        logger.exception("skill detail bootstrap failed")

threading.Thread(
    target=_bootstrap_skill_details_safe,
    name="skill-detail-bootstrap",
    daemon=True,
).start()
```

镜像 `server.py:817-837` 的现有模式：try/except 包裹、`integration_enabled` 门控、守护线程、零启动阻塞。新增 `import random` 到 server.py 顶部。

## 日志输出

| 场景 | 级别 | 示例 |
|---|---|---|
| integration 禁用 | INFO | `[--] skill detail bootstrap skipped: HERMES_INTEGRATION not enabled` |
| 启动抖动等待 | INFO | `[--] skill detail bootstrap: waiting 12.3s before scan (jitter, max 30s)` |
| 同步成功（不调上游） | INFO | `skill detail bootstrap: synced hermes-agent from profile sss3 to 1 profile(s)` |
| 抽取成功 | INFO | `skill detail bootstrap: extracted sketch in 4.61s (attempt 1/3)` |
| 抽取失败（3 次后放弃） | WARNING | `skill detail bootstrap: extraction failed for sketch after 3 attempts in 6.23s: ...` |
| 写入前 re-check 跳过 | INFO | `skill detail bootstrap: skip save for hermes-agent in profile sss3 (.detail.json became complete during extraction)` |
| 摘要 | INFO | `[ok] skill detail bootstrap: scanned=10, missing=4, extracted=1, synced=3, failed=0` |
| 有失败技能 | WARNING | `skill detail bootstrap failed for: ['sketch']` |

摘要总是打印（即使 `missing=0`），便于确认钩子执行过。

## 复用的现有函数

| 函数 | 路径 | 作用 |
|---|---|---|
| `read_detail_json` | `integration/skills/local_skills.py:1157-1169` | 读取 `.detail.json` |
| `save_skill_detail` | `integration/skills/local_skills.py:1619-1637` | 写入 `.detail.json` 到指定 profile |
| `extract_ai_meta` | `integration/skills/skillhub.py:1031-1054` | 上游 LLM 三步抽取 |
| `skills_dir_for_profile` | `integration/skills/paths.py:34-35` | profile 技能目录路径 |
| `parse_logical_name_from_skill_md` | `integration/skills/local_skills.py:821-826` | 从 frontmatter 解析技能名 |
| `iter_skill_index_files` | `agent/skill_utils.py`（hermes-agent 包） | 递归查找 SKILL.md |
| `list_profiles_api` | `api/profiles.py:2070` | 枚举所有 profile |

跨 profile 同步参考 `_post_skillhub_detail` (`integration/skills/handlers.py:758-798`) 的实现模式。

## 测试

### 新增：`integration/tests/skills/test_detail_bootstrap.py`

用 `tmp_path` + `monkeypatch` 把 `skills_dir_for_profile`（通过 patch `paths.get_hermes_home_for_profile`）/ `list_profiles_api` / `extract_ai_meta` / `time.sleep` / `random.random` 都打到临时目录和 MagicMock 上。14 个测试用例：

1. **`test_skill_with_no_detail_json_triggers_extraction`** - 技能目录无 `.detail.json`，断言 `extract_ai_meta` 被调用且 `.detail.json` 被写入。
2. **`test_skill_with_incomplete_detail_json_triggers_extraction`** - `.detail.json` 存在但缺 `detail_json` 字段，断言重新抽取。
3. **`test_skill_with_complete_detail_json_is_skipped`** - 完整 `.detail.json` 存在，断言 `extract_ai_meta` 未被调用。
4. **`test_extraction_retries_3_times_then_gives_up`** - `extract_ai_meta` 始终抛异常，断言调用 3 次、`failed` 列表包含技能名、无 `.detail.json` 被写入。
5. **`test_extraction_uses_exponential_backoff_with_jitter`** - 固定 `random.random()=0.5`，断言两次 sleep 分别精确等于 1.0s 和 2.0s（`base_delay * 2^(attempt-1) * 1.0`）。
6. **`test_extraction_succeeds_on_second_attempt`** - 第 1 次抛异常、第 2 次返回有效结果，断言最终写入。
7. **`test_detail_synced_to_all_profiles`** - 同一技能存在于 default 和另一 profile，断言两个 profile 的 `.detail.json` 都被写入且内容一致。
8. **`test_hub_installed_skill_is_skipped`** - 目录含 `.hub_installed` 标记，断言被扫描器跳过。
9. **`test_session_created_skill_without_marker_is_included`** - 目录无 `.user_created` 也无 `.hub_installed`（对话创建场景），断言被纳入扫描和抽取。
10. **`test_integration_disabled_returns_zeros`** - `integration_enabled` 返回 False，断言函数直接返回零值结果且不访问文件系统。
11. **`test_category_nested_skill_is_scanned`** - 技能在 `skills/<分类>/<技能>/SKILL.md` 二层结构下，断言被递归扫描器发现。
12. **`test_save_skipped_when_detail_becomes_complete_during_extraction`** - mock `extract_ai_meta` 在返回前写入完整 `.detail.json`（模拟前端并发保存），断言 bootstrap 不覆盖。
13. **`test_description_passed_from_skill_md_so_result_is_stable`** - 断言 description 从 frontmatter 传入 `extract_ai_meta`，保存的 `.detail.json` 第二次启动不再重提。
14. **`test_sync_from_complete_profile_skips_upstream_call`** - default profile 有完整 `.detail.json`、work 缺失，断言 `extract_ai_meta` 未被调用，work 内容与 default 一致，`synced=1`。

### 手动验证

1. 设置 `HERMES_INTEGRATION=1` 和 `SKILLHUB_URL=<上游地址>`，启动服务。
2. 观察日志：`[--] ... waiting X.Xs before scan (jitter, max 30s)` -> `[ok] skill detail bootstrap: scanned=N, missing=M, extracted=X, synced=S, failed=0`。
3. 检查 `.detail.json` 文件已生成且 `description` 字段非空（来自 SKILL.md frontmatter）。
4. 在两个 profile 下放同名技能（一个完整一个缺失），启动后观察 `synced=1`，缺失 profile 的 `.detail.json` 与完整 profile 一致。
5. 关掉网络或 mock `extract_ai_meta` 抛异常，启动后观察日志 `failed=1`，技能目录仍无 `.detail.json`，服务正常对外提供。
6. 多实例同时启动：设置 `HERMES_SKILL_BOOTSTRAP_JITTER=60` 加大抖动范围，观察各实例的 `waiting` 日志分散在不同时间点。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HERMES_INTEGRATION` | 未设置 | 需设为 `1`/`true`/`yes`/`on` 才启用 bootstrap（同时是 SkillHub 集成总开关） |
| `SKILLHUB_URL` | 未设置 | 上游 SkillHub 地址，`extract_ai_meta` 依赖此地址，未配置则抽取必失败 |
| `HERMES_SKILL_BOOTSTRAP_JITTER` | `30` | 启动抖动上限（秒），设 `0` 禁用抖动立即扫描 |

## 不在本次范围内

- 市场安装的技能（`.hub_installed` 标记）在扫描阶段即被排除 -- 这类技能在安装时已通过 `fetch_skill_detail` 写入 `.detail.json`，缺失属于上游问题，不应由本地补全流程重写。
- 重试超过 3 次的技能不写入"失败队列"持久化状态 -- 仅日志记录，避免引入新的状态 DB schema。如需后续重试，下次重启服务时会自动再试。
- 不阻塞启动、不暴露 HTTP API 触发扫描 -- 如需手动触发，可后续追加 `/api/skillhub/bootstrap-details` 端点。
- 跨实例无分布式协调 -- 多实例同时启动时各自独立扫描抽取，靠启动抖动 + 退避抖动错开上游请求峰值。如上游有严格并发限制需分布式锁，不在本次范围。
