"""Validated, atomic persistence for the current container's user avatar."""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from pathlib import Path

from integration.profiles.enrich import LOGO_MAX_BYTES, normalize_logo_data_uri
from integration.webui_appearance.paths import appearance_root, config_path, resolve_appearance_rel

AVATAR_CONFIG_FIELD = "user_avatar_path"
AVATAR_RELATIVE_DIRECTORY = "user_avatar"

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
_WRITE_LOCK = threading.RLock()


class AvatarError(ValueError):
    """A client-visible avatar validation or persistence-precondition error."""


class AvatarConfigConflict(AvatarError):
    """The existing appearance document cannot hold an avatar field."""


class AvatarStorageError(AvatarError):
    """The validated avatar could not be persisted by the server."""


def avatar_multipart_max_bytes() -> int:
    """Bound the multipart body while allowing normal framing overhead."""
    return LOGO_MAX_BYTES + 64 * 1024


def _validated_image(filename: str, data: bytes) -> tuple[str, bytes]:
    suffix = Path(str(filename or "")).suffix.lower()
    mime = _MIME_BY_SUFFIX.get(suffix)
    if not mime:
        raise AvatarError("头像仅支持 PNG、JPEG、GIF、WebP 或 SVG 图片")

    encoded = base64.b64encode(data).decode("ascii")
    normalized = normalize_logo_data_uri(f"data:{mime};base64,{encoded}")
    if normalized is None:
        raise AvatarError("头像文件无效，或大小超过 10M")

    # normalize_logo_data_uri() has already checked the MIME, size and SVG
    # safety. Decode its canonical form so the exact validated bytes are what
    # reach disk rather than trusting the original multipart payload again.
    try:
        canonical_data = normalized.split(",", 1)[1]
        return suffix, base64.b64decode(canonical_data, validate=True)
    except (IndexError, ValueError) as exc:  # defensive: normalizer contract
        raise AvatarError("头像文件无效") from exc


def _load_appearance_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AvatarError("外观配置文件不是合法 JSON") from exc
    except OSError as exc:
        raise AvatarStorageError("读取外观配置失败") from exc
    if not isinstance(data, dict):
        raise AvatarConfigConflict("外观配置必须是 JSON 对象才能设置头像")
    return data


def _write_json_atomically(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _configured_avatar_file(relative_path: object, root: Path, avatar_dir: Path) -> Path | None:
    if not isinstance(relative_path, str):
        return None
    if not relative_path.startswith(f"{AVATAR_RELATIVE_DIRECTORY}/"):
        return None
    try:
        target = resolve_appearance_rel(relative_path)
    except ValueError:
        return None
    # Only a file directly inside the directory owned by this feature can be
    # removed. This keeps arbitrary appearance assets and nested content intact.
    if (
        target.parent != avatar_dir
        or not target.name.startswith("avatar-")
        or target.suffix.lower() not in _MIME_BY_SUFFIX
    ):
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def save_user_avatar(filename: str, data: bytes) -> None:
    """Persist an avatar and publish its preview URL in appearance config.

    The config pointer is committed only after the candidate file exists. On a
    failed config write, the unpublished candidate is removed. Replacing an
    older avatar best-effort removes only the previous feature-owned file.
    """
    suffix, image_data = _validated_image(filename, data)

    with _WRITE_LOCK:
        root = appearance_root()
        config = _load_appearance_config(config_path())
        previous = config.get(AVATAR_CONFIG_FIELD)

        try:
            root.mkdir(parents=True, exist_ok=True)
            avatar_dir = resolve_appearance_rel(AVATAR_RELATIVE_DIRECTORY)
            avatar_dir.mkdir(exist_ok=True)
        except OSError as exc:
            raise AvatarStorageError("头像保存失败") from exc
        if not avatar_dir.is_dir():
            raise AvatarError("头像保存目录无效")

        relative_path = f"{AVATAR_RELATIVE_DIRECTORY}/avatar-{uuid.uuid4().hex}{suffix}"
        target = resolve_appearance_rel(relative_path)
        try:
            with target.open("xb") as handle:
                handle.write(image_data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise AvatarStorageError("头像保存失败") from exc

        config[AVATAR_CONFIG_FIELD] = relative_path
        try:
            _write_json_atomically(config_path(), config)
        except OSError as exc:
            try:
                target.unlink()
            except OSError:
                pass
            raise AvatarStorageError("头像保存失败") from exc

        old_target = _configured_avatar_file(previous, root, avatar_dir)
        if old_target and old_target != target:
            try:
                old_target.unlink(missing_ok=True)
            except OSError:
                # The new config pointer is already durable; a stale old image
                # is harmless and can be cleaned up by a later replacement.
                pass
