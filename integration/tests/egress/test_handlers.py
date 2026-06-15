import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.egress.apply import ApplyResult
from integration.egress.handlers import try_handle_get, try_handle_post


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def test_handlers_noop_when_disabled():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False
        assert try_handle_post(handler, parsed, {"policy_type": "open"}) is False


def test_get_returns_rules_text():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch(
            "integration.egress.handlers.get_current_iptables_rules",
            return_value=ApplyResult(ok=True, rules_path="", stdout="RULES"),
        ):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["rules"] == "RULES"


def test_post_open_applies_rules():
    handler = MagicMock()
    handler.client_address = ("9.9.9.9", 1234)
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_policy_rules_path", return_value="/tmp/rules.v4"):
            with patch(
                "integration.egress.handlers.apply_iptables_rules",
                return_value=ApplyResult(ok=True, rules_path="/tmp/rules.v4"),
            ) as apply:
                assert try_handle_post(handler, parsed, {"policy_type": "open"}) is True
                assert apply.call_count == 1
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["policy"]["policy_type"] == "open"
    assert payload["rules_path"] == "/tmp/rules.v4"


def test_post_whitelist_include_request_ip_opt_in():
    handler = MagicMock()
    handler.client_address = ("1.2.3.4", 7777)
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_policy_rules_path", return_value="/tmp/rules.v4"):
            with patch(
                "integration.egress.handlers.apply_iptables_rules",
                return_value=ApplyResult(ok=True, rules_path="/tmp/rules.v4"),
            ):
                body = {"policy_type": "whitelist", "allowed_ips": ["10.0.0.0/24"], "include_request_ip": True}
                assert try_handle_post(handler, parsed, body) is True
    payload = _json_payload(handler)
    allowed_ips = payload["policy"]["allowed_ips"]
    assert "10.0.0.0/24" in allowed_ips
    assert "1.2.3.4" in allowed_ips


def test_post_whitelist_rejects_non_list_allowed_ips():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        assert try_handle_post(handler, parsed, {"policy_type": "whitelist", "allowed_ips": "1.2.3.4"}) is True
    handler.send_response.assert_called_with(400)


def test_get_traffic_returns_records():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?limit=2")
    fake = [
        {"ts": "2026-06-15 11:00:02.000000", "verdict": "allowed", "proto": "tcp",
         "src": "172.17.0.5", "sport": 1, "dst": "1.1.1.1", "dport": 443, "length": 0, "flags": "S"},
        {"ts": "2026-06-15 11:00:01.000000", "verdict": "denied", "proto": "tcp",
         "src": "172.17.0.5", "sport": 2, "dst": "9.9.9.9", "dport": 443, "length": 0, "flags": "S"},
    ]
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", return_value=fake) as rt:
                assert try_handle_get(handler, parsed) is True
                assert rt.call_args.kwargs["limit"] == 2
                assert rt.call_args.kwargs["all_records"] is False
                assert rt.call_args.kwargs["verdict"] is None
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["records"][0]["verdict"] == "allowed"


def test_get_traffic_all_and_verdict_params():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?all=1&verdict=denied")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", return_value=[]) as rt:
                assert try_handle_get(handler, parsed) is True
                assert rt.call_args.kwargs["all_records"] is True
                assert rt.call_args.kwargs["verdict"] == "denied"
    payload = _json_payload(handler)
    assert payload["count"] == 0


def test_get_traffic_bad_verdict_rejected():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?verdict=bogus")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_get_traffic_tcpdump_missing_returns_500():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", side_effect=FileNotFoundError("tcpdump")):
                assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(500)

