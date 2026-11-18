"""Background package-generation worker.

A single daemon thread polls the database. Builds run one at a time, but a
slow or failing build for one cohort only blocks the queue, never the HTTP
download endpoint: other cohorts' already published packages remain
downloadable throughout.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config
from .services import StaleRevisionError, claim_pending_package, finalize_package, mark_failed

log = logging.getLogger("deid.worker")


class PackageWorker:
    def __init__(self, interval: float | None = None) -> None:
        self.interval = config.WORKER_INTERVAL if interval is None else interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="package-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_once(self) -> bool:
        """Process at most one due package. Returns True if one was handled."""
        claim = claim_pending_package()
        if claim is None:
            return False
        cohort_id, revision_label = claim
        log.info("building package cohort=%s revision=%s", cohort_id, revision_label)
        try:
            finalize_package(cohort_id, revision_label)
            log.info("package published cohort=%s revision=%s", cohort_id, revision_label)
        except StaleRevisionError as exc:
            log.warning("stale build discarded: %s", exc)
        except Exception as exc:  # noqa: BLE001 - worker must never die
            log.exception("package build failed cohort=%s revision=%s", cohort_id, revision_label)
            try:
                mark_failed(cohort_id, revision_label, f"{type(exc).__name__}: {exc}")
            except Exception:  # pragma: no cover - defensive
                log.exception("could not record failure")
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                handled = self.run_once()
            except Exception:  # pragma: no cover - defensive
                log.exception("worker tick crashed")
                handled = False
            # Drain the queue when busy; sleep when idle.
            if not handled:
                self._stop.wait(self.interval)
