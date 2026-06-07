"""ID3-Tag-Sync: schreibt den Song-Score zurück in die MP3-Datei.

Wir schreiben ZWEI Felder:
- TXXX:MUSIK_MANAGER_SCORE    — unser interner Score (0-100, volle Pr\u00e4zision)
- TXXX:MUSIK_MANAGER_SONG_ID  — die DB-ID (Robustheits-anker, analog ?sid=)
- POPM (Popularimeter)        — standardisiertes Rating, 0-255. Wird von
                                 WMP, Winamp, vielen Auto-Head-Units nativ
                                 angezeigt. Mappung: round(score * 2.55).

Wichtig: mutagen.ID3 schreibt NUR die ID3v2-Container. Die MPEG-Audio-Frames
werden bit-identisch nicht angefasst (siehe /tmp/test_id3_write.py).
"""
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from mutagen.id3 import ID3, ID3NoHeaderError, TXXX, POPM

log = logging.getLogger(__name__)

# Konstanten f\u00fcr TXXX-Desc-Felder. Nicht \u00e4ndern ohne Frontend-Code
# mitzudrehen — die Strings sind der Vertrag.
TXXX_SCORE = "MUSIK_MANAGER_SCORE"
TXXX_SONG_ID = "MUSIK_MANAGER_SONG_ID"

# POPM-Email: Standard ist leerer String ("unknown user"), das ist genau
# was wir wollen — das Rating geh\u00f6rt der Datei, nicht einem User-Konto.
POPM_EMAIL = ""


def _score_to_popm(score_0_100: float) -> int:
    """Map 0-100 EWMA-Score auf POPM-Skala 0-255."""
    return max(0, min(255, round(score_0_100 * 2.55)))


def write_score_to_mp3(
    filepath: Optional[str], song_id: int, score: float
) -> Tuple[bool, str]:
    """Schreibe Score + Song-ID + POPM in die MP3-Tags.

    Args:
        filepath: Absoluter Pfad zur MP3 (kann None sein, dann no-op).
        song_id:  DB-ID des Songs.
        score:    0-100 EWMA-Score.

    Returns:
        (success, message) — message ist leer bei Erfolg, sonst Grund.
        Caller (record_play_event) loggt message und blockt die Response
        NICHT — DB ist die Source of Truth, ID3 ist Bonus.
    """
    if not filepath:
        return True, ""  # Kein Pfad in DB (Stream/Online?) — no-op, ok
    if not filepath.lower().endswith(".mp3"):
        return True, f"skip (not .mp3: {filepath})"

    # NUL-Byte-Check VOR exists(): Scanner-Bug bei IDs ~9907+ hat NULs
    # im Pfad. exists() kann auf sowas seltsam reagieren, und mutagen
    # würde beim open() hart crashen. Pfad bereinigen hilft aber meistens.
    if "\x00" in filepath:
        clean = filepath.replace("\x00", "")
        return False, f"NUL-bytes in path (try: {clean})"

    path = Path(filepath)
    if not path.exists():
        return False, f"file not found: {filepath}"

    try:
        # CIFS/NFS-Quirk: mutagen's in-place save() benutzt insert_bytes/
        # delete_bytes via pwrite, was auf manchen SMB-Shares (Synology etc.)
        # mit "Permission denied" fehlschlägt, obwohl ein simpler Write
        # via cp/touch funktioniert.
        #
        # Außerdem: mutagen's save() auf einem BytesIO schreibt NUR die
        # ID3v2-Tags, nicht den Audio-Anteil — bei Dateien ohne v2-Header
        # (nur v1) wäre die neue Datei nur ~1KB statt 3MB.
        #
        # Lösung: wir parsen die vorhandene Struktur (v2-Header am Anfang,
        # v1-Trailer am Ende), bauen den v2-Block neu, hängen Audio + v1
        # unverändert dran, schreiben atomar (tmp + os.replace).
        original = path.read_bytes()

        # 1) Audio-Anteil extrahieren (alles zwischen letztem v2-Header
        #    und v1-Trailer)
        if original[:3] == b"ID3":
            v2_size = ((original[6] & 0x7F) << 21) | ((original[7] & 0x7F) << 14) \
                    | ((original[8] & 0x7F) << 7)  | (original[9] & 0x7F)
            audio_start = 10 + v2_size
        else:
            audio_start = 0
        if original[-128:-125] == b"TAG":
            audio_end = len(original) - 128
        else:
            audio_end = len(original)
        audio = original[audio_start:audio_end]
        v1 = original[audio_end:] if audio_end < len(original) else b""

        # 2) Bestehende v2-Tags parsen (oder leeres Set wenn keine v2)
        try:
            tags = ID3(BytesIO(original))
        except ID3NoHeaderError:
            tags = ID3()

        # 3) Eigene Frames idempotent überschreiben
        for key in list(tags.keys()):
            if key.startswith("TXXX:MUSIK_MANAGER_") or key == "POPM":
                del tags[key]
        tags.add(TXXX(encoding=3, desc=TXXX_SCORE, text=[f"{score:.4f}"]))
        tags.add(TXXX(encoding=3, desc=TXXX_SONG_ID, text=[str(song_id)]))
        tags.add(POPM(email=POPM_EMAIL, rating=_score_to_popm(score), count=0))

        # 4) Neuen v2-Block rendern (nur Header+Frames, kein Audio)
        tag_buf = BytesIO()
        tags.save(tag_buf, v2_version=3)
        new_v2 = tag_buf.getvalue()

        # 5) Atomar zurückschreiben: neue_v2 + audio + v1
        new_bytes = new_v2 + audio + v1
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(new_bytes)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        return True, ""
    except PermissionError as e:
        return False, f"permission denied: {e}"
    except OSError as e:
        return False, f"OS error: {e}"
    except Exception as e:
        # Letzter Fangschirm: mutagen-spezifische Fehler (kaputte Tags etc.)
        return False, f"unexpected: {type(e).__name__}: {e}"


def read_score_from_mp3(filepath: str) -> Optional[float]:
    """Lese den Score aus einer MP3. None wenn nicht vorhanden / kaputt.

    Praktisch für die geplante Bulk-Resync-Route, und um zu prüfen
    ob ein Song schon ein ID3-Rating hat (z.B. wenn User Songs auf einem
    anderen Wege bewertet hat und wir reimportieren wollen)."""
    if not filepath or "\x00" in filepath or not filepath.lower().endswith(".mp3"):
        return None
    path = Path(filepath)
    if not path.exists():
        return None
    try:
        tags = ID3(path)
        frames = tags.getall("TXXX")
        for f in frames:
            if f.desc == TXXX_SCORE:
                return float(f.text[0])
    except Exception:
        return None
    return None


def has_mm_song_id(filepath: str) -> bool:
    """True wenn die MP3 eine TXXX:MUSIK_MANAGER_SONG_ID hat.

    Schneller O(1)-Check via tags.get() statt getall()+iter. Gebraucht
    vom Bulk-Backfill, um entscheiden zu können ob wir schreiben müssen
    (Mode 'missing_only')."""
    if not filepath or "\x00" in filepath or not filepath.lower().endswith(".mp3"):
        return False
    path = Path(filepath)
    if not path.exists():
        return False
    try:
        tags = ID3(path)
        t = tags.get("TXXX:MUSIK_MANAGER_SONG_ID")
        return bool(t and t.text)
    except Exception:
        return False


# === Bulk-Sync (Backfill) ===

from typing import Callable, Iterable  # noqa: E402

ProgressFn = Callable[[int, int, str], None]
ShouldStopFn = Callable[[], bool]


def run_id3_sync(
    db_path: str | Path,
    mode: str = "missing_only",
    on_progress: ProgressFn | None = None,
    should_stop: ShouldStopFn | None = None,
) -> dict:
    """Bulk-Backfill: schreibt TXXX+TAG+POPM für alle (oder gefilterte)
    MP3-Songs in der DB.

    Args:
        db_path: Pfad zur SQLite-Datei mit der songs-Tabelle.
        mode:
            - "missing_only" (default): nur Dateien ohne mm_song_id
            - "all":  alle MP3-Songs (überschreibt bestehende)
            - "force": wie "all", ignoriert aber auch mtime-Check nicht
              (reserviert für künftige Optimierung)
        on_progress: Callback(current, total, message)
        should_stop: Callable, der True liefert wenn der Job abgebrochen
            werden soll.

    Returns:
        Dict mit Statistik: scanned, written, skipped, errors, stopped.

    Designentscheidungen:
        - Nur MP3 (kein FLAC/M4A/OGG): write_score_to_mp3 ist aktuell
          nur für MP3-ID3v2 implementiert. Erweiterung später.
        - DB ist Source of Truth: Score/ID kommen aus der DB, Datei wird
          aktualisiert. Bei Konflikten gewinnt immer die DB (siehe
          song-ranking.md).
        - Atomar: write_score_to_mp3 nutzt tmp+os.replace — kein
          Risiko halbgeschriebener Dateien.
        - Fehler pro Datei werden gezählt, der Job bricht NICHT ab.
    """
    import sqlite3 as _sqlite3
    if mode not in ("missing_only", "all", "force"):
        raise ValueError(f"Ungültiger mode: {mode!r}")
    # "force" ist aktuell synonym mit "all", aber semantisch anders
    # gemeint (z.B. wenn wir später mtime-Check einbauen)
    effective_mode = "all" if mode == "force" else mode

    conn = _sqlite3.connect(str(db_path), timeout=30)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, filepath, score FROM songs "
            "WHERE filepath LIKE '%.mp3' ORDER BY id"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if on_progress:
        on_progress(0, len(rows), f"{len(rows)} MP3-Songs in DB gefunden")

    stats = {
        "scanned": 0, "written": 0, "skipped": 0,
        "errors": 0, "stopped": False,
    }
    for i, (song_id, filepath, score) in enumerate(rows):
        if should_stop and should_stop():
            stats["stopped"] = True
            break
        stats["scanned"] += 1

        # missing_only: vor dem Schreiben prüfen ob Datei schon ID hat
        if effective_mode == "missing_only" and has_mm_song_id(filepath):
            stats["skipped"] += 1
            if (i + 1) % 50 == 0 and on_progress:
                on_progress(i + 1, len(rows),
                            f"{i+1}/{len(rows)} verarbeitet "
                            f"(geschrieben: {stats['written']})")
            continue

        ok, msg = write_score_to_mp3(filepath, song_id, score or 50.0)
        if ok:
            stats["written"] += 1
        else:
            stats["errors"] += 1
            log.warning("ID3-Backfill Fehler song_id=%d: %s", song_id, msg)

        if (i + 1) % 50 == 0 and on_progress:
            on_progress(i + 1, len(rows),
                        f"{i+1}/{len(rows)} verarbeitet "
                        f"(geschrieben: {stats['written']}, "
                        f"Fehler: {stats['errors']})")

    if on_progress:
        on_progress(len(rows), len(rows), "ID3-Sync abgeschlossen")
    return stats
