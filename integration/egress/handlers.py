"""HTTP handlers for integration egress policy routes (/api/integration/egress/*)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api.helpers import bad, j, require, _sanitize_error

from integration.config import egress_policy_enabled, egress_policy_rules_path
from integration.config import egress_capture_dir, egress_nflog_group_allowed, egress_nflog_group_denied
from integration.egress.apply import apply_iptables_rules, get_current_iptables_rules
from integration.egress.capture import read_traffic
from integration.egress.rules import generate_iptables_open_rules, generate_iptables_whitelist, normalize_allowed_ips


_STDERR_SNIPPET_MAX = 4000


def _client_ip(handler) -> str:
    host = getattr(handler, "client_address", None)
    if isinstance(host, tuple) and host:
        return str(host[0] or "")
    return ""


def try_handle_get(handler, parsed) -> bool:
    """分发 egress 的 GET 路由：/policy 查看当前规则，/traffic 读取出口流量日志。

    未启用 egress 策略或路径不匹配时返回 False，交还给上层路由继续处理。
    """
    if not egress_policy_enabled():
        return False
    if parsed.path == "/api/integration/egress/policy":
        result = get_current_iptables_rules()
        if not result.ok:
            bad(handler, result.error or "failed", status=500)
            return True
        j(handler, {"ok": True, "rules": result.stdout})
        return True
    if parsed.path == "/api/integration/egress/traffic":
        return _handle_traffic_get(handler, parsed)
    return False


def _handle_traffic_get(handler, parsed) -> bool:
    """处理 GET /traffic：按 verdict/all/limit 查询参数返回出口流量记录。"""
    qs = parse_qs(parsed.query)
    # verdict 可选，留空表示放行+拒绝都返回；给了值则必须是 allowed/denied。
    verdict = (qs.get("verdict", [None])[0] or None)
    if verdict is not None:
        verdict = verdict.strip().lower()
        if verdict not in ("allowed", "denied"):
            bad(handler, "verdict must be 'allowed' or 'denied'", status=400)
            return True
    # all=1/true/yes/on 时返回全量，忽略 limit。
    all_records = (qs.get("all", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
    # limit 默认 100，非法值回落默认，负数视为 0。
    raw_limit = (qs.get("limit", ["100"])[0]).strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 100
    if limit < 0:
        limit = 0
    try:
        records = read_traffic(
            egress_capture_dir(),
            limit=limit,
            all_records=all_records,
            verdict=verdict,
        )
    except FileNotFoundError:
        bad(handler, "tcpdump not found in container", status=500)
        return True
    except Exception as exc:
        bad(handler, _sanitize_error(exc), status=500)
        return True
    j(handler, {"ok": True, "count": len(records), "records": records})
    return True


def try_handle_post(handler, parsed, body: dict | None) -> bool:
    if not egress_policy_enabled():
        return False
    if parsed.path != "/api/integration/egress/policy":
        return False
    body = body if isinstance(body, dict) else {}

    try:
        require(body, "policy_type")
    except ValueError as exc:
        bad(handler, str(exc), status=400)
        return True

    policy_type = str(body.get("policy_type") or "").strip().lower()
    include_request_ip = body.get("include_request_ip") is True

    try:
        if policy_type == "open":
            rules = generate_iptables_open_rules(nflog_group_allowed=egress_nflog_group_allowed())
            payload: dict = {"policy_type": "open"}
        elif policy_type == "whitelist":
            allowed = body.get("allowed_ips")
            if not isinstance(allowed, list):
                bad(handler, "allowed_ips must be a list", status=400)
                return True
            ips = [str(x) for x in allowed]
            if include_request_ip:
                client_ip = _client_ip(handler)
                if client_ip:
                    ips.append(client_ip)
            normalized = normalize_allowed_ips(ips)
            rules = generate_iptables_whitelist(
                normalized,
                nflog_group_allowed=egress_nflog_group_allowed(),
                nflog_group_denied=egress_nflog_group_denied(),
            )
            payload = {"policy_type": "whitelist", "allowed_ips": normalized, "include_request_ip": include_request_ip}
        else:
            bad(handler, "policy_type must be 'open' or 'whitelist'", status=400)
            return True
    except ValueError as exc:
        bad(handler, str(exc), status=400)
        return True
    except Exception as exc:
        bad(handler, _sanitize_error(exc), status=500)
        return True

    result = apply_iptables_rules(rules, rules_path=egress_policy_rules_path())
    if not result.ok:
        err = (result.stderr or result.error or "apply failed")[:_STDERR_SNIPPET_MAX]
        j(
            handler,
            {
                "ok": False,
                "error": err,
                "policy": payload,
                "rules_path": result.rules_path,
                "returncode": result.returncode,
            },
            status=500,
        )
        return True

    j(
        handler,
        {
            "ok": True,
            "policy": payload,
            "rules_path": result.rules_path,
        },
    )
    return True

