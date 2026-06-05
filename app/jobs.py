"""Background-Job-Manager für langlaufende Tasks (Scan, Enrichment).

Jobs laufen in einem dedizierten Thread (Executor mit max_workers=1),
sodass der FastAPI-Request-Thread nie blockiert wird. Pro Job wird ein
Progress-Callback in den State geschrieben; der UI-Polling-Endpoint
liest diesen State und liefert JSON.

Garantien:
    - Maximal 1 Job gleichzeitig (zweiter Start → 409 Conflict)
    - Stop-Flag wird per should_stop-Callback in die Job-Funktion
      propagiert; die Job-Funktion MUSS den Callback in ihrer
      Schleife prüfen (siehe app.scanner / app.enrich)
    - Log-Zeilen werden in einen Ringpuffer (max. 200) geschrieben
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Optional


class Job:
    """Zustandsobjekt für einen laufenden/abgeschlossenen Job."""

    __slots__ = (
        "name", "status", "current", "total", "message",
        "log_lines", "result", "error", "started_at", "finished_at",
        "_stop_flag", "_lock",
    )

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.status: str = "pending"  # pending|running|done|failed|stopped
        self.current: int = 0
        self.total: int = 0
        self.message: str = ""
        self.log_lines: deque = deque(maxlen=200)
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

    def progress(self, current: int, total: int, message: str = "") -> None:
        with self._lock:
            self.current = current
            self.total = total
            if message:
                self.message = message
                self.log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def finish(self, status: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.finished_at = datetime.now().isoformat()
            if result is not None:
                self.result = result
            if error is not None:
                self.error = error
                self.log_lines.append(f"[ERROR] {error}")

    def request_stop(self) -> None:
        self._stop_flag.set()

    def should_stop(self) -> bool:
        return self._stop_flag.is_set()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "message": self.message,
                "progress_pct": round(self.current / self.total * 100, 1) if self.total else 0,
                "log_lines": list(self.log_lines),
                "result": self.result,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "running": self.status == "running",
            }


class JobManager:
    """Singleton-artiger Manager mit dediziertem Worker-Thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="bg-job"
        )
        self._current_job: Optional[Job] = None
        self._lock = threading.Lock()
        # History: fertige Jobs (max 10)
        self._history: deque = deque(maxlen=10)

    def start(self, name: str, fn: Callable[..., dict], **kwargs: Any) -> Job:
        """Startet einen Job. Wirft 409-Exception wenn schon einer läuft."""
        with self._lock:
            if self._current_job and self._current_job.status == "running":
                raise RuntimeError(
                    f"Job '{self._current_job.name}' läuft bereits – "
                    f"erst /api/jobs/stop aufrufen oder warten"
                )
            job = Job(name=name)
            self._current_job = job
            future: Future = self._executor.submit(self._run, job, fn, kwargs)
            future.add_done_callback(lambda f: self._on_done(job))
            return job

    def _run(self, job: Job, fn: Callable, kwargs: dict) -> None:
        job.status = "running"
        job.started_at = datetime.now().isoformat()
        job.progress(0, 0, f"Job '{job.name}' gestartet")
        try:
            # on_progress + should_stop in kwargs injizieren
            kwargs.setdefault("on_progress", job.progress)
            kwargs.setdefault("should_stop", job.should_stop)
            result = fn(**kwargs)
            if job.should_stop():
                job.finish("stopped", result=result)
            else:
                job.finish("done", result=result)
        except Exception:
            tb = traceback.format_exc()
            job.finish("failed", error=tb)
            # Fehler auch ins echte Log schreiben, damit er sichtbar bleibt
            print(f"[job:{job.name}] EXC:\n{tb}", flush=True)

    def _on_done(self, job: Job) -> None:
        with self._lock:
            self._history.append(job)
            if self._current_job is job:
                self._current_job = None

    def stop(self) -> bool:
        """Fordert den laufenden Job zum Stop auf. Liefert False wenn keiner läuft."""
        with self._lock:
            if not self._current_job or self._current_job.status != "running":
                return False
            self._current_job.request_stop()
            self._current_job.message = "Stop angefordert..."
            return True

    def current(self) -> Optional[Job]:
        with self._lock:
            return self._current_job

    def history(self) -> list[Job]:
        with self._lock:
            return list(self._history)


# Singleton
_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
