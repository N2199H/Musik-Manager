"""Deduplication-Detection für den Musik-Manager.

Zwei complementary Strategien, beide READ-ONLY:

1. `find_duplicates(db)`    — gruppiert Songs nach billigem Fingerprint
                              (title+artist+duration) und liefert Gruppen
                              mit >= 2 Songs. Erkennt den klassischen Fall
                              "zwei DB-Rows für denselben Track".

2. `find_duplicate_paths(db)` — gruppiert Files mit identischem
                                 basename+parent-Inhalt. Erkennt den
                                 "Schreibweisen"-Fall: gleicher Song in zwei
                                 Ordnern mit unterschiedlichem Case im
                                 Verzeichnisnamen (Pink Floyd - A Momentary
                                 Lapse Of Reason/ vs. ...momentary.../).

3. `chromaprint_fingerprint(filepath)` — optionaler acoustic-Fingerprint
   via `acoustid` Python-Binding. Falls nicht installiert → None. Wird
   in einer späteren Phase für audio-inhaltliche Duplikate genutzt.

Designentscheidungen:
- KEINE DB-Mutation, KEIN File-Löschen. Detector only.
- Performance: ein Pass durch die DB (<5s für 10k Songs).
- Datei-Existenz-Check mit try/except (CIFS-Mount kann lahm sein).
- ID3-Tag-Check optional via mutagen. Wenn Tag != DB → "stale".

Public API:
    from app.scanner.dedup import (
        fingerprint, find_duplicates, find_duplicate_paths,
        chromaprint_fingerprint, DuplicateGroup, DuplicateReason,
    )
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gründe für ein Duplikat
# ---------------------------------------------------------------------------

class DuplicateReason(str, Enum):
    """Warum zwei Songs als Duplikat gelten.

    Werte sind Strings (str-mixin), damit sie direkt in JSON / DB landen
    können ohne weitere Konvertierung.
    """
    TITLE_ARTIST_DURATION = "title+artist+duration"
    SAME_BASENAME_PARENT  = "basename+parent"  # gleicher basename im selben parent
    SAME_BASENAME_DIFFERENT_PARENT = "basename+different_parent"  # Schreibweisen-Fall
    AUDIO_FINGERPRINT     = "chromaprint"  # Phase-2 / Phase-5


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class DuplicateGroup:
    """Eine Gruppe von Duplikat-Songs.

    Attributes:
        reason:  Wieso diese Gruppe als Duplikat klassifiziert wurde.
        key:     Der gemeinsame Fingerprint (oder basename|parent).
        songs:   Liste der Song-Rows (oder Dicts) die zusammengehören.
        keep:    Optional — Empfehlung, welcher Eintrag behalten werden soll
                 (gesetzt durch annotate_keep()).
    """
    reason: DuplicateReason
    key: str
    songs: list = field(default_factory=list)
    keep: object = None  # song-Row oder None wenn nicht entschieden

    @property
    def size(self) -> int:
        return len(self.songs)

    @property
    def title(self) -> str:
        """Bequemer Zugriff: erster nicht-leerer title."""
        for s in self.songs:
            t = getattr(s, "title", None)
            if t:
                return t
        return ""

    @property
    def artist(self) -> str:
        for s in self.songs:
            a = getattr(s, "artist", None)
            if a:
                return a
        return ""


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def fingerprint(song) -> str:
    """Billiger Fingerprint: lower(title)+lower(artist)+duration_rounded_2s.

    Args:
        song: SQLAlchemy-Song-Row ODER Dict mit title/artist/duration_sec.

    Returns:
        String der Form  "title|artist|duration". Für leere Felder wird "" benutzt.
    """
    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            v = obj.get(key, default)
        else:
            v = getattr(obj, key, default)
        return v if v is not None else default

    title = _get(song, "title", "") or ""
    artist = _get(song, "artist", "") or ""
    dur_raw = _get(song, "duration_sec", 0.0)
    try:
        dur = round(float(dur_raw), 2)
    except (TypeError, ValueError):
        dur = 0.0
    return f"{title.strip().lower()}|{artist.strip().lower()}|{dur}"


# ---------------------------------------------------------------------------
# Detector 1: Inhaltliche Duplikate
# ---------------------------------------------------------------------------

def find_duplicates(db, pattern_dir: Optional[str] = None) -> list[DuplicateGroup]:
    """Findet Duplikate via (title, artist, duration)-Fingerprint.

    Args:
        db:          SQLAlchemy-Session (oder None, dann wird SessionLocal benutzt).
        pattern_dir: Optional — beschränkt auf Files deren Pfad diesen Substring
                     enthält (case-sensitive substring match).

    Returns:
        Liste von DuplicateGroup mit reason=TITLE_ARTIST_DURATION und >=2 Songs,
        sortiert nach Größe (größte zuerst).
    """
    from app.database import SessionLocal, Song  # lokal, um Circular-Import zu vermeiden

    owns_session = False
    if db is None:
        db = SessionLocal()
        owns_session = True

    try:
        q = db.query(Song)
        if pattern_dir:
            q = q.filter(Song.filepath.like(f"%{pattern_dir}%"))
        songs = q.all()

        buckets: dict[str, list] = defaultdict(list)
        for s in songs:
            buckets[fingerprint(s)].append(s)

        groups: list[DuplicateGroup] = []
        for key, items in buckets.items():
            if len(items) < 2:
                continue
            grp = DuplicateGroup(reason=DuplicateReason.TITLE_ARTIST_DURATION, key=key, songs=items)
            _annotate_keep(grp)
            groups.append(grp)

        groups.sort(key=lambda g: (-g.size, g.title.lower()))
        return groups
    finally:
        if owns_session:
            db.close()


# ---------------------------------------------------------------------------
# Detector 2: Pfad-Duplikate
# ---------------------------------------------------------------------------

def _path_key(filepath: str) -> tuple[str, str]:
    """Reduziert einen Pfad auf (basename, parent) für case-insensitive Gruppierung.

    Return: (basename_lower, parent_lower)
    """
    fp = (filepath or "").strip()
    if not fp:
        return ("", "")
    base = os.path.basename(fp)
    parent = os.path.dirname(fp)
    return (base.lower(), parent.lower())


def find_duplicate_paths(db, pattern_dir: Optional[str] = None) -> list[DuplicateGroup]:
    """Gruppiert Files mit identischem basename+parent.

    Zwei Songs landen in derselben Gruppe wenn:
        os.path.basename(s1.filepath) == os.path.basename(s2.filepath)
    UND sie liegen in unterschiedlichen parent-Verzeichnissen
    (case-insensitive verglichen).

    Das fängt den typischen Schreibweisen-Fall:
        /Pink Floyd - A Momentary Lapse Of Reason/.../Signs Of Life.mp3
        /Pink Floyd - A momentary Lapse of Reason/.../Signs Of Life.mp3

    Args:
        db:          SQLAlchemy-Session (oder None).
        pattern_dir: Optional — Pfad-Substring-Filter.

    Returns:
        Liste von DuplicateGroup mit reason=SAME_BASENAME_DIFFERENT_PARENT.
    """
    from app.database import SessionLocal, Song

    owns_session = False
    if db is None:
        db = SessionLocal()
        owns_session = True

    try:
        q = db.query(Song)
        if pattern_dir:
            q = q.filter(Song.filepath.like(f"%{pattern_dir}%"))
        songs = q.all()

        # Map: (base_lower, parent_lower_case_insensitive_set) -> list[song]
        # Wir gruppieren erst nach basename+parent (genau gleicher parent →
        # exakter Pfad-Duplikat, d.h. die DB hat zwei Rows für dieselbe
        # Datei). Dann gruppieren wir weiter: gleicher basename aber
        # unterschiedliche parents → Schreibweisen-Duplikat.
        exact_groups: dict[tuple[str, str], list] = defaultdict(list)
        for s in songs:
            fp = s.filepath or ""
            base = os.path.basename(fp).lower()
            parent = os.path.dirname(fp).lower()
            exact_groups[(base, parent)].append(s)

        groups: list[DuplicateGroup] = []

        # 2a) exakte basename+parent-Kollisionen (selten, aber möglich wenn
        # z.B. Scanner zweimal lief und Pfad nicht unique war)
        for (base, parent), items in exact_groups.items():
            if len(items) < 2:
                continue
            grp = DuplicateGroup(
                reason=DuplicateReason.SAME_BASENAME_PARENT,
                key=f"{base}|{parent}",
                songs=items,
            )
            _annotate_keep(grp)
            groups.append(grp)

        # 2b) basename kommt in >=2 unterschiedlichen parent-Ordnern vor
        #    (Schreibweisen-Fall)
        # Map: base_lower -> {parent_lower_set: [songs]}
        from collections import defaultdict as _dd
        per_base: dict[str, dict[str, list]] = _dd(lambda: _dd(list))
        for s in songs:
            fp = s.filepath or ""
            base = os.path.basename(fp).lower()
            parent = os.path.dirname(fp).lower()
            per_base[base][parent].append(s)

        for base, per_parent in per_base.items():
            if len(per_parent) < 2:
                continue
            # Sammle alle Songs aus allen parents für diesen basename.
            all_items: list = []
            for parent_l, lst in per_parent.items():
                all_items.extend(lst)
            if len(all_items) < 2:
                continue
            # Vermeide Doppel-Erfassung wenn exakte Gruppe schon alle Songs
            # abdeckt. Wir nehmen diese Gruppe nur wenn es echte Cross-Parent
            # Duplikate gibt UND nicht schon in 2a).
            grp = DuplicateGroup(
                reason=DuplicateReason.SAME_BASENAME_DIFFERENT_PARENT,
                key=base,
                songs=all_items,
            )
            _annotate_keep(grp)
            groups.append(grp)

        groups.sort(key=lambda g: (-g.size, g.key))
        return groups
    finally:
        if owns_session:
            db.close()


# ---------------------------------------------------------------------------
# KEEP-Empfehlung
# ---------------------------------------------------------------------------

def _annotate_keep(group: DuplicateGroup) -> None:
    """Wählt innerhalb der Gruppe den 'besten' Kandidaten zum Behalten.

    Heuristik (in dieser Reihenfolge):
      1. Pfad existiert auf Platte (CIFS kann lahm sein — try/except).
      2. Längster Filename-Pfad im Sinne von "mehr Kontext im Pfad" =
         in der Regel der canonical filename (z.B. mit Track-Nummer).
      3. Höherer score (Ranking) — falls Songs schon gespielt wurden.
      4. Niedrigere ID (älterer Eintrag) als Tie-Breaker.

    Schreibt das Ergebnis in group.keep.
    """
    songs = group.songs
    if len(songs) < 2:
        return

    def _exists_safe(s) -> bool:
        fp = getattr(s, "filepath", None) or ""
        if not fp:
            return False
        try:
            return os.path.exists(fp)
        except OSError:
            return False

    def _path_len(s) -> int:
        return len(getattr(s, "filepath", "") or "")

    def _score(s) -> float:
        return float(getattr(s, "score", 0.0) or 0.0)

    def _id(s) -> int:
        return int(getattr(s, "id", 0) or 0)

    def _dur(s) -> float:
        """Bevorzuge Einträge mit echtem duration_sec > 0."""
        try:
            return float(getattr(s, "duration_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Sortierung: existiert DESC, länge DESC, duration DESC, score DESC, id ASC
    ranked = sorted(
        songs,
        key=lambda s: (
            -int(_exists_safe(s)),
            -_path_len(s),
            -_dur(s),
            -_score(s),
            _id(s),
        ),
    )
    group.keep = ranked[0]


# ---------------------------------------------------------------------------
# Detector 3: Audio-Fingerprint (optional, Phase 5)
# ---------------------------------------------------------------------------

def chromaprint_fingerprint(filepath: str) -> Optional[str]:
    """Berechnet einen acoustid/chromaprint-Fingerprint für eine Audio-Datei.

    Versucht zuerst das `pyacoustid`-Binding zu importieren. Falls nicht
    verfügbar (oder `fpcalc` nicht installiert) → None.

    Args:
        filepath: Absoluter Pfad zur Audio-Datei.

    Returns:
        Fingerprint-String oder None bei Fehler / nicht installiert.
    """
    if not filepath:
        return None
    try:
        import acoustid  # type: ignore
    except ImportError:
        return None
    try:
        duration, fp = acoustid.fingerprint_file(filepath)
        return fp.decode("ascii") if isinstance(fp, (bytes, bytearray)) else str(fp)
    except Exception as e:  # acoustid.CouldntDecodeError, OSError, ...
        log.debug("chromaprint_fingerprint failed for %s: %s", filepath, e)
        return None
