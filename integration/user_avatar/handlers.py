"""Early POST handler for the avatar variant of the existing upload route."""

from __future__ import annotations

from urllib.parse import parse_qs

from api.helpers import bad, j
from api.upload import parse_multipart
from integration.config import integration_enabled
from integration.user_avatar.store import (
    AvatarConfigConflict,
    AvatarError,
    AvatarStorageError,
    avatar_multipart_max_bytes,
    save_user_avatar,
)


def _is_avatar_upload(parsed) -> bool:
    purpose = (parse_qs(parsed.query or "").get("purpose") or [""])[0]
    return purpose == "user_avatar"


def try_handle_upload(handler, parsed) -> bool:
    """Consume only ``/api/upload?purpose=user_avatar`` before generic parsing."""
    if not _is_avatar_upload(parsed):
        return False
    if not integration_enabled():
        bad(handler, "头像功能未启用", status=404)
        return True

    try:
        _fields, files = parse_multipart(
            handler.rfile,
            handler.headers.get("Content-Type", ""),
            handler.headers.get("Content-Length", 0) or 0,
            max_bytes=avatar_multipart_max_bytes(),
        )
    except ValueError as exc:
        if "too large" in str(exc).lower():
            bad(handler, "头像大小需控制在 10M 以内", status=413)
        else:
            bad(handler, "头像上传请求无效", status=400)
        return True

    if "file" not in files:
        bad(handler, "缺少头像文件", status=400)
        return True
    filename, data = files["file"]
    if not filename:
        bad(handler, "头像文件名无效或缺失", status=400)
        return True

    try:
        save_user_avatar(filename, data)
    except AvatarConfigConflict as exc:
        bad(handler, str(exc), status=409)
        return True
    except AvatarStorageError as exc:
        bad(handler, str(exc), status=500)
        return True
    except AvatarError as exc:
        bad(handler, str(exc), status=400)
        return True

    # The external frontend intentionally re-fetches the shared appearance
    # config. It obtains user_avatar_path there instead of treating upload's
    # response as a durable source of truth.
    j(handler, {"ok": True})
    return True
