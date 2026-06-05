"""MusicBrainz-Client + Enrichment-Logik.

Stellt wiederverwendbare Funktionen bereit:
    mb_request(url)                 -- HTTP-GET mit Retry + Rate-Limit
    mb_search_recording(artist, title)
    mb_get_artist_mbid(artist_name)
    mb_get_artist_tags(mbid)        -- Top-3 Genres als String
    best_match(...)                 -- Score-basiertes Ranking
    extract_recording_info(...)     -- Album + Year aus Recording-JSON

Konstanten:
    RATE_LIMIT      -- Sekunden zwischen Requests (1.1 = MusicBrainz-konform)
    USER_AGENT      -- wird von MusicBrainz verlangt (Kontakt-E-Mail)
    SKIP_ARTISTS    -- Pseudo-/Radio-Artists, die nicht angereichert werden
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from app.scanner.nas import ensure_schema as _ensure_schema  # Schema-Setup teilen

RATE_LIMIT = 1.1
USER_AGENT = "MusikManager/1.0 (harald@home; music enrichment)"

# Künstler-Namen, die keine echten Artists sind (Radio-Sender, Sound-Effekte)
SKIP_ARTISTS = {
    "This Is House", "1LIVE (WDR)", "Air Classique", "Absolute Ibiza",
    "W26 Radio", "wunschradio.fm Rock AutoDJ", "soundtrack", "Soundtrack",
    "sounds", "Zeitgenössisch", "Meeresrauschen.mp3",
}


# === MusicBrainz HTTP-Layer ===

def mb_request(url: str, retries: int = 3) -> Optional[dict]:
    """HTTP-GET mit Retry- und Rate-Limit-Handling."""
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"    ⏳ Rate-limited, warte {wait}s...", file=sys.stderr)
                time.sleep(wait)
            elif e.code >= 500:
                time.sleep(3)
            else:
                return None
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"    ⚠ Network error: {e}", file=sys.stderr)
            time.sleep(3)
    return None


def mb_search_recording(artist: str, title: str) -> Optional[dict]:
    """MusicBrainz nach Recording suchen."""
    artist_clean = artist.replace('"', "").replace("\\", "")
    title_clean = title.replace('"', "").replace("\\", "")
    title_clean = re.sub(r"[(){}\[\]^~:/]", " ", title_clean)
    title_clean = re.sub(r"\s+", " ", title_clean).strip()
    query = f'artist:"{artist_clean}" AND recording:"{title_clean}"'
    url = (
        f"https://musicbrainz.org/ws/2/recording/?"
        f"query={quote_plus(query)}&fmt=json&limit=5"
    )
    return mb_request(url)


def mb_get_artist_mbid(artist_name: str) -> Optional[str]:
    """MusicBrainz Artist-ID (MBID) suchen."""
    url = (
        f"https://musicbrainz.org/ws/2/artist/?"
        f"query={quote_plus(artist_name)}&fmt=json&limit=1"
    )
    data = mb_request(url)
    if data and data.get("artists"):
        return data["artists"][0]["id"]
    return None


def mb_get_artist_tags(mbid: str) -> Optional[str]:
    """Genre-Tags für einen Artist von MusicBrainz holen (Top 3, kommasepariert)."""
    url = f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=tags&fmt=json"
    data = mb_request(url)
    if data and data.get("tags"):
        tags = sorted(data["tags"], key=lambda t: t.get("count", 0), reverse=True)
        return ", ".join(t["name"].title() for t in tags[:3])
    return None


def best_match(results: dict, artist: str, title: str) -> Optional[dict]:
    """Bestes Recording aus MusicBrainz-Ergebnissen auswählen.

    Bevorzugt Studio-Aufnahmen vor Live/Remix-Versionen.
    """
    if not results or "recordings" not in results or not results["recordings"]:
        return None
    candidates = [c for c in results["recordings"] if c.get("score", 0) >= 80]
    if not candidates:
        candidates = [c for c in results["recordings"] if c.get("score", 0) >= 60]
        if not candidates:
            return None
    best = None
    best_score = 0
    for c in candidates:
        score = c.get("score", 0)
        disambig = (c.get("disambiguation") or "").lower()
        if "live" not in disambig and "remix" not in disambig:
            score += 5
        if score > best_score:
            best_score = score
            best = c
    return best or candidates[0]


def extract_recording_info(recording: dict) -> dict:
    """Album + Jahr + MBID aus MusicBrainz-Recording extrahieren."""
    info = {"album": None, "year": None, "mbid": recording.get("id")}
    releases = recording.get("releases", [])
    if not releases:
        return info
    for rel in releases:
        rg = rel.get("release-group", {}) or {}
        if rg.get("primary-type") == "Album":
            info["album"] = rel.get("title")
            date = rel.get("date", "")
            if date:
                info["year"] = date[:4]
            break
    if not info["album"]:
        info["album"] = releases[0].get("title")
        date = releases[0].get("date", "")
        if date:
            info["year"] = date[:4]
    return info


def clean_title(filename: str, artist: Optional[str] = None) -> str:
    """Bereinigt einen Filename zu einem brauchbaren Song-Titel."""
    base = filename.rsplit(".", 1)[0]
    base = re.sub(r"^\d{1,3}[\.\s]+", "", base)
    if artist:
        base = re.sub(
            r"^" + re.escape(artist) + r"\s*[-–]\s*", "", base, flags=re.IGNORECASE
        )
    base = re.sub(r"\s*\(.*?\)\s*$", "", base)
    base = re.sub(r"\s*\[.*?\]\s*$", "", base)
    base = base.strip()
    return base if len(base) > 1 else filename.rsplit(".", 1)[0].strip()


# === High-Level Enrichment-Jobs ===

def _db_execute(cur, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """DB-Execute mit Retry bei Lock."""
    last_exc: Optional[Exception] = None
    for attempt in range(5):
        try:
            return cur.execute(sql, params)
        except sqlite3.OperationalError as e:
            last_exc = e
            if "locked" in str(e).lower() and attempt < 4:
                wait = 0.5 * (attempt + 1)
                print(f"    🔒 DB locked, retry {attempt+1}/5 in {wait:.1f}s...",
                      file=sys.stderr)
                time.sleep(wait)
    # Wenn alle Retries fehlschlagen, Exception werfen
    raise last_exc  # type: ignore[misc]


def run_enrich_genres(
    db_path: str | Path,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Holt Genre-Tags pro Artist aus MusicBrainz.

    Effizient: 1 Artist-Request + 1 Tags-Request = 2 req/Artist.
    Bei 1952 Artists = ~3900 Requests ≈ 1,1h.

    Returns:
        Dict mit: updated (int), skipped, errors, total_artists
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        skip_list = ",".join(f'"{a}"' for a in SKIP_ARTISTS)
        cur.execute(
            f"""SELECT DISTINCT artist FROM songs
                WHERE artist IS NOT NULL AND artist != ''
                AND artist NOT IN ({skip_list})
                ORDER BY artist"""
        )
        artists = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT artist, genre FROM songs "
            "WHERE genre IS NOT NULL AND genre != ''"
        )
        genre_cache: dict = {artist: genre for artist, genre in cur.fetchall()}

        stats = {"total_artists": len(artists), "updated": 0,
                 "skipped": 0, "errors": 0, "stopped": False}

        if on_progress:
            on_progress(0, len(artists), f"{len(artists)} Künstler zu prüfen")

        for i, artist in enumerate(artists):
            if should_stop and should_stop():
                stats["stopped"] = True
                break
            if artist in genre_cache:
                stats["skipped"] += 1
                continue
            if (i + 1) % 50 == 0 and on_progress:
                on_progress(i + 1, len(artists), f"{i+1}/{len(artists)} Künstler...")
            try:
                mbid = mb_get_artist_mbid(artist)
                time.sleep(RATE_LIMIT)
                if not mbid:
                    stats["errors"] += 1
                    continue
                genre = mb_get_artist_tags(mbid)
                time.sleep(RATE_LIMIT)
                if genre:
                    genre_cache[artist] = genre
                    stats["updated"] += 1
            except Exception as e:
                print(f"    ⚠ Fehler bei '{artist}': {e}", file=sys.stderr)
                stats["errors"] += 1

        for artist, genre in genre_cache.items():
            cur.execute(
                "UPDATE songs SET genre = ? WHERE artist = ? "
                "AND (genre IS NULL OR genre = '')",
                (genre, artist),
            )
        conn.commit()
        if on_progress:
            on_progress(len(artists), len(artists), "Genre-Enrichment abgeschlossen")
        return stats
    finally:
        conn.close()


def run_enrich_album_year(
    db_path: str | Path,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """Holt Album + Year pro Song aus MusicBrainz.

    1 Request/Song → bei 10k Songs = ca. 3 Stunden.

    Returns:
        Dict mit: total, updated_album, updated_year, not_found, errors, stopped
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        skip_list = ",".join(f'"{a}"' for a in SKIP_ARTISTS)
        cur.execute(
            f"""SELECT id, artist, title, filename FROM songs
                WHERE artist IS NOT NULL AND artist != ''
                AND (album IS NULL OR album = '' OR year IS NULL OR year = '')
                AND artist NOT IN ({skip_list})
                ORDER BY id"""
        )
        songs = cur.fetchall()
        stats = {
            "total": len(songs),
            "updated_album": 0,
            "updated_year": 0,
            "not_found": 0,
            "errors": 0,
            "stopped": False,
        }
        if on_progress:
            on_progress(0, len(songs), f"{len(songs)} Songs zu prüfen")

        for i, (song_id, artist, title, filename) in enumerate(songs):
            if should_stop and should_stop():
                stats["stopped"] = True
                break
            search_title = clean_title(filename, artist)
            if title and title != filename.rsplit(".", 1)[0] and len(title) > len(search_title):
                search_title = title
            search_title = re.sub(r"\(\d+\)$", "", search_title).strip()
            if len(search_title) < 2:
                stats["not_found"] += 1
                continue
            if (i + 1) % 100 == 0 and on_progress:
                on_progress(
                    i + 1, len(songs),
                    f"{i+1}/{len(songs)} – {artist}: {search_title[:40]}",
                )
            try:
                result = mb_search_recording(artist, search_title)
                if result is None:
                    stats["errors"] += 1
                    time.sleep(RATE_LIMIT)
                    continue
                match = best_match(result, artist, search_title)
                if match is None:
                    stats["not_found"] += 1
                    time.sleep(RATE_LIMIT)
                    continue
                info = extract_recording_info(match)
                cur.execute("SELECT album, year FROM songs WHERE id = ?", (song_id,))
                cur_album, cur_year = cur.fetchone()
                if info["album"] and (not cur_album or cur_album == ""):
                    cur.execute("UPDATE songs SET album = ? WHERE id = ?",
                                (info["album"], song_id))
                    stats["updated_album"] += 1
                if info["year"] and (not cur_year or cur_year == ""):
                    cur.execute("UPDATE songs SET year = ? WHERE id = ?",
                                (info["year"], song_id))
                    stats["updated_year"] += 1
            except Exception as e:
                print(f"    ⚠ Fehler bei Song {song_id}: {e}", file=sys.stderr)
                stats["errors"] += 1
            time.sleep(RATE_LIMIT)
            if (i + 1) % 500 == 0:
                conn.commit()
                if on_progress:
                    on_progress(
                        i + 1, len(songs),
                        f"💾 Zwischenspeicher ({i+1}, Album: {stats['updated_album']}, "
                        f"Year: {stats['updated_year']})",
                    )
        conn.commit()
        if on_progress:
            on_progress(len(songs), len(songs), "Album/Year-Enrichment abgeschlossen")
        return stats
    finally:
        conn.close()
