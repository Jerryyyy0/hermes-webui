"""Downstream paths and fixed parameter defaults."""

from __future__ import annotations

API_PREFIX = "/knowledge_base"

DOWNSTREAM_PATHS: dict[str, str] = {
    "list": "list_ps_knowledge_bases",
    "joined": "user_joined_shkbs",
    "create": "create_ps_kb",
    "info": "show_ps_kb_info",
    "edit": "edit_kb_information",
    "delete": "delete_ps_kb",
    "available": "available_shkbs",
    "apply_join": "apply_join_shkb",
    "members": "get_user_inshkb",
    "documents": "list_knowledge_bases_details",
    "upload_docs": "upload_docs",
    "update_docs": "update_docs",
    "delete_docs": "delete_docs",
    "show_pdf": "show_pdf",
    "search_docs": "search_docs",
    "search_docs_xcore": "search_docs_xcore",
    "creater_handle_application": "creater_handle_application",
    "get_joinkb_applications": "get_joinkb_applications",
    "get_user_messages": "get_user_messages",
    "mark_message_read": "mark_message_read",
    "user_exit_shkb": "user_exit_shkb",
    "remove_from_myshkb": "remove_from_myshkb",
    "delete_readed_message": "delete_readed_message",
    "download_doc": "download_doc",
}

WEBUI_ROUTE_PREFIX = "/api/integration/knowledge_base/"

PASSTHROUGH_ROUTES: set[str] = {
    "creater_handle_application",
    "get_joinkb_applications",
    "get_user_messages",
    "mark_message_read",
    "user_exit_shkb",
    "remove_from_myshkb",
    "delete_readed_message",
    "download_doc",
}

BINARY_PASSTHROUGH_ROUTES: set[str] = {
    "download_doc",
}

VS_TYPE = "faiss"
EMBED_MODEL = "bce-base"
ICON_TYPE = 1
PUBLICATION_DATE = "1"
DELETE_CONTENT = True
NOT_REFRESH_VS_CACHE = False
DEFAULT_CHUNK_SIZE = "500"
DEFAULT_CHUNK_OVERLAP = "50"
DEFAULT_LOCATION = "101"
DEFAULT_PAGE_SIZE = 15
DEFAULT_TOP_K = 3
DEFAULT_SCORE_THRESHOLD = 1.0

MAX_ARTIFACT_FILE_BYTES = 50 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ARTIFACT_COUNT = 20
