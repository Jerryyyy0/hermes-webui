"""Upstream SkillHub client for the skill publish flow (docs §3).

B1-B4 endpoints are NOT yet provided by upstream SkillHub. This client
implements the documented contract so handlers are complete; requests will
raise SkillHubUpstreamError until the endpoints go live. Unit tests mock
``_client`` - no real requests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from integration.config import skillhub_url

_log = logging.getLogger(__name__)

_TIMEOUT = 120.0
_UPLOAD_TIMEOUT = 300.0
_NOTIFICATIONS_LIMIT = 50


class SkillHubUpstreamError(Exception):
    """Upstream SkillHub call failed (unconfigured URL, HTTP error, network)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SkillHubConflictError(SkillHubUpstreamError):
    """Upstream returned 409 (name/version conflict, B2)."""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


def _client(timeout: float = _TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _hub_base() -> str:
    url = skillhub_url()
    if not url:
        raise SkillHubUpstreamError("SKILLHUB_URL not configured")
    return url


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        with _client(kwargs.pop("timeout", _TIMEOUT)) as client:
            resp = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise SkillHubUpstreamError(f"skillhub request failed: {exc}") from exc
    if resp.status_code == 409:
        raise SkillHubConflictError(_detail_of(resp) or "Conflict")
    if resp.status_code >= 400:
        raise SkillHubUpstreamError(
            f"skillhub {resp.status_code}: {_detail_of(resp)}",
            status_code=resp.status_code,
        )
    return resp


def _detail_of(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("detail") or data.get("message") or "")
    except Exception:
        pass
    return resp.text[:200] if resp.text else ""


def upload_skill(
    zip_path: Path | str,
    zip_filename: str,
    *,
    platform: str,
    external_user_id: str,
    version: str,
    display_name: str = "",
    display_description: str = "",
    detail_json: str = "",
    applicant_name: str = "",
    applicant_org: str = "",
    applicant_title: str = "",
    source: str = "user",
    category: str = "",
) -> dict[str, Any]:
    """B2: POST /api/admin/upload (multipart). Returns parsed response dict.

    Response fields used by callers: name, skillId, applicationId, version,
    status, applyTime.
    """
    data = {
        "autoApprove": "false",
        "platform": platform,
        "externalUserId": external_user_id,
        "version": version,
        "source": source,
    }
    if display_name:
        data["displayName"] = display_name
    if display_description:
        data["displayDescription"] = display_description
    if detail_json:
        data["detailJson"] = detail_json
    if applicant_name:
        data["applicantName"] = applicant_name
    if applicant_org:
        data["applicantOrg"] = applicant_org
    if applicant_title:
        data["applicantTitle"] = applicant_title
    if category:
        data["category"] = category
    _log.info(
        "skillhub upload request: skill=%s version=%s fields=%s",
        zip_filename, version, data,
    )
    path = Path(zip_path)
    # Open-and-close inside the request context; caller unlinks in finally.
    with open(path, "rb") as fh:
        resp = _request(
            "POST",
            f"{_hub_base()}/api/admin/upload",
            data=data,
            files={"file": (zip_filename, fh, "application/zip")},
            timeout=_UPLOAD_TIMEOUT,
        )
    try:
        payload = resp.json()
    except Exception as exc:
        raise SkillHubUpstreamError("skillhub upload: non-JSON response") from exc
    if not isinstance(payload, dict):
        raise SkillHubUpstreamError("skillhub upload: unexpected response shape")
    return payload


def withdraw_approval(
    upstream_application_id: str,
    *,
    platform: str,
    external_user_id: str,
) -> dict[str, Any]:
    """B1: POST /api/admin/approvals/{id}/withdraw (publish/unpublish unified)."""
    resp = _request(
        "POST",
        f"{_hub_base()}/api/admin/approvals/{upstream_application_id}/withdraw",
        json={
            "platform": platform,
            "externalUserId": external_user_id,
        },
    )
    try:
        payload = resp.json()
    except Exception as exc:
        raise SkillHubUpstreamError("skillhub withdraw: non-JSON response") from exc
    return payload if isinstance(payload, dict) else {}


def unpublish_skill(
    name: str,
    *,
    platform: str,
    external_user_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """B4: POST /api/admin/skills/{name}/unpublish. Returns applicationId etc."""
    resp = _request(
        "POST",
        f"{_hub_base()}/api/admin/skills/{name}/unpublish",
        json={
            "platform": platform,
            "externalUserId": external_user_id,
            "reason": reason,
        },
    )
    try:
        payload = resp.json()
    except Exception as exc:
        raise SkillHubUpstreamError("skillhub unpublish: non-JSON response") from exc
    return payload if isinstance(payload, dict) else {}


def fetch_external_notifications(
    *,
    platform: str,
    external_user_id: str,
    since_id: int = 0,
    limit: int = _NOTIFICATIONS_LIMIT,
    read_type: str = "all",
) -> list[dict[str, Any]]:
    """B3: GET /api/external/notifications (live event stream).

    ``read_type`` mirrors the KB contract: ``all`` / ``unread`` / ``seen``.
    Read state is owned by upstream SkillHub, not persisted locally.
    """
    resp = _request(
        "GET",
        f"{_hub_base()}/api/external/notifications",
        params={
            "platform": platform,
            "externalUserId": external_user_id,
            "sinceId": since_id,
            "limit": limit,
            "readType": read_type,
        },
    )
    try:
        payload = resp.json()
    except Exception as exc:
        raise SkillHubUpstreamError(
            "skillhub notifications: non-JSON response"
        ) from exc
    items = payload.get("notifications") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def mark_external_notifications_read(
    *,
    platform: str,
    external_user_id: str,
    event_ids: list[int],
) -> bool:
    """B3 companion: POST /api/external/notifications/read.

    Placeholder until upstream provides the endpoint; mirrors the KB
    mark_message_read contract.
    """
    if not event_ids:
        return True
    resp = _request(
        "POST",
        f"{_hub_base()}/api/external/notifications/read",
        json={
            "platform": platform,
            "externalUserId": external_user_id,
            "notificationIds": event_ids,
        },
    )
    try:
        payload = resp.json()
    except Exception:
        return True
    if isinstance(payload, dict) and payload.get("code") not in (None, 200):
        return False
    return True
