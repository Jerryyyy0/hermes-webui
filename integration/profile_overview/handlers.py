"""HTTP handlers for profile overview and fixed raw profile documents."""

from __future__ import annotations

from urllib.parse import unquote

from api.helpers import bad, j
from integration.config import integration_enabled
from integration.profile_overview.service import (
    build_overview,
    list_raw_files,
    raw_file_path,
    resolve_profile,
)

_PREFIX = "/api/integration/profiles/"


def _route_parts(path: str) -> tuple[str, list[str]] | None:
    if not path.startswith(_PREFIX):
        return None
    parts = [unquote(part) for part in path[len(_PREFIX) :].split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _binary(handler, content: bytes, filename: str) -> bool:
    safe_filename = filename.replace('"', "")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/markdown; charset=utf-8")
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(content)
    return True


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, message: str, status: int) -> bool:
    bad(handler, message, status=status)
    return True


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    route = _route_parts(parsed.path)
    if route is None:
        return False
    profile_name, parts = route

    try:
        if parts == ["overview"]:
            return _respond(handler, build_overview(profile_name))

        profile, home = resolve_profile(profile_name)
        resolved_name = str(profile.get("name") or profile_name)
        if parts == ["raw_files"]:
            return _respond(handler, {"profile": resolved_name, "files": list_raw_files(home)})
        if len(parts) == 2 and parts[0] == "raw_files":
            filename, path = raw_file_path(home, parts[1])
            if not path.is_file():
                return _respond_bad(handler, "档案文件不存在", 404)
            return _respond(
                handler,
                {
                    "profile": resolved_name,
                    "file_type": parts[1].lower(),
                    "filename": filename,
                    "content": path.read_text(encoding="utf-8"),
                },
            )
        if len(parts) == 3 and parts[0] == "raw_files" and parts[2] == "download":
            filename, path = raw_file_path(home, parts[1])
            if not path.is_file():
                return _respond_bad(handler, "档案文件不存在", 404)
            return _binary(handler, path.read_bytes(), filename)
    except ValueError as exc:
        return _respond_bad(handler, str(exc), 400)
    except LookupError as exc:
        return _respond_bad(handler, str(exc), 404)
    except (OSError, UnicodeError):
        return _respond_bad(handler, "读取档案文件失败", 500)
    return False
