from unittest.mock import MagicMock, patch

from integration.logout.client import _AUTH_PROXY_LOGOUT_PATH, logout_current_user


def test_logout_posts_to_fixed_path_under_base_url():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp

    with patch("integration.logout.client.zhiling_logout_base_url", return_value="http://auth-proxy:8080"):
        with patch("integration.logout.client.httpx.Client", return_value=mock_client):
            status, payload = logout_current_user()

    assert status == 200
    assert payload == {"status": "ok"}
    mock_client.post.assert_called_once_with(
        f"http://auth-proxy:8080{_AUTH_PROXY_LOGOUT_PATH}",
        headers={"Content-Type": "application/json"},
        json={},
    )
