"""Run blocking work (network calls) off the Tk main thread safely.

Tkinter is not thread-safe: widgets may only be touched from the thread that
created the root window. So a worker thread does the blocking call, pushes the
result onto a queue, and the main thread drains that queue from a periodic
`after()` pump — meaning every callback runs on the main thread.

This is the Tk equivalent of the marshalling that the Qt version needed, and it
removes the whole class of "touched a widget from a worker thread" crashes.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Any, Callable

log = logging.getLogger("autotyper.async")

_PUMP_MS = 40


class TaskRunner:
    """Owns the result queue and the main-thread pump."""

    def __init__(self, root: tk.Misc):
        self._root = root
        self._q: "queue.Queue[tuple[Callable, Any]]" = queue.Queue()
        self._running = True
        self._pump()

    def _pump(self) -> None:
        """Drain completed callbacks on the main thread."""
        try:
            while True:
                cb, payload = self._q.get_nowait()
                try:
                    cb(payload)
                except Exception:
                    log.exception("async callback raised")
        except queue.Empty:
            pass
        if self._running:
            self._root.after(_PUMP_MS, self._pump)

    def stop(self) -> None:
        self._running = False

    def run(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_fail: Callable[[str], None] | None = None,
        name: str = "task",
    ) -> None:
        """Execute `fn` on a daemon thread; deliver the outcome on the main thread."""

        def worker() -> None:
            log.debug("task %s: start", name)
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                msg = getattr(e, "message", None) or str(e)
                log.warning("task %s: failed: %s", name, msg)
                if on_fail:
                    self._q.put((on_fail, msg))
                return
            log.debug("task %s: ok", name)
            if on_done:
                self._q.put((on_done, result))

        threading.Thread(target=worker, name=f"task-{name}", daemon=True).start()

    def post(self, fn: Callable[[], None]) -> None:
        """Schedule `fn` to run on the main thread (from any thread)."""
        self._q.put((lambda _ignored: fn(), None))
