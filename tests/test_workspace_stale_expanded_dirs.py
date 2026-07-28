"""Regression coverage for deleted persisted workspace tree directories."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str, *, async_fn: bool = False) -> str:
    marker = f"{'async ' if async_fn else ''}function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name}() function must exist"
    brace = source.find("{", source.find(")", start))
    assert brace > start, f"{name}() function body must start"
    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx in range(brace, len(source)):
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"could not extract {name}()")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


@node_test
def test_root_load_prunes_missing_persisted_expanded_directories_only():
    ws_expand_key = _extract_function(WORKSPACE_JS, "_wsExpandKey")
    save_expanded = _extract_function(WORKSPACE_JS, "_saveExpandedDirs")
    restore_expanded = _extract_function(WORKSPACE_JS, "_restoreExpandedDirs")
    load_dir = _extract_function(WORKSPACE_JS, "loadDir", async_fn=True)
    script = textwrap.dedent(
        f"""
        const stored = new Map([[
          'hermes-webui-expanded:/workspace',
          JSON.stringify(['tt', 'notes/github-trending', 'still-here']),
        ]]);
        const requested = [];
        const S = {{
          session: {{session_id: 'fresh-session', workspace: '/workspace'}},
          _dirCache: {{}},
        }};
        let _workspacePanelActiveTab = 'files';
        let _previewDirty = false;
        global.localStorage = {{
          getItem: key => stored.has(key) ? stored.get(key) : null,
          setItem: (key, value) => stored.set(key, value),
        }};
        function _isBrowserPreviewOpen() {{ return false; }}
        function renderBreadcrumb() {{}}
        function renderFileTree() {{}}
        function clearPreview() {{}}
        function _refreshGitBadge() {{}}
        function showConfirmDialog() {{ return Promise.resolve(true); }}
        function t(key) {{ return key; }}
        function api(url) {{
          requested.push(url);
          if (url.includes('path=.')) return Promise.resolve({{entries: []}});
          if (url.includes('path=still-here')) return Promise.resolve({{entries: [{{name: 'file.txt'}}]}});
          const error = new Error('Path does not exist');
          error.status = 404;
          return Promise.reject(error);
        }}
        {ws_expand_key}
        {save_expanded}
        {restore_expanded}
        {load_dir}
        loadDir('.').then(() => {{
          process.stdout.write(JSON.stringify({{
            stored: JSON.parse(stored.get('hermes-webui-expanded:/workspace')),
            expanded: [...S._expandedDirs],
            cache: S._dirCache,
            requested,
          }}));
        }}).catch(error => {{ console.error(error.stack || error); process.exit(1); }});
        """
    )

    result = _run_node(script)

    assert result["stored"] == ["still-here"]
    assert result["expanded"] == ["still-here"]
    assert "tt" not in result["cache"]
    assert "notes/github-trending" not in result["cache"]
    assert result["cache"]["still-here"] == [{"name": "file.txt"}]
    assert any("path=tt" in url for url in result["requested"])
    assert any("path=notes%2Fgithub-trending" in url for url in result["requested"])
