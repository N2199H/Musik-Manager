"""dedup — CLI-Tool zur Duplikat-Erkennung im Musik-Manager.

Rein informativ, KEINE Schreibaktionen. Gedacht als Diagnose-Tool
das beantwortet: "Welche Songs / Files sind Duplikate wovon?"

Aufruf:
    python -m app.tools.dedup
    python -m app.tools.dedup --json
    python -m app.tools.dedup --limit 20
    python -m app.tools.dedup --pattern-dir "Pink Floyd"
    python -m app.tools.dedup --mode paths    # nur Pfad-Duplikate
    python -m app.tools.dedup --mode content  # nur Inhalt (Default)
    python -m app.tools.dedup --mode all      # beides
    python -m app.tools.dedup --keep-best     # KEEP-Empfehlung anzeigen

Detectors (siehe app.scanner.dedup):
  - content: gruppiert nach (title, artist, duration). Erkennt mehrere
             DB-Rows für denselben Track.
  - paths:  gruppiert nach basename+parent (case-insensitive). Erkennt
             den Schreibweisen-Fall (Pink Floyd - A Momentary Lapse Of
             Reason/ vs. ...momentary...).
  - all:    beides.

Output:
  Pro Gruppe eine Header-Zeile + 1+ Zeilen für die enthaltenen Songs.
  Mit --keep-best wird der "beste" Kandidat mit [KEEP] markiert, der
  Rest mit [DUP ].
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

from app.database import SessionLocal
from app.scanner.dedup import (
    DuplicateGroup,
    DuplicateReason,
    find_duplicates,
    find_duplicate_paths,
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen für die Anzeige
# ---------------------------------------------------------------------------

def fmt_path(p: str, max_len: int = 60) -> str:
    if not p:
        return ""
    if len(p) <= max_len:
        return p
    return "..." + p[-(max_len - 3):]


def _file_info(s) -> dict:
    """Sammelt Metadaten zu einem Song für die tabellarische Anzeige.

    Returns:
        dict mit id, filename, id3_status, mtime_iso, exists, filesize, score.
        id3_status ist "ok" | "stale" | "missing" | "n/a".
    """
    fp = getattr(s, "filepath", "") or ""
    info = {
        "id": getattr(s, "id", None),
        "filepath": fp,
        "filename": os.path.basename(fp),
        "parent": os.path.dirname(fp),
        "exists": False,
        "mtime_iso": "",
        "filesize": getattr(s, "filesize", None),
        "id3_status": "n/a",
        "score": getattr(s, "score", None),
        "duration_sec": getattr(s, "duration_sec", None),
    }
    if not fp:
        info["id3_status"] = "n/a"
        return info
    # Existenz + mtime (CIFS kann lahm sein → try/except)
    try:
        if os.path.exists(fp):
            info["exists"] = True
            try:
                st = os.stat(fp)
                info["filesize"] = info["filesize"] or st.st_size
                info["mtime_iso"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
            except OSError:
                pass
    except OSError:
        info["exists"] = False

    # ID3-Check (nur MP3, mit mutagen) — billig, nur Title+Artist lesen
    if fp.lower().endswith(".mp3") and info["exists"]:
        try:
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tags = ID3(fp, translate=False)
                tag_title = str(tags.get("TIT2", "")) if "TIT2" in tags else ""
                tag_artist = str(tags.get("TPE1", "")) if "TPE1" in tags else ""
                if not tag_title and not tag_artist:
                    info["id3_status"] = "missing"
                else:
                    db_title = (getattr(s, "title", "") or "").strip()
                    db_artist = (getattr(s, "artist", "") or "").strip()
                    # Stale = tag vorhanden, aber != DB-Spiegel
                    # (Tippfehler im DB oder Tag wurde später nicht gesynct)
                    if (tag_title and tag_title != db_title) or \
                       (tag_artist and tag_artist != db_artist):
                        info["id3_status"] = "stale"
                    else:
                        info["id3_status"] = "ok"
            except ID3NoHeaderError:
                info["id3_status"] = "missing"
        except Exception:
            # mutagen kann auf kaputten Files / CIFS-Quirks crashen
            info["id3_status"] = "n/a"
    return info


def _print_group_table(group: DuplicateGroup, idx: int, show_keep: bool) -> None:
    """Eine Gruppe tabellarisch ausgeben.

    Format:
        Gruppe 12: "Learning To Fly" — Pink Floyd (2 Duplikate)  [title+artist+duration]
          [KEEP ]  id=2927 dur=293.3 | id3=ok   | mtime=2024-10-12 | path=.../Pink Floyd - 02 - Learning To Fly.mp3
          [DUP  ]  id= 571 dur=293.3 | id3=ok   | mtime=2024-10-12 | path=.../Pink Floyd - 02 - Learning To Fly.mp3
    """
    # Repräsentativer title/artist (längster nicht-leerer)
    title = group.title or "(kein Titel)"
    artist = group.artist or ""
    head = f'Gruppe {idx}: "{title}"'
    if artist:
        head += f" — {artist}"
    head += f"  ({group.size} Duplikate)  [{group.reason.value}]"
    print(head)
    keep_id = id(group.keep) if group.keep is not None else None
    for s in group.songs:
        info = _file_info(s)
        marker = "[KEEP ]" if show_keep and keep_id is not None and info["id"] == getattr(group.keep, "id", None) else \
                 ("[DUP  ]" if show_keep else "[     ]")
        line = (
            f"  {marker}  "
            f"id={info['id']:<6} "
            f"dur={info['duration_sec']:.1f} | "
            f"id3={info['id3_status']:<7} | "
            f"mtime={info['mtime_iso'] or '?':<10} | "
            f"path={fmt_path(info['filepath'])}"
        )
        print(line)
    print()


def _group_to_dict(g: DuplicateGroup) -> dict:
    """Serialisiert eine Gruppe als Dict (für --json)."""
    keep_id = getattr(g.keep, "id", None) if g.keep is not None else None
    items = []
    for s in g.songs:
        info = _file_info(s)
        items.append({
            "id": info["id"],
            "filepath": info["filepath"],
            "filename": info["filename"],
            "parent": info["parent"],
            "exists": info["exists"],
            "mtime": info["mtime_iso"],
            "filesize": info["filesize"],
            "id3_status": info["id3_status"],
            "score": info["score"],
            "duration_sec": info["duration_sec"],
            "is_keep": info["id"] == keep_id,
        })
    return {
        "reason": g.reason.value,
        "key": g.key,
        "size": g.size,
        "title": g.title,
        "artist": g.artist,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Duplikat-Detection im Musik-Manager (read-only)",
    )
    parser.add_argument(
        "--mode", choices=["content", "paths", "all"], default="content",
        help="Welcher Detector: 'content' (title+artist+duration, default), "
             "'paths' (basename+parent, case-insensitive), oder 'all' (beides).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output als JSON statt menschenlesbar.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max N Gruppen ausgeben (0 = alle).",
    )
    parser.add_argument(
        "--pattern-dir", default=None,
        help="Beschränkt auf Files deren Pfad diesen Substring enthält "
             "(z.B. 'Pink Floyd').",
    )
    parser.add_argument(
        "--keep-best", action="store_true",
        help="KEEP/DUP-Markierung anzeigen: welcher Eintrag sollte "
             "behalten werden? Heuristik: existiert+Pfad-Länge+Score+ID.",
    )
    parser.add_argument(
        "--no-id3", action="store_true",
        help="ID3-Tag-Check überspringen (schneller, keine CIFS-Reads).",
    )
    args = parser.parse_args()

    t_start = time.time()
    db = SessionLocal()
    try:
        all_groups: list[DuplicateGroup] = []
        detectors_used: list[str] = []

        if args.mode in ("content", "all"):
            t0 = time.time()
            g_content = find_duplicates(db, pattern_dir=args.pattern_dir)
            detectors_used.append(
                f"content(title+artist+duration) → {len(g_content)} Gruppen in {time.time()-t0:.2f}s"
            )
            all_groups.extend(g_content)

        if args.mode in ("paths", "all"):
            t0 = time.time()
            g_paths = find_duplicate_paths(db, pattern_dir=args.pattern_dir)
            detectors_used.append(
                f"paths(basename+parent)          → {len(g_paths)} Gruppen in {time.time()-t0:.2f}s"
            )
            all_groups.extend(g_paths)

        # Wenn beides aktiv: kombiniert nach Größe sortieren
        if args.mode == "all":
            all_groups.sort(key=lambda g: (-g.size, g.reason.value, g.key))

        if args.limit > 0:
            all_groups = all_groups[:args.limit]

        elapsed = time.time() - t_start

        if args.json:
            payload = {
                "detectors": detectors_used,
                "elapsed_sec": round(elapsed, 3),
                "pattern_dir": args.pattern_dir,
                "group_count": len(all_groups),
                "groups": [_group_to_dict(g) for g in all_groups],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print("=" * 78)
            print("  Musik-Manager — Duplikat-Detection")
            print("=" * 78)
            print(f"  Pattern:    {args.pattern_dir or '(alle)'}")
            print(f"  Mode:       {args.mode}")
            print(f"  KEEP-Reco:  {'ja' if args.keep_best else 'nein'}")
            print(f"  Detectoren:")
            for d in detectors_used:
                print(f"    - {d}")
            print(f"  Gruppen total: {len(all_groups)}")
            print(f"  Dauer:         {elapsed:.2f}s")
            print()

            if not all_groups:
                print("  Keine Duplikate gefunden.")
            else:
                for i, g in enumerate(all_groups, 1):
                    _print_group_table(g, i, show_keep=args.keep_best)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
