"""Tests for Zhiling identity lookup client logging."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from integration.identity.client import lookup_current_identity


def test_lookup_logs_full_downstream_response_body(capsys):
    payload = {
        "username": "liuweijun",
        "display_name": "刘为君-测试",
        "ithinktank": {
            "account": "liuweijun",
            "userId": "7f929de24d1e9611807740a278006c",
            "uuid": "7f929de24d1e9611807740a278006c",
        },
        "ithinktank_account": "liuweijun",
        "ithinktank_user_id": "7f929de24d1e9611807740a278006c",
        "access_token": "should-redact",
    }
    # Build a body longer than the old 240/4000 caps to prove no truncation.
    payload["padding"] = "x" * 5000
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.text = '{"username":"liuweijun"}'

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.return_value = resp

    with patch("integration.identity.client.zhiling_control_plane_url", return_value="http://cp.test"):
        with patch("integration.identity.client.httpx.Client", return_value=mock_client):
            status, data = lookup_current_identity("tok")

    assert status == 200
    assert data["username"] == "liuweijun"
    err = capsys.readouterr().err
    assert "[webui][integration_login][identity_lookup] downstream response" in err
    assert "status=200" in err
    assert '"uuid":"7f929de24d1e9611807740a278006c"' in err
    assert "should-redact" not in err
    assert "<redacted>" in err
    assert "x" * 5000 in err
    assert "…" not in err.split("body=", 1)[-1]
