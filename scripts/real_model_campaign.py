#!/usr/bin/env python3
"""Manual real-model E2E campaign for multi-turn artifact alignment.

This is intentionally outside tests/ and is never run by CI or pytest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
ROUND_TIMEOUT = 180
SETTLE_TIMEOUT = 25


@dataclass(frozen=True)
class Scenario:
    slug: str
    title: str
    objective: str


SCENARIOS = (
    Scenario("order-incident", "电商订单异常事故复盘与整改", "修复订单异常并交付整改材料"),
    Scenario("sales-funnel", "销售漏斗与区域业绩分析", "统一指标口径并产出管理报告"),
    Scenario("support-kb", "客服工单知识库治理", "整理矛盾工单并交付可检索知识库"),
    Scenario("pipeline-migration", "Python 数据管道迁移修复", "迁移旧配置并提供回滚方案"),
    Scenario("release-audit", "SaaS 发布前质量审计", "修复高风险项并交付发布 Runbook"),
    Scenario("access-control", "内部权限配置安全整改", "修复越权风险并交付验证材料"),
    Scenario("content-ops", "内容运营月度复盘与排期", "汇总素材与效果数据并制定排期"),
    Scenario("expense-audit", "财务费用报销数据核验", "发现异常并交付审计摘要"),
    Scenario("training-refresh", "内部培训课程改版", "依据反馈改造课程并交付培训包"),
    Scenario("service-config", "遗留服务配置迁移", "完成配置转换和兼容性校验"),
)

ACTIVITIES = (
    "阅读项目背景、约束和已有实现，输出风险判断", "盘点原始资料和已有资产",
    "验证跨文件矛盾，输出可执行问题清单", "修复关键实现并运行局部验证",
    "建立业务规则、指标口径或分类规则", "完成第二项关键修复或分析",
    "产出中间业务分析或设计文档", "修改已有交付物并补充变更说明",
    "执行跨文件核验，输出异常归因或兼容性分析", "创建用户可浏览的最终交付",
    "补齐可复用的校验脚本或检查表", "完成依赖前序结果的文件交付",
    "运行校验并修订此前交付", "完成交付说明和遗留风险", "生成最终索引并复核依赖关系",
)

SCENARIO_EVIDENCE = {
    "order-incident": ("orders.csv", "order_id,amount,status\nA1,120,paid\nA2,-20,paid\n", "clean_orders.py"),
    "sales-funnel": ("funnel.csv", "region,lead,qualified,won\neast,120,80,32\nwest,90,65,31\n", "metrics.py"),
    "support-kb": ("tickets.json", '[{"id":"T1","topic":"退款"},{"id":"T2","topic":"billing"}]\n', "classify_tickets.py"),
    "pipeline-migration": ("legacy.yaml", "source: invoices.csv\ndelimiter: ';'\n", "convert_config.py"),
    "release-audit": ("release.json", '{"version":"2.4.0","migration":"001","checkout":true}\n', "release_check.py"),
    "access-control": ("roles.json", '{"admin":["read","write"],"analyst":["read","delete"]}\n', "authorize.py"),
    "content-ops": ("posts.csv", "title,channel,views,leads\nA,wechat,1000,12\nB,video,800,18\n", "score_content.py"),
    "expense-audit": ("expenses.csv", "id,employee,amount,category\nE1,alice,1200,travel\nE2,alice,1200,travel\n", "check_expenses.py"),
    "training-refresh": ("feedback.csv", "topic,rating,comment\nsecurity,2,too abstract\ntesting,5,useful\n", "build_course.py"),
    "service-config": ("old-service.ini", "[server]\nport=8080\ntimeout=30\n", "migrate_service.py"),
}


def cancellation_plan(turns: int) -> dict[int, str]:
    count = min(4, max(1, -(-turns // 4)))
    choices = ((0.20, "immediate_after_start"), (0.40, "after_first_tool"),
               (0.53, "after_manifest_delta"), (0.80, "after_first_artifact"))
    if count == 1:
        choices = choices[:1]
    elif count == 2:
        choices = (choices[0], choices[3])
    elif count == 3:
        choices = (choices[0], choices[1], choices[3])
    result = {}
    for ratio, trigger in choices:
        round_number = max(2, min(turns, round(turns * ratio)))
        while round_number in result and round_number < turns:
            round_number += 1
        result[round_number] = trigger
    return dict(sorted(result.items()))


def seed_scenario(workspace: Path, scenario: Scenario) -> None:
    source_name, source_content, implementation_name = SCENARIO_EVIDENCE[scenario.slug]
    files = {
        "brief.md": f"# {scenario.title}\n\n目标：{scenario.objective}\n",
        "constraints.md": "不得删除原始输入；结论必须引用本地证据；修改必须可追溯。\n",
        f"input/{source_name}": source_content,
        "input/events.json": '[{"id":"A2","event":"negative amount accepted"},{"id":"A3","event":"duplicate refund"}]\n',
        f"existing/{implementation_name}": "def validate(row):\n    return True\n",
        "existing/guide.md": "# Existing guide\nThe old process has not been validated.\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_prompt(scenario: Scenario, turn: int, nonce: str, previous_path: str | None) -> tuple[str, str]:
    artifact = f"deliverables/turn-{turn:02d}/delivery.md"
    previous = previous_path or "brief.md、constraints.md 和 existing/ 下的资料"
    prompt = f"""我正在推进“{scenario.title}”。请使用工作区本地资料完成本轮真实交付，而不是只在回复中描述结果。
本轮任务：{ACTIVITIES[turn - 1]}。请先用工具阅读 {previous}，再读取必要的 input/ 或 existing/ 文件，进行编辑、分析或运行验证。
请把面向项目负责人的交付写到 `{artifact}`，包含业务结论、证据和下一步建议。文件中必须包含：
CAMPAIGN_ID={{campaign_id}}\nSESSION_ID={{session_id}}\nTURN={turn}\nNONCE={nonce}
完成后简要说明实际创建或修改的文件。"""
    return prompt, artifact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class Api:
    def __init__(self, base_url: str): self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None, timeout: int = 20) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try: payload = json.loads(exc.read().decode())
            except json.JSONDecodeError: payload = {"error": str(exc)}
            payload["_http_status"] = exc.code
            return payload

    def events(self, stream_id: str):
        request = urllib.request.Request(self.base_url + "/api/chat/stream?stream_id=" + urllib.parse.quote(stream_id))
        with urllib.request.urlopen(request, timeout=ROUND_TIMEOUT) as response:
            event, data = "message", []
            for raw in response:
                line = raw.decode(errors="replace").strip()
                if not line and data:
                    try: yield event, json.loads("\n".join(data))
                    except json.JSONDecodeError: yield event, {"raw": "\n".join(data)}
                    event, data = "message", []
                elif line.startswith("event:"): event = line[6:].strip()
                elif line.startswith("data:"): data.append(line[5:].strip())


def _turn_key(session: dict, nonce: str) -> str:
    rows = [m for m in session.get("messages", []) if isinstance(m, dict) and nonce in str(m.get("content") or "")]
    return str(rows[0].get("_turn_key") or "") if len(rows) == 1 else ""


def _artifacts(manifest: dict, turn_key: str) -> set[str]:
    row = next((r for r in manifest.get("turns", []) if r.get("turn_key") == turn_key), {})
    return {str(item.get("path") or "") for item in row.get("artifacts", []) if item.get("path")}


def _artifact_hashes(manifest: dict, turn_key: str, workspace: Path) -> dict[str, str]:
    hashes = {}
    for relative in _artifacts(manifest, turn_key):
        path = (workspace / relative).resolve()
        if workspace.resolve() in path.parents and path.is_file() and not path.is_symlink():
            hashes[relative] = _sha256(path)
    return hashes


def evaluate_alignment(session: dict, manifest: dict, ledger: dict, workspace: Path, dom_paths: set[str] | None) -> tuple[list[dict], list[dict], dict[str, str]]:
    failures, observations, hashes = [], [], {}
    actual_key = _turn_key(session, ledger["nonce"])
    if not actual_key:
        return failures, [{"code": "TURN_KEY_UNAVAILABLE"}], hashes
    ledger["turn_key"] = actual_key
    turn_keys = {str(row.get("turn_key") or "") for row in manifest.get("turns", [])}
    if actual_key not in turn_keys: failures.append({"code": "MANIFEST_TURN_MISSING", "turn_key": actual_key})
    if actual_key in set(manifest.get("diagnostics", {}).get("orphan_turn_keys", [])): failures.append({"code": "MANIFEST_ORPHAN_TURN", "turn_key": actual_key})
    artifacts = _artifacts(manifest, actual_key)
    for relative in artifacts:
        path = (workspace / relative).resolve()
        if workspace.resolve() not in path.parents or not path.is_file() or path.is_symlink():
            failures.append({"code": "ARTIFACT_PATH_INVALID", "path": relative}); continue
        if relative.startswith("deliverables/") and ledger["nonce"] not in path.read_text(encoding="utf-8", errors="replace"):
            failures.append({"code": "ARTIFACT_NONCE_MISMATCH", "path": relative})
        hashes[relative] = _sha256(path)
        for other in manifest.get("turns", []):
            other_paths = {str(a.get("path") or "") for a in other.get("artifacts", [])}
            if other.get("turn_key") != actual_key and relative in other_paths:
                failures.append({"code": "ARTIFACT_MULTI_TURN_OWNER", "path": relative, "other_turn": other.get("turn_key")})
    if not artifacts: observations.append({"code": "MODEL_NO_ARTIFACT"})
    if dom_paths is not None and dom_paths != artifacts:
        failures.append({"code": "DOM_MANIFEST_ARTIFACT_MISMATCH", "manifest": sorted(artifacts), "dom": sorted(dom_paths)})
    return failures, observations, hashes


class BrowserProbe:
    def __init__(self, base_url: str):
        from playwright.sync_api import sync_playwright
        self.base_url, self.pw = base_url.rstrip("/"), sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)

    def paths(self, session_id: str, turn_key: str) -> set[str] | None:
        page = self.browser.new_page()
        try:
            page.add_init_script("sid => localStorage.setItem('hermes-webui-session', sid)", session_id)
            page.goto(self.base_url + "/", wait_until="domcontentloaded")
            page.wait_for_function("sid => window.S && S.session && S.session.session_id === sid", session_id, timeout=15000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("sid => window.S && S.session && S.session.session_id === sid", session_id, timeout=15000)
            return set(page.locator(f'.assistant-turn[data-turn-key="{turn_key}"] .turn-artifact-chip').evaluate_all("nodes => nodes.map(n => n.dataset.path || '')"))
        except Exception:
            return None
        finally:
            page.close()

    def close(self): self.browser.close(); self.pw.stop()


def _settle(api: Api, session_id: str) -> dict:
    until, latest = time.monotonic() + SETTLE_TIMEOUT, {}
    while time.monotonic() < until:
        latest = api.request("GET", "/api/session?session_id=" + urllib.parse.quote(session_id) + "&messages=1")
        if not latest.get("session", {}).get("active_stream_id"): return latest
        time.sleep(0.4)
    return latest


def _run_round(api: Api, probe: BrowserProbe, session_id: str, workspace: Path, scenario: Scenario, campaign_id: str, turn: int, previous: str | None, trigger: str | None) -> dict:
    nonce = uuid.uuid4().hex
    prompt, expected = build_prompt(scenario, turn, nonce, previous)
    prompt = prompt.format(campaign_id=campaign_id, session_id=session_id)
    row: dict[str, Any] = {"turn": turn, "nonce": nonce, "prompt": prompt, "expected_artifact_path": expected, "cancel_trigger": trigger, "events": [], "observations": [], "alignment_failures": []}
    start = api.request("POST", "/api/chat/start", {"session_id": session_id, "workspace": str(workspace), "message": prompt})
    chat_start_received_at = time.monotonic()
    row["chat_start"], row["stream_id"] = start, str(start.get("stream_id") or "")
    if not row["stream_id"]:
        row["observations"].append({"code": "CHAT_START_FAILED", "detail": start.get("error", "missing stream_id")}); return row
    cancelled = False
    if trigger == "immediate_after_start":
        row["cancel_dispatch_delay_ms"] = round((time.monotonic() - chat_start_received_at) * 1000, 1)
        t0 = time.monotonic(); row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"])); row["cancel_response_ms"] = round((time.monotonic() - t0) * 1000, 1); cancelled = True
        if row["cancel_dispatch_delay_ms"] > 250: row["alignment_failures"].append({"code": "IMMEDIATE_CANCEL_SLOW", "actual_ms": row["cancel_dispatch_delay_ms"]})
    try:
        for event, payload in api.events(row["stream_id"]):
            row["events"].append({"event": event, "payload": payload})
            matched = (trigger == "after_first_tool" and event in {"tool", "tool_complete"}) or (trigger == "after_manifest_delta" and event == "manifest_delta") or (trigger == "after_first_artifact" and event == "manifest_delta" and bool(payload.get("artifacts")))
            if matched and not cancelled: row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"])); cancelled = True
            if event in {"done", "cancel", "apperror", "error", "stream_end"}: break
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        row["observations"].append({"code": "STREAM_ERROR", "detail": type(exc).__name__})
    if trigger and not cancelled:
        row["observations"].append({"code": "CANCEL_TRIGGER_NOT_REACHED", "trigger": trigger}); row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"]))
    session = _settle(api, session_id).get("session", {})
    row["turn_key"] = _turn_key(session, nonce)
    manifest = api.request("GET", "/api/session/manifest?session_id=" + urllib.parse.quote(session_id)).get("manifest", {})
    before_reload_hashes = _artifact_hashes(manifest, row["turn_key"], workspace) if row["turn_key"] else {}
    dom_paths = probe.paths(session_id, row["turn_key"]) if row["turn_key"] else None
    failures, observations, hashes = evaluate_alignment(session, manifest, row, workspace, dom_paths)
    for path, digest in before_reload_hashes.items():
        if hashes.get(path) != digest:
            failures.append({"code": "RELOAD_HASH_MISMATCH", "path": path, "before": digest, "after": hashes.get(path)})
    row["alignment_failures"].extend(failures); row["observations"].extend(observations); row["artifact_hashes"] = hashes
    row["pre_reload_artifact_hashes"] = before_reload_hashes
    return row


def _state_dir() -> Path:
    home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(home / "webui"))).expanduser().resolve()


def run_campaign(session_count: int, turns: int, base_url: str) -> int:
    api = Api(base_url); health = api.request("GET", "/health")
    if health.get("status") != "ok" or health.get("active_streams") or health.get("active_runs"):
        raise RuntimeError("WebUI must be healthy and have no active streams or runs")
    try:
        from api.config import get_config
        model = ((get_config().get("model") or {}).get("default"))
    except Exception as exc:
        raise RuntimeError("cannot read config.yaml default model") from exc
    if not model: raise RuntimeError("config.yaml model.default is required")
    try: probe = BrowserProbe(base_url)
    except Exception as exc: raise RuntimeError("Playwright Chromium is required before real calls") from exc
    campaign_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = _state_dir() / "e2e_campaigns" / campaign_id; root.mkdir(parents=True, exist_ok=False)
    summary = {"campaign_id": campaign_id, "model": model, "base_url": base_url, "cancellation_plan": cancellation_plan(turns), "sessions": []}
    try:
        for index in range(session_count):
            scenario, workspace = SCENARIOS[index % len(SCENARIOS)], root / "artifacts" / f"session-{index + 1:02d}"
            seed_scenario(workspace, scenario)
            created = api.request("POST", "/api/session/new", {"workspace": str(workspace), "worktree": False})
            session_id = str(created.get("session", {}).get("session_id") or "")
            report = {"session_id": session_id, "scenario": asdict(scenario), "rounds": []}
            previous = None
            for turn in range(1, turns + 1):
                if not session_id: break
                round_row = _run_round(api, probe, session_id, workspace, scenario, campaign_id, turn, previous, cancellation_plan(turns).get(turn)); report["rounds"].append(round_row); previous = round_row["expected_artifact_path"]
            issues = [issue for row in report["rounds"] for issue in row["alignment_failures"]]
            report["alignment_status"] = "FAIL" if issues else ("PASS" if report["rounds"] else "INDETERMINATE")
            name = f"{campaign_id}-session-{index + 1:02d}-{session_id or 'uncreated'}.json"; (root / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["sessions"].append({"session_id": session_id, "report": name, "alignment_status": report["alignment_status"], "alignment_failure_count": len(issues)})
    finally:
        probe.close()
    (root / f"{campaign_id}-campaign.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(root); return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=1); 
    parser.add_argument("--turns", type=int, default=15); 
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    if args.sessions < 1 or args.turns < 5: parser.error("--sessions must be >= 1 and --turns must be >= 5")
    return run_campaign(args.sessions, args.turns, args.base_url)


if __name__ == "__main__": 
    raise SystemExit(main())
