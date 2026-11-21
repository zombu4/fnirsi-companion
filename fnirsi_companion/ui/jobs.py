"""Jobs: every blocking device call runs in a thread of a QThreadPool, never on the GUI thread.

A job is a function fn(progress) -> result. progress(fraction, text) may be called from the job's
thread. The result, the error and the progress reach the GUI thread as queued Qt signals of a small
QObject that is created in the GUI thread for each job, so the callbacks given to submit() always
run in the GUI thread.

Exclusive jobs are those that hold the meter's port (backup, update, restore, calibrate, screenshot,
saves; DESIGN 13.11): while one runs, `busy` is True, busy_changed(True) has been emitted and the
window blocks the other actions and the switch of the active meter (DESIGN 13.13). Only one
exclusive job runs at a time; submit() refuses a second one.

Cancellation (DESIGN 3): every job has a devices.base.CancelToken, made the cancel token of the
job's thread while it runs. The device methods open the phases in which the job may stop between
requests; cancel() asks the exclusive job to stop and is refused outside such a phase.
cancellable_changed(job, allowed) reports the phases in the GUI thread; Job.last_text is the
job's latest progress text (what it does or waits for now).
"""

from __future__ import annotations

import threading
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot

from ..devices.base import CancelToken, cancel_scope


class JobRefused(Exception):
    """An exclusive job was asked for while another one holds the port."""


class _Relay(QObject):
    """Lives in the GUI thread; the job thread emits its *_sig signals, the queued connections to
    the slots below deliver them in the GUI thread."""

    result_sig = Signal(object)
    error_sig = Signal(object)
    progress_sig = Signal(float, str)
    cancel_sig = Signal(bool)

    def __init__(self, job: "Job"):
        super().__init__()
        self.job = job
        self.result_sig.connect(self._result, Qt.QueuedConnection)
        self.error_sig.connect(self._error, Qt.QueuedConnection)
        self.progress_sig.connect(self._progress, Qt.QueuedConnection)
        self.cancel_sig.connect(self._cancel, Qt.QueuedConnection)

    @Slot(bool)
    def _cancel(self, allowed):
        if not self.job.done.is_set():
            self.job.runner.cancellable_changed.emit(self.job, allowed)

    @Slot(object)
    def _result(self, value):
        self.job._finish(True, value)

    @Slot(object)
    def _error(self, exc):
        self.job._finish(False, exc)

    @Slot(float, str)
    def _progress(self, frac, text):
        if text:
            self.job.last_text = text
        if self.job.on_progress is not None:
            self.job.on_progress(frac, text)


class Job(QRunnable):
    """One job; created by JobRunner.submit()."""

    def __init__(self, runner: "JobRunner", fn: Callable, what: str, exclusive: bool,
                 on_result, on_error, on_progress):
        super().__init__()
        self.setAutoDelete(False)
        self.runner, self.fn, self.what, self.exclusive = runner, fn, what, exclusive
        self.on_result, self.on_error, self.on_progress = on_result, on_error, on_progress
        self.done = threading.Event()
        self.ok: bool | None = None
        self.value = None
        self.trace = ""
        self.last_text = ""
        self.cancel = CancelToken()
        self._relay = _Relay(self)

    def run(self) -> None:          # in the pool thread
        relay = self._relay

        def emit(sig, *args):
            try:
                sig.emit(*args)
            except RuntimeError:     # the window that waits for it is gone (closed)
                pass

        self.cancel.changed = lambda allowed: emit(relay.cancel_sig, bool(allowed))
        try:
            with cancel_scope(self.cancel):
                value = self.fn(lambda frac, text="": emit(relay.progress_sig, float(frac),
                                                           str(text)))
        except BaseException as e:   # every failure goes back to the GUI as the job's error
            self.trace = traceback.format_exc()
            emit(relay.error_sig, e)
        else:
            emit(relay.result_sig, value)

    def _finish(self, ok: bool, value) -> None:   # in the GUI thread
        self.ok, self.value = ok, value
        self.runner._finishing = self
        try:
            if ok:
                if self.on_result is not None:
                    self.on_result(value)
            elif self.on_error is not None:
                self.on_error(value)
            else:
                self.runner.failed.emit(self.what, value)
        finally:
            self.runner._finishing = None
            self.runner._ended(self)
            self.done.set()
            self.runner.finished.emit(self)


class JobRunner(QObject):
    """Submits jobs to a thread pool and tracks the exclusive one."""

    busy_changed = Signal(bool)         # an exclusive job started (True) / ended (False)
    cancellable_changed = Signal(object, bool)   # Job, True while it may be cancelled
    failed = Signal(str, object)        # what, exception: a job without its own error handler
    started = Signal(object)            # Job
    finished = Signal(object)           # Job, after its callbacks

    def __init__(self, pool: QThreadPool | None = None):
        super().__init__()
        self.pool = pool or QThreadPool()
        if pool is None:
            self.pool.setMaxThreadCount(8)
        self.exclusive_job: Job | None = None
        self.running: set[Job] = set()
        self._finishing: Job | None = None     # the job whose callbacks run now

    @property
    def busy(self) -> bool:
        """True while an exclusive job holds the port."""
        return self.exclusive_job is not None

    def submit(self, fn: Callable, *, what: str, exclusive: bool = False,
               on_result=None, on_error=None, on_progress=None) -> Job:
        """Starts fn(progress) in the pool. Raises JobRefused for an exclusive job while another
        exclusive job runs. A callback of the exclusive job that ends may start the next one: the
        port passes on without a busy_changed(False) in between. Call from the GUI thread."""
        handover = exclusive and self.exclusive_job is not None \
            and self.exclusive_job is self._finishing
        if exclusive and self.exclusive_job is not None and not handover:
            raise JobRefused("%s waits: %s holds the meter's port" % (what, self.exclusive_job.what))
        job = Job(self, fn, what, exclusive, on_result, on_error, on_progress)
        self.running.add(job)
        if exclusive:
            self.exclusive_job = job
            if not handover:
                self.busy_changed.emit(True)
        self.started.emit(job)
        self.pool.start(job)
        return job

    def cancel(self) -> bool:
        """Asks the exclusive job to stop at its next cancel point. False when no exclusive job
        runs or it is at a step that must run to its end (Job.cancel.why_not says which)."""
        job = self.exclusive_job
        return job is not None and job.cancel.request()

    def _ended(self, job: Job) -> None:
        self.running.discard(job)
        if job is self.exclusive_job:
            self.exclusive_job = None
            self.busy_changed.emit(False)

    def wait_all(self, msecs: int = -1) -> bool:
        """Waits for the pool's threads (used when the window closes)."""
        return self.pool.waitForDone(msecs)
