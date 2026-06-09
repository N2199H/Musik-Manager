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
    Plus 'mm_song_id': Integer aus TXXX:MUSIK_MANAGER_SONG_ID (oder None)
    wenn vorhanden — wird vom Scanner für Rename/Move-Detection genutzt.
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
        "mm_song_id": None,  # TXXX:MUSIK_MANAGER_SONG_ID wenn vorhanden
    }
    try:
        info["filesize"] = os.path.getsize(filepath)
    except OSError:
        pass
    # MutagenFile wirft bei kaputten Files 'can't sync to MPEG frame' o.ä.
    # Wenn es fehlschlaegt, lassen wir den File aus der DB raus — der File
    # ist ohnehin nicht abspielbar. Caller zaehlt das als error + sample.
    # length==0 lassen wir durch (manche Files haben ID3 ohne Audio-Stream,
    # oder das ist ein Edge-Case den wir nicht verschlimmern wollen).
    # Original-Exception weiterwerfen — nicht in RuntimeError verpacken,
    # damit der Caller den echten Typ (HeaderNotFoundError, ...) sieht.
    mf = MutagenFile(filepath)
    if mf is None:
        raise ValueError("kein Audio-Format erkannt")
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
            # Unsere eigene TXXX:MUSIK_MANAGER_SONG_ID — wird für
            # Rename/Move-Detection in run_scan() gebraucht.
            # Erst Schnell-Pfad: tags.get("TXXX:DESC") ist O(1) per Hash,
            # getall("TXXX") würde alle TXXX-Frames linear durchgehen.
            try:
                t = tags.get("TXXX:MUSIK_MANAGER_SONG_ID")
                if t and t.text:
                    info["mm_song_id"] = int(str(t.text[0]))
            except (AttributeError, ValueError, TypeError):
                pass
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


def _insert_song(conn: sqlite3.Connection, song_info: dict) -> str | None:
    """INSERT OR REPLACE. Liefert None bei Erfolg, Fehler-String sonst.

    Vorher: stiller False-Return. User sah "N Fehler" ohne zu wissen was.
    Jetzt: Fehlergrund wird in error_samples gesammelt (analog sync_id3)
    damit die UI ausklappbar anzeigen kann welche Pfade/Gründe es sind.
    """
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
        return None
    except sqlite3.Error as e:
        print(f"  ⚠ DB-Fehler: {e}", file=sys.stderr)
        return f"db: {e}"


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
        Dict mit Statistik: scanned, inserted, updated, skipped, errors,
        stopped, renamed (neue Kategorie — Datei hatte ID3-Song-ID, Pfad
        wurde in der DB aktualisiert).
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

        stats = {
            "scanned": 0, "inserted": 0, "updated": 0, "skipped": 0,
            "renamed": 0, "deleted": 0, "playlist_tracks_removed": 0,
            "errors": 0, "stopped": False,
            # Sample der ersten Fehler (max 5) — UI klappt das aus,
            # so wie bei sync_id3. Pattern-Hinweis wenn alle gleich.
            "error_samples": [],
        }
        ERROR_SAMPLE_LIMIT = 5
        # Hard-Delete-Reconciliation (Files die in DB aber nicht auf Platte)
        # wird NACH dem Loop gemacht — damit Rename-Erkennung via mm_song_id
        # vorher laufen kann und die DB-Pfade aktualisiert. Sonst wuerden
        # renamed files fälschlich als geloescht erkannt.
        for i, filepath in enumerate(files):
            if should_stop and should_stop():
                stats["stopped"] = True
                break
            try:
                # Tags lesen — beinhaltet mm_song_id aus TXXX
                song_info = extract_tags(filepath)

                # 1) Rename/Move-Detection: Hat die Datei eine
                #    MUSIK_MANAGER_SONG_ID, die wir kennen? Wenn ja, ist
                #    sie vermutlich umbenannt/verschoben worden. Wir
                #    schauen in der DB nach dieser ID:
                #    - existiert mit ANDEREM Pfad → Pfad-Update
                #    - existiert mit GLEICHEM Pfad → Skip (nichts tun)
                #    - existiert NICHT → normal als Insert weiter
                mm_id = song_info.get("mm_song_id")
                if mm_id is not None:
                    cur.execute(
                        "SELECT id, filepath FROM songs WHERE id = ?", (mm_id,)
                    )
                    row = cur.fetchone()
                    if row is not None:
                        old_id, old_path = row
                        if old_path != filepath:
                            # Pfad-Update: nur filepath + filename aktualisieren
                            # (Rest der Tags bleibt — sind ja identisch, ist
                            # nur eine Pfad-Mutation)
                            conn.execute(
                                "UPDATE songs SET filepath = ?, filename = ? "
                                "WHERE id = ?",
                                (filepath, os.path.basename(filepath), old_id),
                            )
                            stats["renamed"] += 1
                            stats["scanned"] += 1
                            if (i + 1) % 50 == 0 and on_progress:
                                on_progress(i + 1, len(files),
                                            f"{i+1}/{len(files)} verarbeitet "
                                            f"(umbenannt: {stats['renamed']})")
                            continue
                        else:
                            # ID + Pfad identisch — nichts zu tun
                            stats["skipped"] += 1
                            stats["scanned"] += 1
                            if (i + 1) % 50 == 0 and on_progress:
                                on_progress(i + 1, len(files),
                                            f"{i+1}/{len(files)} verarbeitet")
                            continue
                    # else: ID nicht in DB — vermutlich alte DB oder
                    # kaputter Tag. Fallthrough zum normalen Insert.

                # 2) Normaler Pfad: existiert der filepath schon?
                if not force_rescan:
                    cur.execute("SELECT id FROM songs WHERE filepath = ?",
                                (filepath,))
                    if cur.fetchone():
                        stats["skipped"] += 1
                        stats["scanned"] += 1
                        if (i + 1) % 50 == 0 and on_progress:
                            on_progress(i + 1, len(files),
                                        f"{i+1}/{len(files)} verarbeitet")
                        continue

                # 3) Insert (oder Update bei force_rescan)
                cur2 = conn.cursor()
                if force_rescan:
                    cur2.execute("SELECT id FROM songs WHERE filepath = ?",
                                 (filepath,))
                    already = cur2.fetchone()
                else:
                    already = None
                insert_err = _insert_song(conn, song_info)
                if insert_err is None:
                    if already:
                        stats["updated"] += 1
                    else:
                        stats["inserted"] += 1
                else:
                    stats["errors"] += 1
                    if len(stats["error_samples"]) < ERROR_SAMPLE_LIMIT:
                        stats["error_samples"].append({
                            "filepath": filepath,
                            "reason": insert_err,
                        })
                stats["scanned"] += 1
            except Exception as e:
                stats["errors"] += 1
                stats["scanned"] += 1
                if len(stats["error_samples"]) < ERROR_SAMPLE_LIMIT:
                    stats["error_samples"].append({
                        "filepath": filepath,
                        "reason": f"{type(e).__name__}: {e}",
                    })
                print(f"  ⚠ Fehler bei {filepath}: {e}", file=sys.stderr)
            if (i + 1) % 50 == 0 and on_progress:
                on_progress(i + 1, len(files),
                            f"{i+1}/{len(files)} verarbeitet")
        # Post-Loop Reconciliation: Files in DB die NICHT (mehr) auf Platte sind
        # → hart loeschen. Reihenfolge: erst playlist_tracks (Cascade), dann
        # songs. So vermeiden wir temporaer "Geister-Playlists" (Eintrag
        # ohne Song). Anschliessend wird die M3U beim naechsten Playlist-Render
        # automatisch korrekt geschrieben.
        if not stats["stopped"]:
            # Set-Build ist O(N) und billig; 10k Strings passen locker in RAM
            files_set = set(files)
            cur.execute("SELECT id, filepath FROM songs")
            orphaned = [(sid, fp) for sid, fp in cur.fetchall()
                        if fp not in files_set]
            if orphaned:
                orphan_ids = [sid for sid, _ in orphaned]
                # SQLite limit: 999 Parameter pro Query. Bei >999 IDs
                # batchen wir in Chunks. Spart "too many SQL variables"
                # Fehler bei sehr grossen DBs (>10k orphans).
                BATCH = 999
                for i in range(0, len(orphan_ids), BATCH):
                    batch = orphan_ids[i:i + BATCH]
                    placeholders = ",".join("?" * len(batch))
                    cur.execute(
                        f"DELETE FROM playlist_tracks WHERE song_id IN ({placeholders})",
                        batch,
                    )
                    stats["playlist_tracks_removed"] += cur.rowcount
                    cur.execute(
                        f"DELETE FROM songs WHERE id IN ({placeholders})",
                        batch,
                    )
                    stats["deleted"] += cur.rowcount
                if on_progress:
                    on_progress(
                        len(files), len(files),
                        f"Scan abgeschlossen — {stats['deleted']} geloescht, "
                        f"{stats['playlist_tracks_removed']} Playlist-Tracks entfernt",
                    )

        conn.commit()
        return stats
    finally:
        conn.close()
