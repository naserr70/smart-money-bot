"""Small POSIX process lock used to prevent duplicate background workers."""

from __future__ import annotations

import logging
import os
from typing import Optional, TextIO

log = logging.getLogger("smart_money_bot.process_lock")


def acquire(path: str) -> Optional[TextIO]:
    """Acquire a non-blocking process lock; return the open handle if owner."""
    try:
        import fcntl
    except ImportError:
        log.warning("PROCESS LOCK UNAVAILABLE | non-POSIX platform")
        return None

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle
