"""Secret files (YouTube sign-in token, OAuth client, alert topic) readable only by the current user.

A project on a secondary drive (e.g. P:\\) inherits ACLs that typically let every local account read its files,
and the YouTube token is enough to upload to the owner's channel. So secrets are written into a file that is
locked to the current user *before* any secret byte goes in, then moved into place; files that already exist
are locked down at startup.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _current_user_sid() -> str | None:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.strip()
    sid = out.rsplit(",", 1)[-1].strip().strip('"') if out else ""
    return sid if sid.startswith("S-1-") else None


def restrict_to_current_user(path: Path) -> bool:
    """Only the current user (full control) may access `path`; inherited permissions are removed.
    Returns False (and logs) if that couldn't be done: the caller still works, just less private."""
    try:
        if sys.platform != "win32":
            os.chmod(path, 0o600)
            return True
        sid = _current_user_sid()
        if not sid:
            raise RuntimeError("couldn't determine the current user")
        # /reset drops explicit entries (a browser-saved file can carry "Users: read" explicitly), then
        # /inheritance:r removes the inherited ones, leaving only the grant to us.
        for args in (["/reset"], ["/inheritance:r", "/grant:r", f"*{sid}:(F)"]):
            p = subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, creationflags=NO_WINDOW)
            if p.returncode != 0:
                raise RuntimeError((p.stdout + p.stderr).strip()[:200])
        return True
    except Exception as e:
        log.warning("couldn't restrict %s to the current user: %s", path.name, e)
        return False


def write_private(path: Path, text: str) -> None:
    """Write a secret so that it is never readable by other accounts, not even for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))   # empty, owner-only on POSIX
    restrict_to_current_user(tmp)                                          # owner-only on Windows
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)       # the moved file keeps the restricted permissions


def secure_existing(paths: list[Path]) -> None:
    """Lock down secret files written before this protection existed (or saved by hand)."""
    for p in paths:
        if p.exists():
            restrict_to_current_user(p)
