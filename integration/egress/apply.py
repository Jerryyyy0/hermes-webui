from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    rules_path: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None


def _atomic_write_text(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def apply_iptables_rules(rules_content: str, *, rules_path: str) -> ApplyResult:
    target = Path(rules_path)
    try:
        _atomic_write_text(target, rules_content)
    except Exception as exc:
        return ApplyResult(ok=False, rules_path=str(target), error=str(exc))

    try:
        proc = subprocess.run(
            ["iptables-restore", "-f", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return ApplyResult(
                ok=False,
                rules_path=str(target),
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                returncode=proc.returncode,
                error="iptables-restore failed",
            )
        return ApplyResult(
            ok=True,
            rules_path=str(target),
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
        )
    except FileNotFoundError:
        return ApplyResult(ok=False, rules_path=str(target), error="iptables-restore not found")
    except Exception as exc:
        return ApplyResult(ok=False, rules_path=str(target), error=str(exc))


def get_current_iptables_rules() -> ApplyResult:
    try:
        proc = subprocess.run(
            ["iptables", "-L", "-n"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return ApplyResult(
                ok=False,
                rules_path="",
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                returncode=proc.returncode,
                error="iptables -L failed",
            )
        return ApplyResult(
            ok=True,
            rules_path="",
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
        )
    except FileNotFoundError:
        return ApplyResult(ok=False, rules_path="", error="iptables not found")
    except Exception as exc:
        return ApplyResult(ok=False, rules_path="", error=str(exc))

