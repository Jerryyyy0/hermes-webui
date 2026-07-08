#!/usr/bin/env python3
"""
获取浏览器当前活动标签页的 URL
支持 macOS 上的 Safari、Chrome、Firefox、Edge 等浏览器

用法:
    python3 get_browser_url.py              # 自动检测
    python3 get_browser_url.py chrome       # 指定浏览器
    python3 get_browser_url.py --all        # 获取所有浏览器
    python3 get_browser_url.py --json       # 仅返回 URL 文本
"""

import subprocess
import json
import sys
from typing import Optional


def _run_applescript(script: str, timeout: float = 3.0) -> str:
    """运行 AppleScript，带全局超时"""
    wrapped = f'''
    with timeout of {int(timeout)} seconds
        {script}
    end timeout
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", wrapped],
            capture_output=True, text=True, timeout=timeout + 2
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_safari_url() -> Optional[str]:
    url = _run_applescript('''
    tell application "Safari"
        if it is running then
            try
                get URL of front document
            on error
                return ""
            end try
        else
            return ""
        end if
    end tell
    ''')
    return url if url else None


def get_chrome_url() -> Optional[str]:
    url = _run_applescript('''
    tell application "Google Chrome"
        if it is running then
            try
                get URL of active tab of front window
            on error
                return ""
            end try
        else
            return ""
        end if
    end tell
    ''')
    return url if url else None


def get_edge_url() -> Optional[str]:
    url = _run_applescript('''
    tell application "Microsoft Edge"
        if it is running then
            try
                get URL of active tab of front window
            on error
                return ""
            end try
        else
            return ""
        end if
    end tell
    ''')
    return url if url else None


def get_firefox_url() -> Optional[str]:
    url = _run_applescript('''
    tell application "Firefox"
        if it is running then
            try
                get URL of active tab of front window
            on error
                return ""
            end try
        else
            return ""
        end if
    end tell
    ''')
    return url if url else None


BROWSERS = {
    "safari": get_safari_url,
    "chrome": get_chrome_url,
    "firefox": get_firefox_url,
    "edge": get_edge_url,
}


def get_browser_url(browser: str = "auto") -> Optional[dict]:
    """
    获取指定浏览器的当前 URL
    """
    if browser == "auto":
        for name, func in BROWSERS.items():
            try:
                url = func()
                if url:
                    return {"browser": name, "url": url}
            except Exception:
                continue
        return None
    elif browser in BROWSERS:
        try:
            url = BROWSERS[browser]()
            if url:
                return {"browser": browser, "url": url}
        except Exception:
            pass
        return None
    else:
        raise ValueError(f"不支持的浏览器: {browser}，可选: {list(BROWSERS.keys())}")


def get_all_browser_urls() -> list[dict]:
    results = []
    for name, func in BROWSERS.items():
        try:
            url = func()
            if url:
                results.append({"browser": name, "url": url})
        except Exception:
            continue
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        results = get_all_browser_urls()
        if results:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            print(json.dumps([], ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--json":
        result = get_browser_url("auto")
        if result:
            print(result["url"])
        else:
            sys.exit(1)
    elif len(sys.argv) > 1:
        result = get_browser_url(sys.argv[1])
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"浏览器 {sys.argv[1]} 未运行或无法获取 URL", file=sys.stderr)
            sys.exit(1)
    else:
        result = get_browser_url("auto")
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"error": "未检测到正在运行的浏览器或无法获取 URL"}, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)

