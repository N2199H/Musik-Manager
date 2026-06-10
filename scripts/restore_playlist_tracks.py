#!/usr/bin/env python3
"""Restore playlist_tracks from a pre-incident DB snapshot.

Hintergrund: Am 9.6.2026 ~13:20 wurden beim Test eines neuen Scan-Codes
versehentlich alle playlist_tracks-Einträge gelöscht (1616 Rows in
musik.db.before-scan-test, 0 Rows danach). Die Song-IDs sind NICHT stabil
(Reimport hat alle neu durchnummeriert), aber die Pfade sind stabil —
also matchen wir über filepath.

Usage:
    venv/bin/python3 scripts/restore_playlist_tracks.py \
        --backup /tmp/musik.db.before-scan-test \
        --target musik.db \
        --dry-run                  # nur reporten, nichts schreiben

    venv/bin/python3 scripts/restore_playlist_tracks.py \
        --backup /tmp/musik.db.before-scan-test \
        --target musik.db          # restore ausführen
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def load_song_map(db_path: str) -> dict[str, int]:
    """filepath → song_id"""
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT id, filepath FROM songs").fetchall()
    con.close()
    return {fp: sid for sid, fp in rows}


def load_backup_tracks(backup_db: str) -> list[tuple[int, int, int]]:
    """Gibt (playlist_id, song_id_old, position)-Tupel aus dem Backup zurück."""
    con = sqlite3.connect(backup_db)
    rows = con.execute(
        """
        SELECT pt.playlist_id, s.filepath, pt.position
        FROM playlist_tracks pt
        JOIN songs s ON s.id = pt.song_id
        ORDER BY pt.playlist_id, pt.position
        """
    ).fetchall()
    con.close()
    return [(pl_id, fp, pos) for pl_id, fp, pos in rows]


def dry_run_report(backup_db: str, target_db: str) -> dict:
    """Vergleicht Backup vs Target, baut Report."""
    print(f"=== Dry-Run: {backup_db} → {target_db} ===\n")

    # Filepath-Maps
    target_song_map = load_song_map(target_db)

    # Backup-Playlist-Tracks (mit Filepath)
    backup_tracks = load_backup_tracks(backup_db)
    print(f"Backup: {len(backup_tracks)} playlist_tracks")

    # Target-Check: existieren die Playlists noch?
    con = sqlite3.connect(target_db)
    target_playlist_ids = {
        r[0] for r in con.execute("SELECT id FROM playlists").fetchall()
    }
    target_existing_tracks = con.execute(
        "SELECT playlist_id, song_id FROM playlist_tracks"
    ).fetchall()
    con.close()
    target_pairs = {(pl, s) for pl, s in target_existing_tracks}
    print(f"Target: {len(target_playlist_ids)} playlists, "
          f"{len(target_existing_tracks)} playlist_tracks")

    # Match-Statistik
    matched = []
    missing_fp = []  # Filepath existiert nicht mehr in target
    missing_pl = []  # Playlist-ID existiert nicht mehr in target
    duplicate = []   # Eintrag existiert schon in target

    for pl_id, fp, pos in backup_tracks:
        if pl_id not in target_playlist_ids:
            missing_pl.append((pl_id, fp, pos))
            continue
        if fp not in target_song_map:
            missing_fp.append((pl_id, fp, pos))
            continue
        new_song_id = target_song_map[fp]
        if (pl_id, new_song_id) in target_pairs:
            duplicate.append((pl_id, new_song_id, pos))
            continue
        matched.append((pl_id, new_song_id, pos))

    # Per-Playlist-Statistik
    backup_counts = defaultdict(int)
    new_counts = defaultdict(int)
    for pl_id, _, _ in backup_tracks:
        backup_counts[pl_id] += 1
    for pl_id, _, _ in matched:
        new_counts[pl_id] += 1
    for pl_id, _, _ in target_existing_tracks:
        new_counts[pl_id] += 1  # falls schon was da war (idempotent)

    print(f"\n=== Match-Ergebnis ===")
    print(f"  Wird eingefügt:        {len(matched)}")
    print(f"  Filepath weg (skip):   {len(missing_fp)}")
    print(f"  Playlist weg (skip):   {len(missing_pl)}")
    print(f"  Bereits vorhanden:     {len(duplicate)}")

    if missing_fp:
        print(f"\n=== Fehlende Filepaths (gekürzt) ===")
        for pl_id, fp, pos in missing_fp[:10]:
            print(f"  Playlist {pl_id}, pos {pos}: {fp[:80]}")

    # Per-Playlist-Stat
    print(f"\n=== Tracks pro Playlist (vorher → nachher) ===")
    con = sqlite3.connect(backup_db)
    pl_names = dict(con.execute("SELECT id, name FROM playlists").fetchall())
    con.close()
    con = sqlite3.connect(target_db)
    pl_names_new = dict(con.execute("SELECT id, name FROM playlists").fetchall())
    con.close()
    name_map = {**pl_names, **pl_names_new}
    all_pl_ids = sorted(set(backup_counts) | set(new_counts))
    for pl_id in all_pl_ids:
        before = backup_counts.get(pl_id, 0)
        after = new_counts.get(pl_id, 0)
        if before or after:
            delta = after - before
            flag = "" if delta >= 0 else f" ⚠️ -{abs(delta)}"
            print(f"  [{pl_id:3d}] {after:3d}/{before:3d}  "
                  f"{name_map.get(pl_id, '?'):40s}{flag}")

    return {
        "matched": len(matched),
        "missing_filepath": len(missing_fp),
        "missing_playlist": len(missing_pl),
        "duplicate": len(duplicate),
        "matched_rows": matched,
        "missing_filepath_rows": missing_fp,
    }


def execute_restore(backup_db: str, target_db: str) -> None:
    """Führt das eigentliche INSERT aus, in einer Transaktion."""
    target_song_map = load_song_map(target_db)
    backup_tracks = load_backup_tracks(backup_db)

    con = sqlite3.connect(target_db)
    con.execute("PRAGMA foreign_keys = ON")

    # Existierende Paare für Idempotenz laden
    existing = {
        (pl, s)
        for pl, s in con.execute(
            "SELECT playlist_id, song_id FROM playlist_tracks"
        ).fetchall()
    }

    # Playlists die existieren
    valid_playlists = {
        r[0] for r in con.execute("SELECT id FROM playlists").fetchall()
    }

    rows_to_insert = []
    skipped = 0
    for pl_id, fp, pos in backup_tracks:
        if pl_id not in valid_playlists:
            skipped += 1
            continue
        if fp not in target_song_map:
            skipped += 1
            continue
        new_song_id = target_song_map[fp]
        if (pl_id, new_song_id) in existing:
            skipped += 1
            continue
        rows_to_insert.append((pl_id, new_song_id, pos))
        existing.add((pl_id, new_song_id))

    print(f"Eingefügt: {len(rows_to_insert)}  Übersprungen: {skipped}")

    try:
        with con:  # Transaktion
            con.executemany(
                """
                INSERT INTO playlist_tracks (playlist_id, song_id, position)
                VALUES (?, ?, ?)
                """,
                rows_to_insert,
            )
        # Verifizieren
        count = con.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
        print(f"playlist_tracks nach Restore: {count}")
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backup", required=True, help="Pfad zur Backup-DB")
    p.add_argument("--target", required=True, help="Pfad zur Ziel-DB")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur Report, nichts schreiben",
    )
    args = p.parse_args()

    if not Path(args.backup).exists():
        print(f"FEHLER: Backup nicht gefunden: {args.backup}")
        return 1
    if not Path(args.target).exists():
        print(f"FEHLER: Target nicht gefunden: {args.target}")
        return 1

    if args.dry_run:
        report = dry_run_report(args.backup, args.target)
        # Kurzer JSON-Output für CI/Logging
        print(f"\n=== JSON-Summary ===")
        summary = {k: v for k, v in report.items()
                   if k not in ("matched_rows", "missing_filepath_rows")}
        print(json.dumps(summary, indent=2))
        return 0

    # Vor dem Schreiben nochmal dry-run
    print("Vor Restore erst Dry-Run...\n")
    report = dry_run_report(args.backup, args.target)
    print()
    if report["matched"] == 0:
        print("Nichts einzufügen — Abbruch.")
        return 0

    print(f"Restore wird ausgeführt: {args.target}")
    execute_restore(args.backup, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
