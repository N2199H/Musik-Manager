#!/usr/bin/env python3
"""
Lege fehlende M3U-Dateien für Playlists an, die in der DB existieren
aber keine m3u_filepath haben.

Idempotent: überspringt Playlists die schon ein m3u_filepath haben.
Defensiv: --dry-run Modus der nichts schreibt.
Symlink-sicher: nutzt die DB-Pfade wie der Service sie sieht (app.database.DB_PATH).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Service-Konstanten (müssen mit app/main.py übereinstimmen)
NAS_MUSIC_PATH = "/tmp/nas-musik"
SERVICE_DB = "/home/openclaw/.openclaw/workspace/musik-manager/musik.db"


def _relpath_or_uri(filepath: str, m3u_dir: str) -> str:
    """Konvertiere Song-Filepath in M3U-Eintrag (relativ wenn möglich, sonst x-file-cifs URI)."""
    if not filepath:
        return ""
    if m3u_dir:
        try:
            rel = os.path.relpath(filepath, m3u_dir)
            return rel
        except ValueError:
            pass
    # Fallback: x-file-cifs URI (für Player die das nicht brauchen ist der rel-pfad oben schon ok)
    rel_path = filepath.replace(NAS_MUSIC_PATH, "")
    return f"x-file-cifs://192.168.0.7/Musik{rel_path}"


def build_m3u_content(playlist_name: str, tracks: list, m3u_dir: str) -> str:
    """Baut den M3U-Text. Format identisch mit app/main.py _export_m3u()."""
    lines = ["#EXTM3U"]
    for song in tracks:
        duration = int(song["duration_sec"]) if song["duration_sec"] else -1
        artist = song["artist"] or ""
        title = song["title"] or ""
        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        lines.append(_relpath_or_uri(song["filepath"], m3u_dir))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Seed M3U files for playlists without m3u_filepath")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, write nothing")
    parser.add_argument("--playlist-ids", nargs="*", type=int, default=None, help="Only process these playlist IDs (default: all without m3u_filepath)")
    args = parser.parse_args()

    if not os.path.exists(SERVICE_DB):
        print(f"ERROR: Service-DB nicht gefunden: {SERVICE_DB}", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(SERVICE_DB)
    db.row_factory = sqlite3.Row

    # Ziel-Playlists wählen
    if args.playlist_ids:
        placeholders = ",".join("?" * len(args.playlist_ids))
        query = f"SELECT id, name, m3u_filepath FROM playlists WHERE id IN ({placeholders})"
        rows = db.execute(query, args.playlist_ids).fetchall()
    else:
        rows = db.execute("SELECT id, name, m3u_filepath FROM playlists WHERE m3u_filepath IS NULL OR m3u_filepath = ''").fetchall()

    if not rows:
        print("Keine Playlists ohne m3u_filepath gefunden.")
        return

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Verarbeite {len(rows)} Playlist(s):")
    total_tracks = 0

    for pl in rows:
        pid = pl["id"]
        pname = pl["name"]
        # Tracks in DB-Reihenfolge
        tracks = db.execute("""
            SELECT pt.position, s.filepath, s.title, s.artist, s.duration_sec
            FROM playlist_tracks pt
            JOIN songs s ON s.id = pt.song_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position
        """, (pid,)).fetchall()

        # M3U-Zielpfad: ins NAS-Root mit Playlist-Name als Filename
        # (analog zu den existierenden +X.m3u Files)
        m3u_path = os.path.join(NAS_MUSIC_PATH, f"{pname}.m3u")
        m3u_dir = os.path.dirname(m3u_path)

        # Existierende Datei am Ziel? (z.B. wenn M3U schon existiert aber DB-Verknüpfung fehlt)
        existing = os.path.exists(m3u_path)

        print(f"\n--- Playlist {pid}: '{pname}' ({len(tracks)} tracks) ---")
        print(f"    Target: {m3u_path}")
        print(f"    Existing: {existing}")
        if not tracks:
            print(f"    SKIP: keine Tracks")
            continue

        # Verify alle Source-Files existieren
        missing = [t for t in tracks if t["filepath"] and not os.path.exists(t["filepath"])]
        if missing:
            print(f"    WARN: {len(missing)}/{len(tracks)} Source-Files fehlen:")
            for m in missing[:3]:
                print(f"        {m['filepath']}")
            if not args.dry_run:
                resp = input(f"    Trotzdem M3U schreiben? [y/N] ").strip().lower()
                if resp != "y":
                    print(f"    SKIP (user declined)")
                    continue

        content = build_m3u_content(pname, tracks, m3u_dir)

        if args.dry_run:
            print(f"    [DRY-RUN] Würde schreiben: {len(content)} bytes, {len(tracks)} tracks")
            print(f"    [DRY-RUN] Preview (erste 5 Zeilen):")
            for line in content.splitlines()[:5]:
                print(f"        {line}")
            total_tracks += len(tracks)
            continue

        # Atomic write
        tmp_path = m3u_path + ".tmp"
        os.makedirs(m3u_dir, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8-sig") as f:
            f.write(content)
        os.replace(tmp_path, m3u_path)
        # DB-Update
        db.execute(
            "UPDATE playlists SET m3u_filepath=?, updated_at=? WHERE id=?",
            (m3u_path, datetime.now().isoformat(), pid)
        )
        db.commit()
        print(f"    OK: {len(content)} bytes geschrieben, DB m3u_filepath gesetzt")
        total_tracks += len(tracks)

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Total: {len(rows)} Playlists, {total_tracks} Tracks")
    db.close()


if __name__ == "__main__":
    main()
