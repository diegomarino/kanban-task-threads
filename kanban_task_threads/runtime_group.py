"""Fan lifecycle events out to independent board runtimes."""

import time
from collections.abc import Sequence

from .runtime import Runtime


class RuntimeGroup:
    """A profile-local collection of independently owned board workers."""

    def __init__(self, runtimes: Sequence[Runtime]):
        self._runtimes = tuple(runtimes)

    def start(self, context=None) -> None:
        for runtime in self._runtimes:
            runtime.start(context.copy() if context is not None else None)

    def kick(self, context=None) -> None:
        for runtime in self._runtimes:
            runtime.kick(context.copy() if context is not None else None)

    def shutdown(self) -> None:
        for runtime in self._runtimes:
            runtime.request_stop()

        deadline = time.monotonic() + 10.0
        for runtime in self._runtimes:
            runtime.join(timeout=max(0.0, deadline - time.monotonic()))
