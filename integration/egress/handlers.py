"""HTTP handlers for integration egress policy routes (/api/integration/egress/*)."""

from __future__ import annotations

from api.helpers import bad, j, require, _sanitize_error

from integration.config import egress_policy_enabled, egress_policy_rules_path
from integration.egress.apply import apply_iptables_rules, get_current_iptables_rules
from integration.egress.rules import generate_iptables_open_rules, generate_iptables_whitelist, normalize_allowed_ips


_STDERR_SNIPPET_MAX = 4000


def _client_ip(handler) -> str:
    host = getattr(handler, "client_address", None)
    if isinstance(host, tuple) and host:
        return str(host[0] or "")
    return ""


def try_handle_get(handler, parsed) -> bool:
    if not egress_policy_enabled():
        return False
    if parsed.path != "/api/integration/egress/policy":
        return False
    result = get_current_iptables_rules()
    if not result.ok:
        bad(handler, result.error or "failed", status=500)
        return True
    j(handler, {"ok": True, "rules": result.stdout})
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
            rules = generate_iptables_open_rules()
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
            rules = generate_iptables_whitelist(normalized)
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

