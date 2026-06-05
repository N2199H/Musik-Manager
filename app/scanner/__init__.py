"""NAS-Musik-Scanner

Scannt ein Verzeichnis (typischerweise ein gemountetes NAS-Share), liest
ID3/Vorbis-Tags aus Audio-Dateien und schreibt die Ergebnisse in die
Musik-Manager SQLite-Datenbank.

Public API:
    from app.scanner import run_scan
    run_scan(music_dir=Path("/tmp/nas-musik"), db_path=Path("musik.db"),
             on_progress=lambda cur, total, msg: ...)
"""
from .service import run_scan, ensure_schema

__all__ = ["run_scan", "ensure_schema"]
