"""Service-Layer für MusicBrainz-Enrichment."""
from pathlib import Path
from typing import Callable, Optional

from .musicbrainz import run_enrich_genres as _run_genres
from .musicbrainz import run_enrich_album_year as _run_album_year

__all__ = ["run_enrich_genres", "run_enrich_album_year"]


def run_enrich_genres(
    db_path: str | Path,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    return _run_genres(db_path=db_path, on_progress=on_progress, should_stop=should_stop)


def run_enrich_album_year(
    db_path: str | Path,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    return _run_album_year(db_path=db_path, on_progress=on_progress, should_stop=should_stop)
