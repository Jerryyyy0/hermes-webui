"""HTTP handlers for profile memory content and its event timeline."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote

from api.helpers import bad, j
from integration.config import integration_enabled
from integration.profile_memory.service import (
    build_memory_overview,
    build_memory_timeline,
    update_memory,
)

_PREFIX = "/api/integration/profiles/"


def _route_parts(path: str) -> tuple[str, list[str]] | None:
    if not path.startswith(_PREFIX):
        return None
    parts = [unquote(part) for part in path[len(_PREFIX) :].split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, message: str, status: int) -> bool:
    bad(handler, message, status=status)
    return True


def _positive_int(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    route = _route_parts(parsed.path)
    if route is None:
        return False
    profile_name, parts = route
    try:
        if parts == ["memory"]:
            return _respond(handler, build_memory_overview(profile_name))
        if parts == ["memory", "timeline"]:
            query = parse_qs(parsed.query)
            page = _positive_int(query, "page", 1)
            page_size = _positive_int(query, "page_size", 20)
            return _respond(
                handler,
                build_memory_timeline(profile_name, page=page, page_size=page_size),
            )
    except ValueError as exc:
        return _respond_bad(handler, str(exc), 400)
    except LookupError as exc:
        return _respond_bad(handler, str(exc), 404)
    except (OSError, UnicodeError):
        return _respond_bad(handler, "读取记忆数据失败", 500)
    return False


def try_handle_put(handler, parsed, body) -> bool:
    if not integration_enabled():
        return False
    route = _route_parts(parsed.path)
    if route is None or route[1] != ["memory"]:
        return False
    if not isinstance(body, dict):
        return _respond_bad(handler, "请求体必须是 JSON 对象", 400)
    if "target" not in body or "content" not in body:
        return _respond_bad(handler, "缺少 target 或 content", 400)
    try:
        return _respond(
            handler,
            update_memory(route[0], body["target"], body["content"]),
        )
    except ValueError as exc:
        return _respond_bad(handler, str(exc), 400)
    except LookupError as exc:
        return _respond_bad(handler, str(exc), 404)
    except (OSError, UnicodeError):
        return _respond_bad(handler, "保存记忆数据失败", 500)
