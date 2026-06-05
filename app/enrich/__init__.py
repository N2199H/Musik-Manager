"""MusicBrainz-Enrichment für die songs-Tabelle.

Zwei Modi:
    run_enrich_genres(...)         -- Genre pro Artist (1 req/Artist)
    run_enrich_album_year(...)     -- Album + Year pro Song (1 req/Song)

MusicBrainz-Rate-Limit: max 1 Request/Sekunde. Bei ~10k Songs dauert
Schritt 2 ca. 3 Stunden – als Background-Job ausführen.

Public API:
    from app.enrich import run_enrich_genres, run_enrich_album_year
"""
from .service import run_enrich_genres, run_enrich_album_year

__all__ = ["run_enrich_genres", "run_enrich_album_year"]
