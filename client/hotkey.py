"""Global hotkey listener wrapper around pynput.

Lets the user configure a launch/toggle key combination. The combo string uses
pynput's GlobalHotKeys syntax, e.g. "<ctrl>+<shift>+1".
"""
from __future__ import annotations

from typing import Callable

from pynput import keyboard


class HotkeyManager:
    def __init__(self):
        self._listener: keyboard.GlobalHotKeys | None = None
        self._combo = ""
        self._callback: Callable[[], None] | None = None

    @property
    def combo(self) -> str:
        return self._combo

    def start(self, combo: str, callback: Callable[[], None]) -> None:
        """(Re)start listening for `combo`. Raises ValueError if combo invalid."""
        self.stop()
        self._combo = combo
        self._callback = callback
        # GlobalHotKeys validates the combo string on construction.
        self._listener = keyboard.GlobalHotKeys({combo: self._fire})
        self._listener.start()

    def _fire(self) -> None:
        if self._callback:
            self._callback()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
