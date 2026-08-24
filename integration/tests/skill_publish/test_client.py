"""Tests for skill_publish upstream client (all requests mocked)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from integration.skill_publish import client
from integration.skill_publish.client import (
    SkillHubConflictError,
    SkillHubUpstreamError,
    fetch_external_notifications,
    unpublish_skill,
    upload_skill,
    withdraw_approval,
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_client(response=None, capture=None):
    mc = MagicMock()
    mc.__enter__ = MagicMock(return_value=mc)
    mc.__exit__ = MagicMock(return_value=False)
    if capture is not None:

        def _request(method, url, **kwargs):
            capture.append((method, url, kwargs))
            return response

        mc.request = _request
    else:
        mc.request = MagicMock(return_value=response)
    return mc


def test_upload_skill_sends_multipart_contract(tmp_path):
    zip_path = tmp_path / "demo-1.0.0.zip"
    zip_path.write_bytes(b"PK-fake")
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={
            "applicationId": "up-1", "applyTime": "2026-08-17 10:00:00",
            "name": "demo", "skillId": "sk-9", "status": 1,
        }),
        capture=captured,
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            resp = upload_skill(
                zip_path, "demo-1.0.0.zip",
                platform="hermes-webui", external_user_id="acct-a",
                version="1.0.0",
                display_name="Demo", display_description="desc",
                detail_json="{}", applicant_name="A", applicant_org="O",
                applicant_title="T",
            )

    assert resp["applicationId"] == "up-1"
    method, url, kwargs = captured[0]
    assert method == "POST"
    assert url == "http://hub.test/api/admin/upload"
    data = kwargs["data"]
    assert data["autoApprove"] == "false"
    assert data["platform"] == "hermes-webui"
    assert data["externalUserId"] == "acct-a"
    assert data["version"] == "1.0.0"
    assert data["source"] == "user"
    assert data["displayName"] == "Demo"
    assert data["applicantName"] == "A"
    filename, fh, _ = kwargs["files"]["file"]
    assert filename == "demo-1.0.0.zip"
    fh.close()


def test_upload_skill_409_raises_conflict(tmp_path):
    zip_path = tmp_path / "x.zip"
    zip_path.write_bytes(b"PK")
    fake = _fake_client(
        response=_FakeResponse(status_code=409, payload={"detail": "name taken"})
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            with pytest.raises(SkillHubConflictError):
                upload_skill(
                    zip_path, "x.zip", platform="p", external_user_id="u",
                    version="1.0.0",
                )


def test_upload_skill_http_error_raises_upstream(tmp_path):
    zip_path = tmp_path / "x.zip"
    zip_path.write_bytes(b"PK")
    fake = _fake_client(response=_FakeResponse(status_code=500, payload={"detail": "boom"}))
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            with pytest.raises(SkillHubUpstreamError) as exc_info:
                upload_skill(
                    zip_path, "x.zip", platform="p", external_user_id="u",
                    version="1.0.0",
                )
    assert exc_info.value.status_code == 500


def test_unconfigured_url_raises_upstream(tmp_path):
    zip_path = tmp_path / "x.zip"
    zip_path.write_bytes(b"PK")
    with patch.object(client, "skillhub_url", return_value=""):
        with pytest.raises(SkillHubUpstreamError, match="not configured"):
            upload_skill(
                zip_path, "x.zip", platform="p", external_user_id="u",
                version="1.0.0",
            )


def test_withdraw_approval_posts_b1(tmp_path):
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={"code": 200}), capture=captured
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            resp = withdraw_approval(
                "up-42", platform="hermes-webui", external_user_id="acct-a"
            )
    assert resp == {"code": 200}
    method, url, kwargs = captured[0]
    assert method == "POST"
    assert url == "http://hub.test/api/admin/approvals/up-42/withdraw"
    assert kwargs["json"] == {
        "platform": "hermes-webui",
        "externalUserId": "acct-a",
    }


def test_unpublish_skill_posts_b4(tmp_path):
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={"applicationId": "up-7"}),
        capture=captured,
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            resp = unpublish_skill(
                "demo", platform="p", external_user_id="u", reason="cleanup"
            )
    assert resp["applicationId"] == "up-7"
    method, url, kwargs = captured[0]
    assert method == "POST"
    assert url == "http://hub.test/api/admin/skills/demo/unpublish"
    assert kwargs["json"] == {"platform": "p", "externalUserId": "u", "reason": "cleanup"}


def test_fetch_external_notifications_parses_b3():
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={
            "notifications": [
                {"id": 5, "scene": "1", "result": "1"},
                {"id": 6, "scene": "2", "result": "0"},
                "garbage",
            ]
        }),
        capture=captured,
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            events = fetch_external_notifications(
                platform="p", external_user_id="u", since_id=4, limit=50,
            )
    assert events == [{"id": 5, "scene": "1", "result": "1"}, {"id": 6, "scene": "2", "result": "0"}]
    method, url, kwargs = captured[0]
    assert method == "GET"
    assert url == "http://hub.test/api/external/notifications"
    assert kwargs["params"] == {
        "platform": "p", "externalUserId": "u", "sinceId": 4, "limit": 50,
        "readType": "all",
    }


def test_fetch_external_notifications_forwards_read_type():
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={"notifications": []}),
        capture=captured,
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            fetch_external_notifications(
                platform="p", external_user_id="u", read_type="unread",
            )
    assert captured[0][2]["params"]["readType"] == "unread"


def test_mark_external_notifications_read_posts_ids():
    captured = []
    fake = _fake_client(
        response=_FakeResponse(payload={"code": 200}), capture=captured
    )
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            ok = client.mark_external_notifications_read(
                platform="p", external_user_id="u", event_ids=[11, 12],
            )
    assert ok is True
    method, url, kwargs = captured[0]
    assert method == "POST"
    assert url == "http://hub.test/api/external/notifications/read"
    assert kwargs["json"] == {
        "platform": "p", "externalUserId": "u", "notificationIds": [11, 12],
    }


def test_mark_external_notifications_read_empty_ids_skips_call():
    with patch.object(client, "_client") as mock_client:
        assert client.mark_external_notifications_read(
            platform="p", external_user_id="u", event_ids=[],
        ) is True
    mock_client.assert_not_called()


def test_mark_external_notifications_read_error_code_returns_false():
    fake = _fake_client(response=_FakeResponse(payload={"code": 500}))
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            assert client.mark_external_notifications_read(
                platform="p", external_user_id="u", event_ids=[1],
            ) is False


def test_fetch_external_notifications_missing_list_returns_empty():
    fake = _fake_client(response=_FakeResponse(payload={"notifications": None}))
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            assert fetch_external_notifications(
                platform="p", external_user_id="u"
            ) == []


def test_non_json_response_raises_upstream():
    fake = _fake_client(response=_FakeResponse(payload=None, text="oops"))
    with patch.object(client, "skillhub_url", return_value="http://hub.test"):
        with patch.object(client, "_client", return_value=fake):
            with pytest.raises(SkillHubUpstreamError):
                withdraw_approval("up-1", platform="p", external_user_id="u")
