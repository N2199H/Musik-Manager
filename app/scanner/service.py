"""Service-Layer für den Scanner (eigentliche run-Funktion)."""
from pathlib import Path
from typing import Callable, Optional

from .nas import run_scan as _run_scan, ensure_schema  # re-export

__all__ = ["run_scan", "ensure_schema"]


def run_scan(
    music_dir: str | Path,
    db_path: str | Path,
    force_rescan: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Wrapper für ``scanner.nas.run_scan`` – kann später um Caching, Locking,
    Worker-Pool o.ä. erweitert werden, ohne dass API-Endpoints angefasst werden.
    """
    return _run_scan(
        music_dir=music_dir,
        db_path=db_path,
        force_rescan=force_rescan,
        on_progress=on_progress,
        should_stop=should_stop,
    )
