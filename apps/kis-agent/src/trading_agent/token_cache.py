"""POSIX process-locked, owner-private cache for application-managed KIS tokens."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .contracts import AgentError


def cached_token(directory: Path, identity: str, issue) -> tuple[str, float]:
    try:
        import fcntl
    except ImportError:
        raise AgentError("Disk token locking requires POSIX; use a persistent single PaperSession instead") from None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise AgentError("Token cache directory must be owner-private (mode 0700)")
    path = directory / (identity + ".json")
    descriptor = os.open(directory / (identity + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            if path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
                raise AgentError("Token cache permissions/size are invalid")
            try:
                raw = json.loads(path.read_text())
                token, expiry = raw["token"], float(raw["expires_at"])
                if isinstance(token, str) and token and expiry > time.time() + 60:
                    return token, expiry
            except (ValueError, KeyError, TypeError):
                pass  # A corrupt/expired private cache can be replaced by one issuance.
        token, lifetime = issue()
        expiry = time.time() + lifetime
        temporary_fd, temporary_path = tempfile.mkstemp(prefix="token-", dir=directory)
        try:
            with os.fdopen(temporary_fd, "w") as handle:
                json.dump({"token": token, "expires_at": expiry}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        return token, expiry
