"""Allowed downstream route names and fixed parameter defaults."""

from __future__ import annotations

API_PREFIX = "/knowledge_base"

DOWNSTREAM_ROUTE_NAMES: frozenset[str] = frozenset({
    "list_ps_knowledge_bases",
    "list_qa_knowledge_bases",
    "user_joined_shkbs",
    "create_ps_kb",
    "show_ps_kb_info",
    "edit_kb_information",
    "delete_ps_kb",
    "available_shkbs",
    "apply_join_shkb",
    "get_user_inshkb",
    "list_knowledge_bases_details",
    "upload_docs",
    "update_docs",
    "delete_docs",
    "show_pdf",
    "search_docs",
    "search_docs_xcore",
    "creater_handle_application",
    "get_joinkb_applications",
    "get_user_messages",
    "mark_message_read",
    "user_exit_shkb",
    "remove_from_myshkb",
    "delete_readed_message",
    "download_doc",
})

WEBUI_ROUTE_PREFIX = "/api/integration/knowledge_base/"

BINARY_PASSTHROUGH_ROUTES: set[str] = {
    "show_pdf",
    "download_doc",
}

DEFAULT_CHUNK_SIZE = "500"
DEFAULT_CHUNK_OVERLAP = "50"

MAX_ARTIFACT_FILE_BYTES = 50 * 1024 * 1024
MAX_ARTIFACT_COUNT = 20
