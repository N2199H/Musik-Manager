"""db_health — liest die DB und prüft Konsistenz mit dem NAS.

Rein informativ, KEINE Schreibaktionen. Gedacht als Diagnose-Tool das
beantwortet: "Was ist zwischen DB und Platte aus dem Ruder gelaufen?"

Aufruf: python -m app.tools.db_health

Output-Sektionen:
  1. Zusammenfassung (Songs, Playlists, Playlists-Eintraege)
  2. Verwaiste Files (in DB, aber nicht auf Platte)
  3. Files ohne ID3-Song-ID (auf Platte, aber nicht beschreibbar)
  4. Playlist-Geister (Playlist-Eintraege deren Song verwaist ist)
  5. Optional: --verify-tags (liest jeden ID3-Tag und vergleicht mit DB-Mirror)

Designentscheidungen:
  - Read-only. Kein Schreiben, kein Fix. Nur Report.
  - Pfade aus DB.muessen mit tatsaechlichem Pfad auf Mount uebereinstimmen.
    Wenn der User Dateien verschoben hat ohne Rescan, ist die DB "veraltet"
    — das ist genau was wir melden.
  - Bei grossen DBs (>20k Songs) kann der File-System-Check etwas dauern.
    Es wird Fortschritt alle 1000 Files ausgegeben.
"""
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

from app.database import SessionLocal, Song, Playlist, PlaylistTrack
from app.id3_sync import has_mm_song_id


def fmt_path(p: str, max_len: int = 70) -> str:
    if not p:
        return ""
    if len(p) <= max_len:
        return p
    return "..." + p[-(max_len - 3):]


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def subsection(title: str) -> None:
    print()
    print(f"--- {title} ---")


def check_files(songs, id3_limit: int = 500, show_progress: bool = True):
    """Prueft fuer jeden Song ob filepath existiert. Liefert (missing, no_id3, exists).

    ID3-Check: liest max die ersten `id3_limit` MP3s, weil ID3-Parsing via SMB
    teuer ist (~50ms pro File). Vollscan braucht 8+ min — fuer Smoke-Tests reicht
    eine Stichprobe.
    """
    missing = []  # in DB, aber nicht auf Platte
    no_id3 = []   # auf Platte, aber keine mm_song_id
    exists = 0
    total = len(songs)
    id3_done = 0
    for i, s in enumerate(songs):
        if show_progress and i and i % 1000 == 0:
            print(f"  ... {i}/{total} Pfade geprueft, {id3_done} ID3-Tags gelesen", file=sys.stderr)
        if not s.filepath:
            missing.append(s)
            continue
        if not os.path.exists(s.filepath):
            missing.append(s)
            continue
        exists += 1
        # ID3-Check optional, nur fuer MP3s, gestoppt bei id3_limit
        if (s.filepath.lower().endswith(".mp3")
                and id3_done < id3_limit
                and not has_mm_song_id(s.filepath)):
            no_id3.append(s)
            id3_done += 1
        elif s.filepath.lower().endswith(".mp3"):
            id3_done += 1
    return missing, no_id3, exists


def main():
    parser = argparse.ArgumentParser(
        description="DB-Konsistenz-Check (read-only)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Nur die ersten N Songs pruefen (schneller Smoke-Test)",
    )
    parser.add_argument(
        "--no-fs-check", action="store_true",
        help="Nur DB-Stats, kein os.path.exists() pro File (sehr schnell)",
    )
    parser.add_argument(
        "--no-id3-check", action="store_true",
        help="ID3-Tag-Check weglassen (schneller, ueberspringt 'ohne ID3'-Sektion)",
    )
    parser.add_argument(
        "--id3-limit", type=int, default=500,
        help="Max N Files auf ID3 pruefen (Default 500 — ID3-Parsing ist teuer)",
    )
    parser.add_argument(
        "--show-ids", action="store_true",
        help="Song-IDs in Listen mit anzeigen (sonst nur Pfade)",
    )
    args = parser.parse_args()

    section("Musik-Manager DB Health Check")
    print(f"  DB-File: {os.environ.get('MUSIK_DB', 'musik.db')}")
    print(f"  Modus: {'nur DB' if args.no_fs_check else 'DB + Filesystem'}")

    db = SessionLocal()
    try:
        # 1. Zusammenfassung
        section("1. Zusammenfassung")
        song_q = db.query(Song)
        if args.limit:
            songs_all = song_q.limit(args.limit).all()
        else:
            songs_all = song_q.all()
        playlist_count = db.query(Playlist).count()
        track_count = db.query(PlaylistTrack).count()
        mp3_count = sum(1 for s in songs_all if s.filepath and s.filepath.lower().endswith(".mp3"))
        flac_count = sum(1 for s in songs_all if s.filepath and s.filepath.lower().endswith(".flac"))
        other_count = len(songs_all) - mp3_count - flac_count
        print(f"  Songs total:        {len(songs_all):>6}")
        print(f"    davon MP3:        {mp3_count:>6}")
        print(f"    davon FLAC:       {flac_count:>6}")
        print(f"    davon andere:     {other_count:>6}")
        print(f"  Playlists:          {playlist_count:>6}")
        print(f"  Playlist-Tracks:    {track_count:>6}")

        if args.no_fs_check:
            return 0

        # 2. Files pruefen
        section("2. File-System Check")
        print(f"  Pruefe {len(songs_all)} Pfade via os.path.exists() ...")
        id3_limit = 0 if args.no_id3_check else args.id3_limit
        missing, no_id3, exists_count = check_files(songs_all, id3_limit=id3_limit)
        print(f"  Auf Platte:         {exists_count:>6}")
        print(f"  Verwaist (missing): {len(missing):>6}")
        if not args.no_id3_check:
            print(f"  Ohne ID3-Tag:       {len(no_id3):>6}  "
                  f"(Stichprobe: erste {args.id3_limit} MP3s)")
        else:
            print(f"  Ohne ID3-Tag:       (uebersprungen, --no-id3-check)")

        # 3. Verwaiste Files nach Verzeichnis gruppieren
        if missing:
            section("3. Verwaiste Files (in DB, nicht auf Platte)")
            by_dir = defaultdict(list)
            for s in missing:
                d = os.path.dirname(s.filepath) if s.filepath else "<kein Pfad>"
                by_dir[d].append(s)
            print(f"  {len(missing)} Files in {len(by_dir)} Verzeichnissen:")
            for d, lst in sorted(by_dir.items(), key=lambda x: -len(x[1])):
                print(f"\n  [{len(lst):3d} Files]  {fmt_path(d, 80)}")
                for s in lst[:5]:
                    fn = os.path.basename(s.filepath) if s.filepath else "?"
                    id_str = f"id={s.id:5d}  " if args.show_ids else ""
                    print(f"    {id_str}{fn}")
                if len(lst) > 5:
                    print(f"    ... und {len(lst) - 5} weitere")

        # 4. Files ohne ID3-Tag
        if no_id3 and not args.no_id3_check:
            section("4. MP3s ohne ID3-Song-ID (nicht beschreibbar / nicht beschrieben)")
            print(f"  {len(no_id3)} Files in Stichprobe von {id3_limit}.")
            print(f"  Hochrechnung (Anteil): ~{int(len(no_id3) / id3_limit * len(songs_all))} von {mp3_count} MP3s geschaetzt.")
            print(f"  Rescan ohne 'force' ueberspringt sie;")
            print(f"  ID3-Backfill-Job (Settings) wuerde sie schreiben.")
            by_dir = defaultdict(int)
            for s in no_id3:
                d = os.path.dirname(s.filepath) if s.filepath else "<kein Pfad>"
                by_dir[d] += 1
            print("\n  Verteilung:")
            for d, c in sorted(by_dir.items(), key=lambda x: -x[1])[:15]:
                print(f"    {c:4d}  {fmt_path(d, 70)}")
            if len(by_dir) > 15:
                print(f"    ... und {len(by_dir) - 15} weitere Verzeichnisse")

        # 5. Playlist-Geister
        section("5. Playlist-Geister")
        missing_ids = {s.id for s in missing}
        if not missing_ids:
            print("  Keine verwaisten Songs. Nichts zu pruefen.")
        else:
            # PlaylistTrack-Eintraege deren song_id verwaist ist
            ghost_tracks = db.query(PlaylistTrack).filter(
                PlaylistTrack.song_id.in_(missing_ids)
            ).all()
            print(f"  Verwaiste Songs:        {len(missing_ids)}")
            print(f"  Playlist-Eintraege:     {len(ghost_tracks)}")
            if ghost_tracks:
                by_playlist = defaultdict(list)
                pl_map = {p.id: p.name for p in db.query(Playlist).all()}
                for pt in ghost_tracks:
                    pname = pl_map.get(pt.playlist_id, f"<id={pt.playlist_id}>")
                    by_playlist[pname].append(pt)
                print(f"\n  Betroffene Playlists ({len(by_playlist)}):")
                for pname, pts in sorted(by_playlist.items(), key=lambda x: -len(x[1])):
                    print(f"    [{len(pts):3d} Tracks]  {pname}")

        # 6. Top-Empfehlungen
        section("6. Empfehlungen")
        recs = []
        if missing:
            recs.append(
                f"{len(missing)} verwaiste Songs.\n"
                f"  → 'Jetzt scannen' im Settings-Modal druecken.\n"
                f"    Der Scanner erkennt 'mm_song_id' in der Datei und aktualisiert\n"
                f"    den DB-Pfad — falls die Datei auch in der neuen Schreibweise\n"
                f"    nicht existiert, wird die DB-Row hart geloescht."
            )
        if no_id3 and not args.no_id3_check:
            total_mp3_no_id3_est = int(len(no_id3) / id3_limit * mp3_count)
            if total_mp3_no_id3_est > 0:
                recs.append(
                    f"~{total_mp3_no_id3_est} MP3s ohne ID3-Song-ID (geschaetzt).\n"
                    f"  → 'ID3-Tags syncen' im Settings-Modal (Mode: 'missing only')."
                )
        if not recs:
            print("  Alles konsistent. DB und Platte sind im Sync.")
        else:
            for r in recs:
                print(f"  {r}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
