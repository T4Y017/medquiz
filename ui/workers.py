"""Arayüzü dondurmadan arka planda iş çalıştırma yardımcıları."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    finished = Signal()


class Task(QRunnable):
    """fn(progress, task) çalıştırır. progress(x) arayüze ilerleme gönderir; task.cancelled iptali bildirir."""

    def __init__(self, fn: Callable[..., Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.cancelled = False
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self.cancelled = True

    def _emit(self, sig, *args) -> None:
        # Uygulama kapanırken sinyal nesnesi silinmiş olabilir; bu durumda sessizce geç
        try:
            sig.emit(*args)
        except RuntimeError:
            self.cancelled = True

    def run(self) -> None:  # worker thread
        try:
            res = self.fn(lambda x: self._emit(self.signals.progress, x), self)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._emit(self.signals.error, str(e) or e.__class__.__name__)
        else:
            self._emit(self.signals.result, res)
        finally:
            self._emit(self.signals.finished)


class Runner:
    """Görevleri çalıştırır ve bitene kadar referanslarını tutar."""

    def __init__(self):
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(4, self.pool.maxThreadCount()))
        self._live: set[Task] = set()

    def start(self, fn: Callable[..., Any], on_result: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None,
              on_progress: Callable[[Any], None] | None = None,
              on_finished: Callable[[], None] | None = None) -> Task:
        t = Task(fn)
        if on_result:
            t.signals.result.connect(on_result)
        if on_error:
            t.signals.error.connect(on_error)
        if on_progress:
            t.signals.progress.connect(on_progress)
        if on_finished:
            t.signals.finished.connect(on_finished)
        t.signals.finished.connect(lambda: self._live.discard(t))
        self._live.add(t)
        self.pool.start(t)
        return t

    def wait_all(self, msecs: int = 3000) -> None:
        for t in list(self._live):
            t.cancel()
        self.pool.waitForDone(msecs)
