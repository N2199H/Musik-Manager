#!/usr/bin/env python3
"""Schneller Test: Safety-Gate funktioniert für root-scan UND subdir-scan.

Mockt scan_files UND extract_tags, damit der Test <5s läuft. Validiert
die Reconciliation-Logik in app/scanner/nas.py:run_scan.

Drei Szenarien:
  1. Sub-Ordner-Scan (Coverage <50%) → Reconciliation übersprungen
  2. Root-Scan mit fehlenden Files (Coverage >50%) → Reconciliation läuft
  3. Root-Scan mit enable_reconciliation=False → Reconcile nie

Nicht-Sub-Ordner Bug: ohne das neue Safety-Gate würde Szenario 1 alle
10.000+ Songs samt playlist_tracks löschen — exakt der Bug vom 9.6.2026.
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/home/openclaw/.openclaw/workspace/musik-manager")

from app.scanner import nas as scanner_nas


def _mock_scan_files(music_dir, test_db):
    """Sub-Dir: 1 File. Sonst: alle DB-Paths minus 5."""
    if "Pink Floyd" in str(music_dir):
        return ["/tmp/nas-musik/Pink Floyd - A Momentary Lapse Of Reason/test.mp3"]
    con = sqlite3.connect(test_db)
    paths = [r[0] for r in con.execute("SELECT filepath FROM songs").fetchall()]
    con.close()
    return paths[:-5]  # 5 Files fehlen


def _mock_extract_tags(fp):
    return {
        "filepath": fp, "filename": os.path.basename(fp),
        "artist": "Mock", "title": "Mock",
        "album": "", "genre": "", "year": "",
        "duration_sec": 0.0, "bitrate_kbps": 0, "filesize": 0,
        "mm_song_id": None,
    }


def _counts(db_path: str) -> tuple[int, int]:
    con = sqlite3.connect(db_path)
    c = con.cursor()
    sg = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    pt = c.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
    con.close()
    return sg, pt


def main() -> int:
    repo_root = Path("/home/openclaw/.openclaw/workspace/musik-manager")
    production_db = repo_root / "musik.db"

    # === Test 1: Sub-Ordner-Scan → Reconciliation übersprungen ===
    test_db = "/tmp/musik-safety-gate-1.db"
    shutil.copy(production_db, test_db)
    print("=== Test 1: Sub-Ordner-Scan (1 File) → Reconcile SKIP ===")
    sg_before, pt_before = _counts(test_db)
    with patch.object(scanner_nas, "scan_files",
                      side_effect=lambda d: _mock_scan_files(d, test_db)), \
         patch.object(scanner_nas, "extract_tags", side_effect=_mock_extract_tags):
        result = scanner_nas.run_scan(
            music_dir="/tmp/nas-musik/Pink Floyd - A Momentary Lapse Of Reason",
            db_path=test_db, force_rescan=False,
        )
    sg_after, pt_after = _counts(test_db)
    print(f"  deleted={result['deleted']}  "
          f"reconciliation_skipped={result['reconciliation_skipped']}  "
          f"playlist_tracks: {pt_before}→{pt_after}")
    ok1 = (result["reconciliation_skipped"] is True
           and result["deleted"] == 0
           and pt_before == pt_after)
    print("  ✅ OK" if ok1 else "  ❌ FAIL")

    # === Test 2: Root-Scan mit fehlenden Files → Reconcile läuft ===
    print("\n=== Test 2: Root-Scan (5 fehlen) → Reconcile läuft ===")
    sg_before2, pt_before2 = _counts(test_db)
    with patch.object(scanner_nas, "scan_files",
                      side_effect=lambda d: _mock_scan_files(d, test_db)), \
         patch.object(scanner_nas, "extract_tags", side_effect=_mock_extract_tags):
        result2 = scanner_nas.run_scan(
            music_dir="/tmp/nas-musik",
            db_path=test_db, force_rescan=False,
        )
    sg_after2, pt_after2 = _counts(test_db)
    print(f"  deleted={result2['deleted']}  "
          f"reconciliation_skipped={result2['reconciliation_skipped']}  "
          f"songs: {sg_before2}→{sg_after2}  playlist_tracks: {pt_before2}→{pt_after2}")
    ok2 = (result2["reconciliation_skipped"] is False
           and result2["deleted"] == 5
           and sg_after2 == sg_before2 - 5)
    print("  ✅ OK" if ok2 else "  ❌ FAIL")

    # === Test 3: enable_reconciliation=False → Reconcile nie ===
    test_db3 = "/tmp/musik-safety-gate-3.db"
    shutil.copy(production_db, test_db3)
    print("\n=== Test 3: Root-Scan mit enable_reconciliation=False → skip ===")
    sg_before3, pt_before3 = _counts(test_db3)
    with patch.object(scanner_nas, "scan_files",
                      side_effect=lambda d: _mock_scan_files(d, test_db3)), \
         patch.object(scanner_nas, "extract_tags", side_effect=_mock_extract_tags):
        result3 = scanner_nas.run_scan(
            music_dir="/tmp/nas-musik",
            db_path=test_db3, force_rescan=False,
            enable_reconciliation=False,
        )
    sg_after3, pt_after3 = _counts(test_db3)
    print(f"  deleted={result3['deleted']}  "
          f"reconciliation_skipped={result3['reconciliation_skipped']}  "
          f"songs: {sg_before3}→{sg_after3}")
    ok3 = (result3["deleted"] == 0
           and sg_after3 == sg_before3
           and result3["reconciliation_skipped"] is False)
    print("  ✅ OK" if ok3 else "  ❌ FAIL")

    # Cleanup
    os.unlink(test_db)
    os.unlink(test_db3)

    print()
    if ok1 and ok2 and ok3:
        print("=== ALLE TESTS OK ===")
        return 0
    print("=== TESTS FEHLGESCHLAGEN ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
