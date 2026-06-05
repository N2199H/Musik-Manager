"""NAS-Musik-Scanner: durchsucht ein Verzeichnis nach Audio-Dateien und
schreibt die ID3/Vorbis-Tags in die SQLite-Datenbank.

Funktionen:
    ensure_schema(conn)        -- legt das songs-Schema an
    scan_files(music_dir)     -- listet Audio-Dateien rekursiv
    extract_tags(filepath)     -- liest Tags (MP3/FLAC/Vorbis)
    run_scan(...)              -- Orchestrator mit Progress-Callback
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from mutagen import File as MutagenFile

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".wma", ".wav", ".aac"}

ProgressFn = Callable[[int, int, str], None]  # None erlaubt (Callable-arg-check unten)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Legt das songs-Schema (und die wichtigsten Indizes) an, falls noch nicht da."""
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT UNIQUE NOT NULL,
            filename TEXT,
            artist TEXT,
            title TEXT,
            album TEXT,
            genre TEXT,
            year TEXT,
            duration_sec REAL,
            bitrate_kbps INTEGER,
            filesize INTEGER,
            bpm REAL,
            energy REAL,
            valence REAL,
            danceability REAL,
            loudness REAL,
            key TEXT,
            mood TEXT,
            tags TEXT,
            spotify_id TEXT,
            lastfm_tags TEXT,
            analyzed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for idx in ("artist", "genre", "bpm", "mood"):
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_{idx} ON songs({idx})")
    conn.commit()


def scan_files(music_dir: str | Path) -> list[str]:
    """Listet alle Audio-Dateien rekursiv in ``music_dir``."""
    files: list[str] = []
    for root, _, filenames in os.walk(music_dir):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                files.append(os.path.join(root, fn))
    return files


def extract_tags(filepath: str) -> dict:
    """Liest ID3/Vorbis-Tags aus einer Audio-Datei.

    Liefert ein Dict mit Schlüsseln: filepath, filename, artist, title,
    album, genre, year, duration_sec, bitrate_kbps, filesize.
    Fehlende Felder werden als leerer String bzw. 0 zurückgegeben.
    """
    info: dict = {
        "filepath": filepath,
        "filename": os.path.basename(filepath),
        "artist": "",
        "title": "",
        "album": "",
        "genre": "",
        "year": "",
        "duration_sec": 0,
        "bitrate_kbps": 0,
        "filesize": 0,
    }
    try:
        info["filesize"] = os.path.getsize(filepath)
    except OSError:
        pass
    try:
        mf = MutagenFile(filepath)
    except Exception as e:  # kaputte Datei o.ä.
        print(f"  ⚠ Fehler bei {info['filename']}: {e}", file=sys.stderr)
        mf = None
    if mf is not None:
        try:
            info["duration_sec"] = round(mf.info.length, 1)
            if mf.info.bitrate:
                info["bitrate_kbps"] = int(mf.info.bitrate / 1000)
        except Exception:
            pass
        tags = getattr(mf, "tags", None)
        if tags is not None:
            # ID3 (MP3) – TPE1/TIT2/TALB/TCON/TDRC
            for src_key, dst in (("TPE1", "artist"), ("TPE2", "artist")):
                if src_key in tags and not info[dst]:
                    info[dst] = str(tags[src_key])
            for src_key, dst in (("TIT2", "title"),):
                if src_key in tags and not info[dst]:
                    info[dst] = str(tags[src_key])
            for src_key, dst in (("TALB", "album"),):
                if src_key in tags and not info[dst]:
                    info[dst] = str(tags[src_key])
            for src_key, dst in (("TCON", "genre"),):
                if src_key in tags and not info[dst]:
                    info[dst] = str(tags[src_key])
            for src_key in ("TDRC", "TYER"):
                if src_key in tags and not info["year"]:
                    info["year"] = str(tags[src_key])
            # FLAC / Vorbis – case-insensitive
            for k, v in tags.items():
                k_lower = k.lower()
                if k_lower == "artist" and not info["artist"]:
                    info["artist"] = str(v)
                elif k_lower == "title" and not info["title"]:
                    info["title"] = str(v)
                elif k_lower == "album" and not info["album"]:
                    info["album"] = str(v)
                elif k_lower == "genre" and not info["genre"]:
                    info["genre"] = str(v)
                elif k_lower == "date" and not info["year"]:
                    info["year"] = str(v)
    # Fallback: Dateiname als Titel
    if not info["title"]:
        info["title"] = os.path.splitext(info["filename"])[0]
    return info


def _insert_song(conn: sqlite3.Connection, song_info: dict) -> bool:
    """INSERT OR REPLACE. Liefert True bei Erfolg."""
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO songs
                (filepath, filename, artist, title, album, genre, year,
                 duration_sec, bitrate_kbps, filesize)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song_info["filepath"], song_info["filename"],
                song_info["artist"], song_info["title"],
                song_info["album"], song_info["genre"], song_info["year"],
                song_info["duration_sec"], song_info["bitrate_kbps"],
                song_info["filesize"],
            ),
        )
        return True
    except sqlite3.Error as e:
        print(f"  ⚠ DB-Fehler: {e}", file=sys.stderr)
        return False


def run_scan(
    music_dir: str | Path,
    db_path: str | Path,
    force_rescan: bool = False,
    on_progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:  # type: ignore[type-arg]
    """Scannt ``music_dir`` und schreibt/aktualisiert die songs-Tabelle.

    Args:
        music_dir: Verzeichnis mit Audio-Dateien (z.B. /tmp/nas-musik)
        db_path: Pfad zur SQLite-Datei
        force_rescan: Wenn True, werden auch bereits vorhandene Dateien
            erneut eingelesen (UPDATE statt SKIP)
        on_progress: Optionaler Callback (current, total, message).
            Wird alle 50 Songs + am Ende aufgerufen.
        should_stop: Optionaler Callable, der True liefert wenn der Job
            abgebrochen werden soll (z.B. UI-Stop-Button).

    Returns:
        Dict mit Statistik: scanned, inserted, updated, skipped, errors, stopped.
    """
    music_dir = Path(music_dir)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM songs")
        existing = cur.fetchone()[0]
        files = scan_files(music_dir)
        if on_progress:
            on_progress(0, len(files), f"{existing} Songs vorhanden, {len(files)} Dateien gefunden")

        stats = {"scanned": 0, "inserted": 0, "updated": 0, "skipped": 0, "errors": 0, "stopped": False}
        for i, filepath in enumerate(files):
            if should_stop and should_stop():
                stats["stopped"] = True
                break
            if not force_rescan:
                cur.execute("SELECT id FROM songs WHERE filepath = ?", (filepath,))
                if cur.fetchone():
                    stats["skipped"] += 1
                    continue
            try:
                song_info = extract_tags(filepath)
                # INSERT OR REPLACE -> wir zählen inserted/updated über rowcount
                cur2 = conn.cursor()
                if force_rescan:
                    cur2.execute("SELECT id FROM songs WHERE filepath = ?", (filepath,))
                    already = cur2.fetchone()
                else:
                    already = None
                if _insert_song(conn, song_info):
                    if already:
                        stats["updated"] += 1
                    else:
                        stats["inserted"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"  ⚠ Fehler bei {filepath}: {e}", file=sys.stderr)
            stats["scanned"] += 1
            if (i + 1) % 50 == 0 and on_progress:
                on_progress(i + 1, len(files), f"{i+1}/{len(files)} verarbeitet")
        conn.commit()
        if on_progress:
            on_progress(len(files), len(files), "Scan abgeschlossen")
        return stats
    finally:
        conn.close()
