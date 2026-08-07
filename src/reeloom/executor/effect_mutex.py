from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator

_LOCK = threading.Lock()


@contextmanager
def effect_mutex() -> Iterator[None]:
    """Serialize checked filesystem effects in this service process."""

    with _LOCK:
        yield
