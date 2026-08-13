from pathlib import Path


def test_server_starts_profile_gateway_coordinator_as_daemon_thread():
    source = (Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8")

    assert "from integration.gateway_startup import ensure_all_profile_gateways" in source
    assert 'name="profile-gateway-startup"' in source
    assert "daemon=True" in source
    assert "ensure_all_profile_gateways()" in source
    assert "from integration.gateway_startup import stop_gateway_processes" in source
    assert "stop_gateway_processes()" in source
