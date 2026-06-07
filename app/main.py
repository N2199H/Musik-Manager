"""FastAPI Backend für den Musik-Manager — Phase 2"""

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text, func
from sqlalchemy.orm import Session
import subprocess
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import http.server
import socketserver
import logging

from datetime import datetime
from .database import SessionLocal, Song, Playlist, PlaylistTrack, engine, init_db

log = logging.getLogger(__name__)

app = FastAPI(
    title="🎵 Musik-Manager API",
    description="REST-API für NAS-Musikverwaltung mit Sonos-Steuerung",
    version="2.0.0",
)


# === Mini-HTTP-Server für /tmp/nas-musik/ (Port 8898) ===
# Sonos kann SMB-Shares (x-file-cifs://) nicht in der Queue navigieren (UPnP 711).
# Lösung: NAS-Files via HTTP-Stream servieren. Sonos behandelt http:// als
# "Track"-Format und unterstützt next/seek korrekt.
NAS_HTTP_PORT = 8898
_nas_http_server = None

class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=NAS_MUSIC_PATH, **kwargs)
    def log_message(self, *args, **kwargs):
        pass  # leise

def _start_nas_http_server():
    global _nas_http_server
    if _nas_http_server is not None:
        return
    try:
        from http.server import ThreadingHTTPServer
        ThreadingHTTPServer.allow_reuse_address = True
        srv = ThreadingHTTPServer(("", NAS_HTTP_PORT), _SilentHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="nas-http")
        t.start()
        _nas_http_server = srv
        print(f"[startup] NAS-HTTP-Server läuft auf Port {NAS_HTTP_PORT} (serving {NAS_MUSIC_PATH})")
    except OSError as e:
        # Port belegt (z. B. weil er schon läuft) — kein Problem
        print(f"[startup] NAS-HTTP-Server: Port {NAS_HTTP_PORT} belegt ({e}), vermutlich schon aktiv")


@app.on_event("startup")
def startup():
    """Tabellen erstellen + NAS-HTTP-Server starten + Sonos-Speaker discoveren"""
    init_db()
    _start_nas_http_server()
    # Sonos-Speaker beim Start frisch einlesen, damit /api/sonos/speakers
    # beim ersten Request sofort antwortet (kein Warten auf Discover).
    speakers = _discover_sonos_speakers()
    print(f"[startup] {len(speakers)} Sonos-Speaker erkannt: {', '.join(speakers.keys())}")

# CORS erlauben (für lokales Frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === Dependency ===
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# === Pydantic Models ===
class SongResponse(BaseModel):
    id: int
    artist: str | None = ""
    title: str | None = ""
    album: str | None = ""
    genre: str | None = ""
    year: str | None = ""
    duration_sec: float | None = 0
    bitrate_kbps: int | None = 0
    bpm: float | None = None
    energy: float | None = None
    valence: float | None = None
    danceability: float | None = None
    mood: str | None = ""
    tags: str | None = ""
    filepath: str | None = ""
    score: float = 50.0

    class Config:
        from_attributes = True

class PlaylistCreate(BaseModel):
    name: str


class PlaylistUpdate(BaseModel):
    name: str | None = None


class PlaylistAddTrack(BaseModel):
    song_id: int
    position: int | None = None


class StatsResponse(BaseModel):
    total_songs: int
    with_artist: int
    with_genre: int
    with_album: int
    with_year: int
    with_bpm: int
    genres: list[dict]
    years: list[dict]


class SonosCommand(BaseModel):
    speaker: str = "Küche"  # für Rückwärtskompatibilität (1 Speaker)
    speakers: list[str] | None = None  # neu: Liste von Speakern für Party-Modus
    action: str = "play"  # play, pause, stop, volume, group, ungroup
    song_id: int | None = None
    playlist_name: str | None = None
    uri: str | None = None
    volume: int | None = None
    shuffle: bool = True

    def get_speakers(self) -> list[str]:
        """Liefert die zu spielenden Speaker. speakers > speaker > [speaker]."""
        if self.speakers:
            return [s for s in self.speakers if s]
        return [self.speaker] if self.speaker else []


# === Endpunkte ===

# API-Übersicht unter /api/ erreichbar
@app.get("/api")
def api_root():
    return {"message": "🎵 Musik-Manager API v2.0", "docs": "/docs"}


@app.get("/api/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)):
    """Datenbank-Statistiken"""
    total = db.query(func.count(Song.id)).scalar()
    with_artist = db.query(func.count(Song.id)).filter(Song.artist != "").scalar()
    with_genre = db.query(func.count(Song.id)).filter(Song.genre != "").scalar()
    with_album = db.query(func.count(Song.id)).filter(Song.album != "").scalar()
    with_year = db.query(func.count(Song.id)).filter(Song.year != "", Song.year != None).scalar()
    with_bpm = db.query(func.count(Song.id)).filter(Song.bpm != None).scalar()

    # Top-Genres
    genres = db.query(Song.genre, func.count(Song.id).label("count"))\
        .filter(Song.genre != "")\
        .group_by(Song.genre)\
        .order_by(func.count(Song.id).desc())\
        .limit(30).all()
    genre_list = [{"genre": g[0][:60], "count": g[1]} for g in genres]

    # Jahr-Verteilung
    years = db.query(Song.year, func.count(Song.id).label("count"))\
        .filter(Song.year != "", Song.year != None)\
        .group_by(Song.year)\
        .order_by(func.count(Song.id).desc())\
        .limit(20).all()
    year_list = [{"year": y[0], "count": y[1]} for y in years]

    return StatsResponse(
        total_songs=total,
        with_artist=with_artist,
        with_genre=with_genre,
        with_album=with_album,
        with_year=with_year,
        with_bpm=with_bpm,
        genres=genre_list,
        years=year_list,
    )


@app.get("/api/songs", response_model=list[SongResponse])
def search_songs(
    q: str | None = Query(None, description="Suchbegriff (Artist, Titel, Album)"),
    artist: str | None = Query(None, description="Filter nach Künstler"),
    genre: str | None = Query(None, description="Filter nach Genre"),
    year: str | None = Query(None, description="Filter nach Jahr"),
    album: str | None = Query(None, description="Filter nach Album"),
    mood: str | None = Query(None, description="Filter nach Stimmung"),
    bpm_min: float | None = Query(None, description="Min BPM"),
    bpm_max: float | None = Query(None, description="Max BPM"),
    sort: str = Query("artist", description="Sortierung: artist, title, year, bpm, random"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Songs suchen und filtern"""
    query = db.query(Song)

    if q:
        search = f"%{q}%"
        query = query.filter(
            (Song.artist.ilike(search)) |
            (Song.title.ilike(search)) |
            (Song.album.ilike(search))
        )

    if artist:
        query = query.filter(Song.artist.ilike(f"%{artist}%"))
    if genre:
        query = query.filter(Song.genre.ilike(f"%{genre}%"))
    if year:
        query = query.filter(Song.year == year)
    if album:
        query = query.filter(Song.album.ilike(f"%{album}%"))
    if mood:
        query = query.filter(Song.mood.ilike(f"%{mood}%"))
    if bpm_min is not None:
        query = query.filter(Song.bpm >= bpm_min)
    if bpm_max is not None:
        query = query.filter(Song.bpm <= bpm_max)

    # Sortierung
    # Dropdown-Werte (Legacy, weiter unterstützt):
    #   artist / title / year / bpm / score / random
    # Klick-auf-Header-Werte (neu, mit Richtung):
    #   <field>_asc / <field>_desc   (field ∈ id, title, artist, album, genre, year, score)
    #   "id_asc" = Original-Reihenfolge (DB-Einfügereihenfolge)
    if sort == "title":
        query = query.order_by(Song.title, Song.artist)
    elif sort == "title_asc":
        query = query.order_by(Song.title.asc(), Song.artist)
    elif sort == "title_desc":
        query = query.order_by(Song.title.desc(), Song.artist)
    elif sort == "artist_asc":
        query = query.order_by(Song.artist.asc(), Song.title)
    elif sort == "artist_desc":
        query = query.order_by(Song.artist.desc(), Song.title)
    elif sort == "album_asc":
        query = query.order_by(Song.album.asc(), Song.artist, Song.title)
    elif sort == "album_desc":
        query = query.order_by(Song.album.desc(), Song.artist, Song.title)
    elif sort == "genre_asc":
        query = query.order_by(Song.genre.asc(), Song.artist, Song.title)
    elif sort == "genre_desc":
        query = query.order_by(Song.genre.desc(), Song.artist, Song.title)
    elif sort == "year":
        query = query.order_by(Song.year.desc())
    elif sort == "year_asc":
        query = query.order_by(Song.year.asc().nullslast(), Song.artist, Song.title)
    elif sort == "year_desc":
        query = query.order_by(Song.year.desc().nullslast(), Song.artist, Song.title)
    elif sort == "bpm":
        query = query.order_by(Song.bpm.desc().nullslast())
    elif sort == "score":
        query = query.order_by(Song.score.desc().nullslast())
    elif sort == "score_asc":
        query = query.order_by(Song.score.asc().nullslast(), Song.artist, Song.title)
    elif sort == "score_desc":
        query = query.order_by(Song.score.desc().nullslast(), Song.artist, Song.title)
    elif sort == "id_asc":
        query = query.order_by(Song.id.asc())  # unsortiert = Original-Reihenfolge
    elif sort == "random":
        query = query.order_by(func.random())
    else:  # artist (default)
        query = query.order_by(Song.artist, Song.title)

    results = query.offset(offset).limit(limit).all()
    return [SongResponse(**{k: getattr(s, k, None) for k in SongResponse.model_fields}) for s in results]


@app.get("/api/songs/{song_id}", response_model=SongResponse)
def get_song(song_id: int, db: Session = Depends(get_db)):
    """Einzelnen Song abrufen"""
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Song nicht gefunden")
    return SongResponse(**{k: getattr(song, k, None) for k in SongResponse.model_fields})


# === Play-Event für Song-Ranking (EWMA-basiert) ===
# Frontend schickt Events, wenn:
#   - Song komplett gespielt wurde (Auto-Advance → neuer Song im Poll)
#   - User überspringt manuell (Nächster-Titel-Klick)
#   - User klickt 👍/👎 in der Now-Playing-Bar
# rel_pct wird vom Frontend aus letztem Polling abgeleitet (rel_time / track_duration).

class PlayEventRequest(BaseModel):
    event: str  # "completed" | "skipped_50_90" | "skipped_10_50" | "skipped_lt_10" | "like" | "dislike"


@app.post("/api/songs/{song_id}/play-event")
def record_play_event(song_id: int, req: PlayEventRequest, db: Session = Depends(get_db)):
    """Speichert ein Play-Event und updated den Song-Score via EWMA.

    Akzeptiert auch "skipped_lt_10" — in dem Fall wird der Score NICHT
    geändert (Test/Falsches Lied), der Endpoint bestätigt das aber mit
    "changed": False, damit das Frontend Feedback bekommt.

    Returns: {"song_id", "event", "score": <neuer Wert>, "changed": bool}
    """
    from .scoring import update_score, compute_event

    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Song nicht gefunden")

    # compute_event mappt eine gespielte Prozentzahl (0.0–1.0) auf den Event-Namen.
    # Für Frontend-Komfort akzeptieren wir entweder das fertige Event ODER
    # die Prozentzahl (rel_pct) — das spart Logik im Frontend.
    if req.event.startswith("rel_pct:"):
        try:
            rel_pct = float(req.event.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Ungültiges rel_pct in '{req.event}'")
        event = compute_event(rel_pct)
    else:
        event = req.event

    old_score = song.score if song.score is not None else 50.0
    new_score = update_score(old_score, event)

    if new_score != old_score:
        song.score = new_score
        db.commit()
        db.refresh(song)

        # ID3-Tag-Sync: schreibe Score + Song-ID + POPM in die MP3.
        # DB bleibt Source of Truth — Fehler hier blockieren die Response
        # NICHT, der User soll sein Rating sehen können auch wenn die MP3
        # nicht erreichbar/kaputt ist.
        try:
            from .id3_sync import write_score_to_mp3
            ok, msg = write_score_to_mp3(song.filepath, song_id, new_score)
            if not ok:
                log.warning("ID3-Sync fehlgeschlagen für song_id=%d: %s",
                            song_id, msg)
        except Exception as e:
            # Letzter Fangschirm — write_score_to_mp3 fängt schon viel ab,
            # aber Import-Fehler oder kaputte mutagen-Internals sollen den
            # Rating-Flow nicht killen.
            log.warning("ID3-Sync Exception für song_id=%d: %s",
                        song_id, e)

    return {
        "song_id": song_id,
        "event": event,
        "score": round(new_score, 2),
        "changed": new_score != old_score,
    }


@app.get("/api/artists")
def list_artists(
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Künstler auflisten (optional mit Suche)"""
    query = db.query(Song.artist, func.count(Song.id).label("song_count"))\
        .filter(Song.artist != "")\
        .group_by(Song.artist)

    if q:
        query = query.filter(Song.artist.ilike(f"%{q}%"))

    results = query.order_by(func.count(Song.id).desc())\
        .limit(limit).all()
    return [{"artist": r[0], "song_count": r[1]} for r in results]


@app.get("/api/genres")
def list_genres(db: Session = Depends(get_db)):
    """Alle Genres mit Song-Anzahl"""
    results = db.query(Song.genre, func.count(Song.id).label("count"))\
        .filter(Song.genre != "")\
        .group_by(Song.genre)\
        .order_by(func.count(Song.id).desc())\
        .all()
    return [{"genre": r[0], "count": r[1]} for r in results]


@app.get("/api/albums")
def list_albums(
    artist: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Alben auflisten (optional nach Künstler gefiltert)"""
    query = db.query(Song.album, Song.artist, Song.year, func.count(Song.id).label("track_count"))\
        .filter(Song.album != "")\
        .group_by(Song.album, Song.artist, Song.year)

    if artist:
        query = query.filter(Song.artist.ilike(f"%{artist}%"))

    results = query.order_by(Song.artist, Song.album)\
        .limit(limit).all()
    return [{"album": r[0], "artist": r[1], "year": r[2] or "", "track_count": r[3]} for r in results]


# === Playlist-Endpunkte ===

@app.get("/api/playlists")
def list_playlists(db: Session = Depends(get_db)):
    """Alle Playlists auflisten"""
    playlists = db.query(Playlist).order_by(Playlist.name).all()
    result = []
    for p in playlists:
        track_count = db.query(func.count(PlaylistTrack.id))\
            .filter(PlaylistTrack.playlist_id == p.id).scalar()
        result.append({
            **p.to_dict(),
            "track_count": track_count,
        })
    return result


@app.post("/api/playlists")
def create_playlist(pl: PlaylistCreate, db: Session = Depends(get_db)):
    """Neue Playlist erstellen"""
    existing = db.query(Playlist).filter(Playlist.name == pl.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Playlist existiert bereits")
    pl_obj = Playlist(name=pl.name, created_at=datetime.now().isoformat(), updated_at=datetime.now().isoformat())
    db.add(pl_obj)
    db.commit()
    db.refresh(pl_obj)
    return pl_obj.to_dict()


@app.patch("/api/playlists/{playlist_id}")
def update_playlist(playlist_id: int, payload: PlaylistUpdate, db: Session = Depends(get_db)):
    """Playlist-Felder aktualisieren (aktuell nur name)."""
    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Name darf nicht leer sein")
        if new_name != pl.name:
            # Eindeutigkeit prüfen
            clash = db.query(Playlist).filter(Playlist.name == new_name, Playlist.id != playlist_id).first()
            if clash:
                raise HTTPException(status_code=409, detail=f"Name '{new_name}' wird bereits verwendet")
            pl.name = new_name

    pl.updated_at = datetime.now().isoformat()
    db.commit()
    db.refresh(pl)
    return pl.to_dict()


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int, db: Session = Depends(get_db)):
    """Playlist mit Tracks abrufen"""
    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

    tracks = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id)\
        .order_by(PlaylistTrack.position).all()

    songs = []
    for t in tracks:
        song = db.query(Song).filter(Song.id == t.song_id).first()
        if song:
            songs.append({**song.to_dict(), "position": t.position})

    return {**pl.to_dict(), "tracks": songs}


@app.post("/api/playlists/current/track")
def add_to_current_playing_playlist(track: PlaylistAddTrack, db: Session = Depends(get_db)):
    """Song zur aktuell laufenden Playlist hinzufügen (ein Klick, kein Modal).

    Findet die erste Playlist, die gerade auf einem Sonos-Speaker spielt
    (über den _sonos_playing-Cache), und fügt den Song dort ans Ende an.
    Aktualisiert sowohl DB als auch M3U-Datei.
    """
    # 1) Finde die aktuell laufende Playlist im In-Memory-Cache
    playing_playlist_name = None
    playing_speaker = None
    for speaker, info in _sonos_playing.items():
        if info.get("playlist_name"):
            playing_playlist_name = info["playlist_name"]
            playing_speaker = speaker
            break
    if not playing_playlist_name:
        raise HTTPException(
            status_code=400,
            detail="Es läuft gerade keine Playlist. Starte erst eine, dann füge Songs hinzu."
        )

    # 2) Playlist in DB finden
    pl = db.query(Playlist).filter(Playlist.name == playing_playlist_name).first()
    if not pl:
        raise HTTPException(
            status_code=404,
            detail=f"Laufende Playlist '{playing_playlist_name}' nicht in der DB gefunden."
        )

    # 3) Song hinzufügen (delegiere an bestehende Logik)
    result = add_track_to_playlist(pl.id, track, db)
    return {
        "message": result["message"],
        "playlist_name": pl.name,
        "speaker": playing_speaker,
        "position": result["position"],
        "m3u_synced": result.get("m3u_synced", False),
    }


@app.post("/api/playlists/{playlist_id}/tracks")
def add_track_to_playlist(playlist_id: int, track: PlaylistAddTrack, db: Session = Depends(get_db)):
    """Song zu Playlist hinzufügen und M3U-Datei aktualisieren"""
    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

    song = db.query(Song).filter(Song.id == track.song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Song nicht gefunden")

    # Position bestimmen
    if track.position is None:
        max_pos = db.query(func.max(PlaylistTrack.position))\
            .filter(PlaylistTrack.playlist_id == playlist_id).scalar() or 0
        position = max_pos + 1
    else:
        position = track.position

    pt = PlaylistTrack(playlist_id=playlist_id, song_id=track.song_id, position=position)
    db.add(pt)
    db.commit()

    # M3U-Datei aktualisieren (falls vorhanden)
    m3u_synced = False
    if pl.m3u_filepath:
        all_tracks = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id)\
            .order_by(PlaylistTrack.position).all()
        tracks_with_songs = []
        for t in all_tracks:
            s = db.query(Song).filter(Song.id == t.song_id).first()
            if s:
                tracks_with_songs.append((t, s))
        try:
            _export_m3u(pl, tracks_with_songs, pl.m3u_filepath)
            m3u_synced = True
        except Exception:
            m3u_synced = False

    result = {"message": f"Song '{song.artist} - {song.title}' zu Playlist '{pl.name}' hinzugefügt", "position": position}
    if pl.m3u_filepath:
        result["m3u_synced"] = m3u_synced
    return result


@app.post("/api/playlists/{playlist_id}/tracks/reorder")
def reorder_track_in_playlist(playlist_id: int, payload: dict, db: Session = Depends(get_db)):
    """Track in der Playlist um eine Position nach oben oder unten verschieben.

    Erwartet JSON-Body: {"song_id": int, "direction": "up" | "down"}.
    Aktualisiert die DB (atomar) und schreibt die M3U-Datei neu.
    """
    song_id = payload.get("song_id")
    direction = payload.get("direction")
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction muss 'up' oder 'down' sein")
    if song_id is None:
        raise HTTPException(status_code=400, detail="song_id fehlt")

    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

    # Tracks in aktueller Reihenfolge laden
    all_tracks = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id)\
        .order_by(PlaylistTrack.position).all()
    if not all_tracks:
        raise HTTPException(status_code=404, detail="Playlist hat keine Tracks")

    # Index des zu verschiebenden Tracks finden
    current_index = next(
        (i for i, t in enumerate(all_tracks) if t.song_id == song_id),
        None,
    )
    if current_index is None:
        raise HTTPException(status_code=404, detail="Track nicht in Playlist")

    swap_index = current_index - 1 if direction == "up" else current_index + 1
    if swap_index < 0 or swap_index >= len(all_tracks):
        # Bereits am Rand → kein Fehler, einfach nichts tun
        return {"message": "Bereits am Rand", "moved": False, "m3u_synced": False}

    a, b = all_tracks[current_index], all_tracks[swap_index]
    # Positionen tauschen (über Hilfswert -1, da UNIQUE-Einschränkung verletzt werden könnte)
    old_a, old_b = a.position, b.position
    a.position = -1
    db.flush()
    a.position = old_b
    b.position = old_a
    db.commit()

    # M3U-Datei neu schreiben
    m3u_synced = False
    if pl.m3u_filepath:
        ordered = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id)\
            .order_by(PlaylistTrack.position).all()
        tracks_with_songs = []
        for t in ordered:
            s = db.query(Song).filter(Song.id == t.song_id).first()
            if s:
                tracks_with_songs.append((t, s))
        try:
            _export_m3u(pl, tracks_with_songs, pl.m3u_filepath)
            m3u_synced = True
        except Exception:
            m3u_synced = False

    return {
        "message": f"Track verschoben ({direction})",
        "moved": True,
        "song_id": song_id,
        "new_position": a.position,  # a ist der verschobene Track, hat jetzt die neue Position
        "m3u_synced": m3u_synced,
    }


@app.delete("/api/playlists/{playlist_id}/tracks/{song_id}")
def remove_track_from_playlist(playlist_id: int, song_id: int, sync_m3u: bool = Query(True, description="M3U-Datei auf der NAS aktualisieren"), db: Session = Depends(get_db)):
    """Song von Playlist entfernen. Standardmäßig wird auch die M3U-Datei auf der NAS aktualisiert."""
    pt = db.query(PlaylistTrack)\
        .filter(PlaylistTrack.playlist_id == playlist_id, PlaylistTrack.song_id == song_id)\
        .first()
    if not pt:
        raise HTTPException(status_code=404, detail="Track nicht in Playlist")

    db.delete(pt)
    db.commit()

    # M3U-Datei aktualisieren (falls die Playlist eine hat)
    m3u_synced = False
    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if sync_m3u and pl and pl.m3u_filepath:
        remaining_tracks = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id)\
            .order_by(PlaylistTrack.position).all()
        tracks_with_songs = []
        for rt in remaining_tracks:
            song = db.query(Song).filter(Song.id == rt.song_id).first()
            if song:
                tracks_with_songs.append((rt, song))
        try:
            _export_m3u(pl, tracks_with_songs, pl.m3u_filepath)
            m3u_synced = True
        except Exception as e:
            m3u_synced = False

    result = {"message": "Track entfernt"}
    if pl and pl.m3u_filepath:
        result["m3u_synced"] = m3u_synced
        result["m3u_filepath"] = pl.m3u_filepath
    return result


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, delete_m3u: bool = Query(False, description="M3U-Datei auf der NAS mit löschen"), db: Session = Depends(get_db)):
    """Playlist löschen. Optional auch die zugehörige M3U-Datei auf der NAS löschen."""
    pl = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

    m3u_filepath = pl.m3u_filepath
    playlist_name = pl.name
    m3u_deleted = False

    # Tracks zuerst löschen
    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist_id).delete()
    db.delete(pl)
    db.commit()

    # M3U-Datei auf der NAS löschen (nur wenn explizit angefordert)
    if delete_m3u and m3u_filepath:
        m3u_deleted = _delete_m3u(m3u_filepath)

    result = {"message": f"Playlist '{playlist_name}' gelöscht"}
    if m3u_filepath:
        if delete_m3u:
            result["m3u_deleted"] = m3u_deleted
            result["m3u_filepath"] = m3u_filepath
        else:
            result["m3u_filepath"] = m3u_filepath
            result["m3u_note"] = "M3U-Datei auf der NAS wurde NICHT gelöscht"
    return result


@app.post("/api/playlists/create-from-filter")
def create_playlist_from_filter(
    name: str = Query(..., description="Playlist-Name"),
    q: str | None = None,
    artist: str | None = None,
    genre: str | None = None,
    year: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    db: Session = Depends(get_db),
):
    """Playlist aus Filter-Ergebnis erstellen"""
    # Existierende Playlist prüfen
    existing = db.query(Playlist).filter(Playlist.name == name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Playlist existiert bereits")

    # Songs filtern
    query = db.query(Song).filter(Song.artist != "")
    if q:
        search = f"%{q}%"
        query = query.filter((Song.artist.ilike(search)) | (Song.title.ilike(search)) | (Song.album.ilike(search)))
    if artist:
        query = query.filter(Song.artist.ilike(f"%{artist}%"))
    if genre:
        query = query.filter(Song.genre.ilike(f"%{genre}%"))
    if year:
        query = query.filter(Song.year == year)
    if bpm_min:
        query = query.filter(Song.bpm >= bpm_min)
    if bpm_max:
        query = query.filter(Song.bpm <= bpm_max)

    songs = query.order_by(func.random()).limit(100).all()
    if not songs:
        raise HTTPException(status_code=404, detail="Keine Songs gefunden für diesen Filter")

    # Playlist erstellen
    pl = Playlist(name=name, created_at=datetime.now().isoformat(), updated_at=datetime.now().isoformat())
    db.add(pl)
    db.flush()

    for i, song in enumerate(songs, 1):
        pt = PlaylistTrack(playlist_id=pl.id, song_id=song.id, position=i)
        db.add(pt)

    db.commit()
    db.refresh(pl)

    return {
        "message": f"Playlist '{name}' erstellt mit {len(songs)} Songs",
        "playlist_id": pl.id,
        "track_count": len(songs),
    }


# === Sonos-Steuerung ===
# === Sonos-Steuerung ===

SONOS_CLI = os.environ.get("SONOS_CLI", "/home/openclaw/go/bin/sonos")
NAS_MUSIC_PATH = os.environ.get("NAS_MUSIC_PATH", "/tmp/nas-musik")
NAS_SMB_HOST = os.environ.get("NAS_SMB_HOST", "192.168.0.7")
NAS_SMB_SHARE = os.environ.get("NAS_SMB_SHARE", "Musik")

# In-Memory: welcher Speaker spielt welche Playlist
_sonos_playing = {}  # {speaker_name: {"playlist_name": str|None, "song_id": int|None, "started_at": str}}

# Cache: {speaker_name: {"volume": int|None, "status": str, "_ts": float}}
_speaker_status_cache: dict = {}
_SPEAKER_STATUS_TTL = 5.0  # Sekunden

def _filepath_to_m3u_path(filepath, m3u_dir):
    """Konvertiere einen lokalen Dateipfad zurück in einen M3U-kompativen Eintrag.
    
    Versucht, den Original-Eintrag aus der M3U-Datei zu rekonstruieren.
    Wenn der Song-Pfad relativ zur M3U-Datei ist, wird ein relativer Pfad erstellt.
    Sonst wird ein x-file-cifs:// URI erstellt.
    """
    if not filepath or not m3u_dir:
        # Fallback: x-file-cifs URI
        if filepath:
            rel_path = filepath.replace(NAS_MUSIC_PATH, "")
            return f"x-file-cifs://{NAS_SMB_HOST}/{NAS_SMB_SHARE}{rel_path}"
        return None
    
    # Berechne den relativen Pfad vom M3U-Verzeichnis zur Song-Datei
    try:
        rel = os.path.relpath(filepath, m3u_dir)
        return rel
    except ValueError:
        # Verschiedene Laufwerke/OS — Fallback auf x-file-cifs
        rel_path = filepath.replace(NAS_MUSIC_PATH, "")
        return f"x-file-cifs://{NAS_SMB_HOST}/{NAS_SMB_SHARE}{rel_path}"


def _export_m3u(playlist, tracks_with_songs, m3u_filepath):
    """Schreibe eine M3U-Datei auf die NAS.
    
    Verwendet Atomic-Write (tmp + rename), da direktes Überschreiben
    bestehender Dateien auf CIFS-Mounts fehlschlagen kann.
    """
    m3u_dir = os.path.dirname(m3u_filepath)
    lines = ["#EXTM3U"]
    
    for pt, song in tracks_with_songs:
        duration = int(song.duration_sec) if song.duration_sec else -1
        artist = song.artist or ""
        title = song.title or ""
        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        entry = _filepath_to_m3u_path(song.filepath, m3u_dir)
        lines.append(entry)
    
    content = "\n".join(lines) + "\n"
    
    # Atomic write: tmp-Datei schreiben, dann umbenennen
    tmp_path = m3u_filepath + ".tmp"
    os.makedirs(os.path.dirname(m3u_filepath), exist_ok=True)
    with open(tmp_path, 'w', encoding='utf-8-sig') as f:
        f.write(content)
    os.replace(tmp_path, m3u_filepath)
    
    return len(lines) - 1  # Anzahl Tracks (ohne Header)


def _delete_m3u(m3u_filepath):
    """Lösche eine M3U-Datei von der NAS."""
    if m3u_filepath and os.path.exists(m3u_filepath):
        os.remove(m3u_filepath)
        return True
    return False


def _enqueue_cifs(speaker_ip, file_uri, title=""):
    """Hänge eine x-file-cifs:// MP3 an die Sonos-Queue an.

    Die `sonos enqueue`-CLI akzeptiert nur Spotify-URIs ("currently only Spotify refs
    are supported"). Wir umgehen das, indem wir den UPnP-SOAP-Call AVTransport.AddURIToQueue
    direkt an die Speaker-IP schicken — das ist URI-agnostisch.

    Speaker-IP kommt aus dem Discovery-Cache. Wichtig: die URI muss URL-encodet sein
    (Sonos antwortet sonst mit UPnPError 402). Die DIDL-Lite-Metadaten können leer
    bleiben — Sonos füllt Anzeige aus dem x-file-cifs-Pfad selbst.
    """
    import requests as _requests
    from urllib.parse import quote as _quote

    encoded_uri = _quote(file_uri, safe=":/")
    # Metadaten leer lassen — Sonos füllt Anzeige aus URI selbst
    soap = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:AddURIToQueue xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        f'<EnqueuedURI>{encoded_uri}</EnqueuedURI>'
        '<EnqueuedURIMetaData></EnqueuedURIMetaData>'
        '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>'
        '<EnqueueAsNext>0</EnqueueAsNext>'
        '</u:AddURIToQueue></s:Body></s:Envelope>'
    )
    r = _requests.post(
        f"http://{speaker_ip}:1400/MediaRenderer/AVTransport/Control",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#AddURIToQueue"',
        },
        data=soap.encode("utf-8"),
        timeout=5,
    )
    if r.status_code != 200:
        raise RuntimeError(f"AddURIToQueue HTTP {r.status_code}: {r.text[:200]}")
    # UPnP-Faults kommen in 200ern mit <s:Fault> — kurz prüfen
    if "<s:Fault" in r.text or "Fault" in r.text[:300]:
        raise RuntimeError(f"AddURIToQueue UPnP fault: {r.text[:200]}")
    return True


def _playlist_library_uri(m3u_filepath):
    """Konvertiere lokalen M3U-Pfad in Sonos-Library-URI (S://server/share/file.m3u).

    Beispiel: /tmp/nas-musik/+11.m3u → S://192.168.0.7/musik/+11.m3u
    (Sonos-SMB-Share ist kleingeschrieben, Großschreibung wird akzeptiert aber
    der Library-Index nutzt kleingeschriebene Namen.)
    """
    if not m3u_filepath:
        return None
    # /tmp/nas-musik/... → S://192.168.0.7/musik/...
    rel = m3u_filepath.replace(NAS_MUSIC_PATH, "").lstrip("/")
    return f"S://{NAS_SMB_HOST}/{NAS_SMB_SHARE.lower()}/{rel}"


# Tatsächlich gefundene Speaker (werden bei Start geladen)
_discovered_speakers = {}


def _discover_sonos_speakers():
    """Speaker frisch discoveren. Schreibt direkt in _discovered_speakers."""
    global _discovered_speakers
    _discovered_speakers = {}  # Cache leeren vor Re-Discover
    try:
        result = subprocess.run(
            [SONOS_CLI, "discover", "--format", "json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            import json as _json
            speakers = _json.loads(result.stdout)
            if isinstance(speakers, list):
                _discovered_speakers = {s["name"]: s for s in speakers}
            elif isinstance(speakers, dict):
                _discovered_speakers = {speakers.get("name", "unknown"): speakers}
    except Exception:
        pass
    # Fallback: bekannte Speaker
    if not _discovered_speakers:
        _discovered_speakers = {
            "Küche": {"name": "Küche", "ip": "192.168.0.38"},
            "Bad": {"name": "Bad", "ip": "192.168.0.34"},
            "Balkon": {"name": "Balkon", "ip": "192.168.0.33"},
            "Laufband": {"name": "Laufband", "ip": "192.168.0.37"},
        }
    return _discovered_speakers


def _get_sonos_speakers():
    """Speaker dynamisch Discoveren (mit Cache, der bei App-Start frisch befüllt wird).

    Wird beim Startup einmal via _discover_sonos_speakers() gefüllt.
    Neue Speaker sind erst nach Service-Restart sichtbar.
    """
    if _discovered_speakers:
        return _discovered_speakers
    return _discover_sonos_speakers()


@app.post("/api/sonos/speakers/rediscover")
def rediscover_speakers():
    """Speaker-Liste neu einlesen (falls ein Speaker dazukommt, ohne Service-Restart)."""
    speakers = _discover_sonos_speakers()
    return {
        "message": f"{len(speakers)} Speaker gefunden",
        "speakers": [{"name": n, "ip": i.get("ip", "")} for n, i in speakers.items()]
    }


def _sonos_cmd(args, timeout=10):
    """sonos CLI ausführen"""
    import signal
    proc = subprocess.Popen(
        [SONOS_CLI] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Eigene process group: wenn wir SIGKILL an die Gruppe schicken,
        # sterben auch eventuelle Sub-Children der Go-Binary.
        preexec_fn=os.setsid,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=[SONOS_CLI] + args,
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        # Go-Binary ignoriert SIGTERM. Wir killen die ganze Process-Group
        # mit SIGKILL, damit der Subprocess garantiert weg ist.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        # communicate() mit kürzerem Timeout, um die restlichen Bytes zu lesen
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(
            args=[SONOS_CLI] + args,
            returncode=-1,
            stdout="",
            stderr="timeout",
        )


def _get_speaker_name(speaker):
    """Speaker-Name validieren"""
    speakers = _get_sonos_speakers()
    if speaker in speakers:
        return speaker
    # Case-insensitive Suche
    for name in speakers:
        if name.lower() == speaker.lower():
            return name
    raise HTTPException(status_code=400, detail=f"Unbekannter Lautsprecher: {speaker}")


@app.get("/api/sonos/speakers")
def list_sonos_speakers():
    """Verfügbare Sonos-Lautsprecher (dynamisch discovered).

    Status wird parallel (ThreadPool) abgefragt, damit das Modal in <2s statt 7-15s erscheint.
    Status-Ergebnisse werden 5s gecacht, damit wiederholte Aufrufe sofort gehen.
    """
    speakers = _get_sonos_speakers()
    visible = [
        (name, info) for name, info in speakers.items()
        if "fernsehraum" not in name.lower() and "tv" not in name.lower()
    ]

    now = time.time()
    cache = _speaker_status_cache
    ttl = _SPEAKER_STATUS_TTL

    def fetch(pair) -> dict:
        name, info = pair
        cached = cache.get(name)
        if cached and (now - cached["_ts"]) < ttl:
            entry = {k: v for k, v in cached.items() if k != "_ts"}
            entry["name"] = name
            entry["ip"] = info.get("ip", "")
            return entry
        volume = None
        status_text = "Verfügbar"
        state = ""
        playing = ""
        try:
            st = _sonos_cmd(["status", "--name", name, "--format", "json"], timeout=5)
            if st.returncode == 0 and st.stdout.strip():
                data = json.loads(st.stdout)
                volume = data.get("volume")
                transport = data.get("transport", {})
                state = transport.get("State", "")
                np = data.get("nowPlaying", {})
                playing = np.get("title", "") or np.get("uri", "")
                if state == "PLAYING":
                    status_text = f"Spielt: {playing[:40]}"
                elif state == "PAUSED_PLAYBACK":
                    status_text = "Pausiert"
                elif state == "STOPPED":
                    status_text = "Gestoppt"
                else:
                    status_text = state if state else "Verfügbar"
        except Exception:
            pass
        cache[name] = {"volume": volume, "status": status_text, "state": state, "_ts": now}
        return {"name": name, "ip": info.get("ip", ""), "volume": volume, "status": status_text}

    result: list = []
    with ThreadPoolExecutor(max_workers=max(2, len(visible))) as pool:
        for entry in pool.map(fetch, visible):
            result.append(entry)
    return result


def _group_speakers(coordinator: str, members: list[str]) -> str:
    """Gruppiert die angegebenen members zum coordinator (Party-Modus, gezielt)."""
    import os
    def log(msg):
        os.write(1, (msg + "\n").encode())
    if not members:
        return ""
    log(f"[group] start: coord={coordinator} members={members}")
    try:
        # Coordinator erst solo machen (falls er noch in einer Gruppe hängt)
        r0 = _sonos_cmd(["group", "solo", "--name", coordinator], timeout=10)
        log(f"[group] solo coord {coordinator}: rc={r0.returncode}")
        for member in members:
            if member == coordinator:
                continue
            # Member erst aus jeder bestehenden Gruppe lösen,
            # sonst schlägt 'join' leise fehl, wenn er in einer ANDEREN Gruppe hängt.
            r1 = _sonos_cmd(["group", "solo", "--name", member], timeout=10)
            log(f"[group] solo member {member}: rc={r1.returncode}")
            r2 = _sonos_cmd(["group", "join", "--name", member, "--to", coordinator], timeout=10)
            log(f"[group] join {member} -> {coordinator}: rc={r2.returncode}, stderr={r2.stderr.strip()[:100]}")
        return ""
    except Exception as e:
        log(f"[group] EXC: {e!r}")
        return f"Grouping-Fehler: {e}"


def _solo_speaker(speaker: str) -> str:
    """Löst den Speaker aus JEDER bestehenden Gruppe und macht ihn zum Solo-Coordinator.

    Hintergrund: Wenn der Speaker gerade Member einer Gruppe ist, würde ein
    play-uri auf seinen Namen trotzdem die ganze Gruppe beschallen — das ist
    z.B. der Grund, warum 'Spiel auf Laufband' plötzlich den Balkon mit
    beschallt, wenn Laufband+Balkon gekoppelt sind.

    Wird NUR bei Einzel-Speaker-Play aufgerufen; bei Party-Modus (mehrere
    Speaker) übernimmt _group_speakers die explizite Gruppierung.
    """
    import os
    def log(msg):
        os.write(1, (msg + "\n").encode())
    log(f"[solo] start: {speaker}")
    try:
        r = _sonos_cmd(["group", "solo", "--name", speaker], timeout=10)
        log(f"[solo] {speaker}: rc={r.returncode}, stderr={r.stderr.strip()[:100]}")
        if r.returncode != 0:
            return f"Solo-Trennung fehlgeschlagen für '{speaker}': {r.stderr.strip() or r.stdout.strip()}"
        return ""
    except Exception as e:
        log(f"[solo] EXC: {e!r}")
        return f"Solo-Fehler: {e}"


@app.post("/api/sonos/play")
def sonos_play(cmd: SonosCommand, db: Session = Depends(get_db)):
    """Song, Playlist oder Radio-URI auf Sonos abspielen — auf einem oder mehreren Speakern.

    Bei mehreren Speakern wird der erste als Coordinator gewählt, die anderen
    werden via 'sonos group party' synchron dazugruppiert (Party-Modus).
    """
    speakers = cmd.get_speakers()
    if not speakers:
        raise HTTPException(status_code=400, detail="Kein Speaker angegeben")
    # Alle Speaker-Namen validieren
    validated = [_get_speaker_name(s) for s in speakers]
    print(f"[play] speakers={validated}")
    if len(validated) > 1:
        print(f"[play] calling _group_speakers {validated[0]} + {validated[1:]}", flush=True)
        grouping_err = _group_speakers(validated[0], validated[1:])
        print(f"[play] _group_speakers returned: {grouping_err!r}", flush=True)
        if grouping_err:
            print(f"[play] GROUPING FEHLER: {grouping_err}")
        else:
            print(f"[play] grouping OK: {validated[0]} + {validated[1:]}")
    else:
        # Einzel-Speaker: vorher aus jeder bestehenden Gruppe lösen, damit
        # 'Spiel auf X' wirklich nur X beschallt (nicht eine Gruppe, in der
        # X gerade Member ist).
        print(f"[play] calling _solo_speaker {validated[0]}", flush=True)
        solo_err = _solo_speaker(validated[0])
        if solo_err:
            print(f"[play] SOLO FEHLER: {solo_err}")
        else:
            print(f"[play] solo OK: {validated[0]}")
    speaker = validated[0]
    result_detail = ""

    # Lautstärke vorher setzen (wenn angegeben) — auf allen Speakern
    if cmd.volume is not None:
        for spk in validated:
            _sonos_cmd(["volume", "set", "--name", spk, str(cmd.volume)])

    if cmd.song_id:
        # Einzelnen Song spielen. Wir nutzen SoCo + HTTP-URL (NICHT die HA-CLI play-uri) — sonst
        # kein Auto-Advance, wenn der User nachher weitere Lieder anhängt. Die HA-CLI startet
        # einen einzelnen Track ohne Queue-Konzept, der State geht auf STOPPED statt zum
        # nächsten Item zu springen.
        # Hintergrund: clear_queue + add_to_queue + play_from_queue baut eine echte Queue
        # auf, in die add_to_queue weitere Items korrekt einfügen kann.
        song = db.query(Song).filter(Song.id == cmd.song_id).first()
        if not song:
            raise HTTPException(status_code=404, detail="Song nicht gefunden")

        # Speaker-IP
        spk_info = _get_sonos_speakers().get(speaker, {})
        speaker_ip = spk_info.get("ip")
        if not speaker_ip:
            raise HTTPException(status_code=404, detail=f"Speaker-IP für '{speaker}' nicht gefunden")

        # HTTP-URL + DidlMusicTrack bauen (NUL-Bytes strippen, Sonderzeichen escapen)
        from soco.data_structures import DidlMusicTrack as _Didl, DidlResource as _Res
        http_url = _http_url_for_song(song)
        def _clean(s):
            return (s or "").replace("\x00", "").strip() or None
        title = _clean(f"{song.artist} - {song.title}") or song.filename
        resource = _Res(uri=http_url, protocol_info="http-get:*:audio/mpeg:*")
        item = _Didl(title=title, parent_id="R:0/0", item_id=f"R:0/0/{song.id}", resources=[resource])

        # SoCo: clear + add + play_from_queue(0)
        import soco as _soco
        device = _soco.SoCo(speaker_ip)
        device.clear_queue()
        device.add_to_queue(item)
        device.play_from_queue(0)

        result_detail = f"{song.artist} - {song.title}"

    elif cmd.playlist_name:
        # Playlist spielen: bevorzugt via SoCo + HTTP-URLs (automatischer Auto-Advance,
        # robust gegen HA-Cache-Bugs), Fallback HA nur wenn SoCo scheitert.
        pl = db.query(Playlist).filter(Playlist.name == cmd.playlist_name).first()
        if not pl:
            raise HTTPException(status_code=404, detail="Playlist nicht gefunden")

        track_rows = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == pl.id)\
            .order_by(PlaylistTrack.position).all()
        songs = []
        for t in track_rows:
            song = db.query(Song).filter(Song.id == t.song_id).first()
            if song:
                songs.append(song)
        if not songs:
            raise HTTPException(status_code=404, detail="Playlist ist leer")

        # Shuffle im Backend statt Sonos-intern (x-file-cifs URIs mischt Sonos
        # unzuverlässig). Hier mischen = deterministisch, funktioniert überall.
        if cmd.shuffle:
            import random
            random.shuffle(songs)

        # Primär: SoCo mit HTTP-URLs
        speakers = _get_sonos_speakers()
        speaker_ip = speakers.get(speaker, {}).get("ip")
        if speaker_ip:
            # SoCo-Pfad mit EINMAL-Retry: Wenn der erste Versuch scheitert
            # (z.B. Queue kaputt, Speaker reagiert kurz nicht), nochmal mit
            # clear_queue() davor. Erst wenn der Retry auch scheitert → 500.
            def _soco_play_attempt():
                import soco
                from soco.data_structures import DidlMusicTrack, DidlResource
                from urllib.parse import quote

                def _build_item(song):
                    rel = song.filepath.replace(NAS_MUSIC_PATH, "").lstrip("/")
                    # safe='/' damit Verzeichnistrenner bleibt; alles andere (eckige Klammern, Leerzeichen, etc.) escapen.
                    # Sonst scheitert add_to_queue() mit "Internal Server Error" bei Files mit Sonderzeichen.
                    http_url = f"http://{_local_ip()}:{NAS_HTTP_PORT}/{quote(rel, safe='/')}"
                    resource = DidlResource(uri=http_url, protocol_info="http-get:*:audio/mpeg:*")
                    # NUL-Bytes (\x00) aus artist/title strippen — sonst Internal Server Error.
                    def _clean(s):
                        return (s or "").replace("\x00", "").strip() or None
                    title = _clean(f"{song.artist} - {song.title}") or song.filename
                    return DidlMusicTrack(
                        title=title,
                        parent_id="R:0/0",
                        item_id=f"R:0/0/{song.id}",
                        resources=[resource],
                    ), http_url

                device = soco.SoCo(speaker_ip)
                device.clear_queue()
                first_item, _ = _build_item(songs[0])
                device.add_to_queue(first_item)
                device.play_from_queue(0)
                for song in songs[1:]:
                    item, _ = _build_item(song)
                    device.add_to_queue(item)
                if cmd.shuffle:
                    device.shuffle = True
                else:
                    device.shuffle = False
                return device

            soco_err = None
            device = None
            for attempt in (1, 2):
                try:
                    device = _soco_play_attempt()
                    soco_err = None
                    break
                except Exception as e:
                    soco_err = e
                    print(f"[play] SoCo attempt {attempt} failed: {e!r}")

            if device is not None:
                _sonos_playing[speaker] = {
                    "playlist_name": cmd.playlist_name,
                    "song_id": songs[0].id,
                    "started_at": datetime.now().isoformat(),
                }
                return {
                    "status": "playing",
                    "speaker": speaker,
                    "detail": f"Playlist '{cmd.playlist_name}' ({len(songs)} Songs, SoCo)",
                    "method": "soco-http",
                }
            result_detail = f"SoCo-Pfad fehlgeschlagen nach 2 Versuchen: {soco_err}"
        else:
            result_detail = f"Speaker-IP für '{speaker}' nicht gefunden"

        raise HTTPException(status_code=500, detail=result_detail)

    elif cmd.uri:
        # Direkte URI spielen (Radio etc.)
        radio_flag = ["--radio"] if cmd.uri.startswith("x-rincon-mp3radio://") or cmd.uri.startswith("http") else []
        r = _sonos_cmd(["play-uri", "--name", speaker] + radio_flag + [cmd.uri])
        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Sonos Fehler: {r.stderr.strip() or r.stdout.strip()}")
        result_detail = cmd.uri

    else:
        raise HTTPException(status_code=400, detail="Weder song_id, playlist_name, noch uri angegeben")

    # Merke was gerade läuft
    _sonos_playing[speaker] = {
        "playlist_name": cmd.playlist_name or None,
        "song_id": cmd.song_id or None,
        "started_at": datetime.now().isoformat(),
    }

    return {
        "status": "playing",
        "speaker": speaker,
        "speakers": validated,  # alle beteiligten Speaker (1 oder mehr)
        "detail": result_detail,
    }


# === Song zur laufenden Queue hinzufügen (via SoCo) ===
# Hintergrund: Sonos S2 kann x-file-cifs://-URIs nicht in der Queue navigieren
# (UPnP 711 bei next/seek). Lösung: Song via lokalem HTTP-Stream (Port 8898)
# als echten "Track" zur Queue hinzufügen — funktioniert mit next/seek.
class QueueAddRequest(BaseModel):
    song_id: int
    speaker: Optional[str] = None  # wenn None: laufender Speaker
    as_next: bool = True  # True = als nächstes, False = ans Ende


class PlayFromPlaylistRequest(BaseModel):
    speaker: str
    speakers: list[str] | None = None  # neu: mehrere Speaker (Party-Modus)
    playlist_name: str
    start_at_song_id: int
    shuffle: bool = False

    def get_speakers(self) -> list[str]:
        if self.speakers:
            return [s for s in self.speakers if s]
        return [self.speaker] if self.speaker else []


@app.post("/api/sonos/play-from-playlist")
def sonos_play_from_playlist(req: PlayFromPlaylistRequest, db: Session = Depends(get_db)):
    """Spielt eine Playlist auf einem oder mehreren Speakern, startet bei einem bestimmten Lied.

    Bei mehreren Speakern: der erste wird Coordinator, die anderen werden synchron
    dazugruppiert (Party-Modus).
    """
    import soco
    from soco.data_structures import DidlMusicTrack, DidlResource
    from urllib.parse import quote

    # 1) Speaker validieren + IP holen
    speakers = req.get_speakers()
    if not speakers:
        raise HTTPException(status_code=400, detail="Kein Speaker angegeben")
    validated = [_get_speaker_name(s) for s in speakers]
    if len(validated) > 1:
        grouping_err = _group_speakers(validated[0], validated[1:])
        if grouping_err:
            print(f"[play-from-playlist] {grouping_err}")
    else:
        # Einzel-Speaker: vorher aus jeder bestehenden Gruppe lösen, damit
        # wirklich nur dieser Speaker spielt (nicht die Gruppe, in der er
        # gerade Member ist, z.B. Laufband+Balkon).
        solo_err = _solo_speaker(validated[0])
        if solo_err:
            print(f"[play-from-playlist] {solo_err}")
    speaker = validated[0]
    speakers = _get_sonos_speakers()
    info = speakers.get(speaker, {})
    speaker_ip = info.get("ip")
    if not speaker_ip:
        raise HTTPException(status_code=404, detail=f"Speaker-IP für '{speaker}' nicht gefunden")

    # 2) Playlist + Tracks laden
    pl = db.query(Playlist).filter(Playlist.name == req.playlist_name).first()
    if not pl:
        raise HTTPException(status_code=404, detail=f"Playlist '{req.playlist_name}' nicht gefunden")
    track_rows = db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == pl.id)\
        .order_by(PlaylistTrack.position).all()
    if not track_rows:
        raise HTTPException(status_code=400, detail="Playlist ist leer")
    songs = [db.query(Song).filter(Song.id == t.song_id).first() for t in track_rows]
    songs = [s for s in songs if s]  # kaputte Referenzen raus
    if not songs:
        raise HTTPException(status_code=400, detail="Keine abspielbaren Tracks in der Playlist")

    # 3) Start-Index finden
    try:
        start_idx = next(i for i, s in enumerate(songs) if s.id == req.start_at_song_id)
    except StopIteration:
        raise HTTPException(
            status_code=404,
            detail=f"Lied mit ID {req.start_at_song_id} ist nicht in Playlist '{req.playlist_name}'",
        )
    start_song = songs[start_idx]
    remaining = songs[start_idx + 1:]

    # 4) SoCo-Gerät + Queue leeren (frischer Start)
    device = soco.SoCo(speaker_ip)
    device.clear_queue()

    # 5) Erste Datei via HTTP-URL als Track bauen und direkt spielen
    def _build_item(song):
        http_url = _http_url_for_song(song)
        resource = DidlResource(uri=http_url, protocol_info="http-get:*:audio/mpeg:*")
        # NUL-Bytes (\x00) aus artist/title strippen — sonst Internal Server Error.
        def _clean(s):
            return (s or "").replace("\x00", "").strip() or None
        title = _clean(f"{song.artist} - {song.title}") or song.filename
        return DidlMusicTrack(
            title=title,
            parent_id="R:0/0",
            item_id=f"R:0/0/{song.id}",
            resources=[resource],
        ), http_url

    first_item, first_url = _build_item(start_song)
    # add_to_queue + play_from_queue ist robuster als play_uri für DidlMusicTrack.
    # clear_queue oben hat den Slot 0 freigemacht, also landet das Item auf Pos 0.
    device.add_to_queue(first_item)
    device.play_from_queue(0)

    # 6) Restliche Tracks ans Ende der Queue
    for song in remaining:
        item, _ = _build_item(song)
        device.add_to_queue(item)

    # 7) Optional: Shuffle. Sonos setzt die Reihenfolge der Queue dann selbst.
    if req.shuffle:
        device.shuffle = True
    else:
        device.shuffle = False

    # 8) Backend-Cache aktualisieren (damit der "+"-Button aus dem Such-Modal
    # weiß, in welche Playlist er das nächste Lied packen soll)
    _sonos_playing[speaker] = {
        "playlist_name": req.playlist_name,
        "song_id": start_song.id,
        "started_at": datetime.now().isoformat(),
    }

    return {
        "status": "playing",
        "speaker": speaker,
        "playlist_name": req.playlist_name,
        "started_with": f"{start_song.artist} - {start_song.title}",
        "start_position": start_idx + 1,  # 1-basiert für Anzeige
        "total_in_queue": 1 + len(remaining),
        "shuffle": req.shuffle,
        "method": "soco-http",
    }


@app.post("/api/sonos/queue/add")
def sonos_queue_add(req: QueueAddRequest, db: Session = Depends(get_db)):
    song = db.query(Song).filter(Song.id == req.song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Song nicht gefunden")

    # Speaker bestimmen: entweder angegeben oder "der, der grad spielt"
    speaker = _get_speaker_name(req.speaker) if req.speaker else None
    if not speaker:
        playing = _currently_playing_speaker()
        if not playing:
            raise HTTPException(
                status_code=400,
                detail="Kein Speaker spielt gerade. Starte erst eine Playlist oder gib einen Speaker an.",
            )
        speaker = playing

    # Speaker-IP aus Topology
    speakers = _get_sonos_speakers()
    info = speakers.get(speaker, {})
    ip = info.get("ip")
    if not ip:
        raise HTTPException(status_code=404, detail=f"Speaker-IP für '{speaker}' nicht gefunden")

    # URL bauen: lokaler HTTP-Stream auf Port 8898 (mit ?sid=… als ID-Marker)
    http_url = _http_url_for_song(song)

    # NUL-Bytes (\x00) aus Strings strippen — die DB hat bei manchen Songs (Migration-Bug)
    # NUL-Bytes am Anfang von artist/title. SoCo/DIDL-Parser mögen das nicht → 500.
    def _clean(s):
        return (s or "").replace("\x00", "").strip() or None
    title_clean = _clean(f"{song.artist} - {song.title}") or song.filename

    # Via SoCo zur Queue hinzufügen
    import soco
    from soco.data_structures import DidlMusicTrack, DidlResource
    device = soco.SoCo(ip)
    resource = DidlResource(uri=http_url, protocol_info="http-get:*:audio/mpeg:*")
    item = DidlMusicTrack(
        title=title_clean,
        parent_id="R:0/0",
        item_id=f"R:0/0/{song.id}",
        resources=[resource],
    )
    # SoCo: position=0 heißt lt. Doku "ans ENDE" — aber nur wenn queue_size > 0.
    # Bei queue_size=0 (CurrentTrack wird "direkt gespielt", nicht aus Queue) wird
    # mit position=0 das neue Item auf Pos 1 eingefügt und überschreibt den
    # CurrentTrack. Symptom: nach Ende des CurrentTrack kommt nichts mehr.
    # Fix: aktuelle Playlist-Position holen + 1 = Position direkt nach CurrentTrack.
    # Das ist semantisch "als nächstes" — was der User sowieso erwartet.
    current_pos = device.get_current_track_info().get("playlist_position") or 1
    try:
        current_pos = int(current_pos)
    except (TypeError, ValueError):
        current_pos = 1
    insert_pos = max(2, current_pos + 1)
    pos = device.add_to_queue(item, position=insert_pos, as_next=False)
    return {
        "status": "queued",
        "speaker": speaker,
        "song": title_clean,
        "queue_position": pos,
        "as_next": req.as_next,
        "uri": http_url,
    }


def _currently_playing_speaker() -> str | None:
    """Gibt den Speaker zurück, der grad was spielt (laut _sonos_playing-Cache)."""
    for name, info in _sonos_playing.items():
        if info.get("playlist_name") or info.get("song_id"):
            return name
    return None


@app.get("/api/sonos/now-playing-active")
def sonos_now_playing_active():
    """Welcher Speaker spielt grad? Für das Add-to-Queue-Modal im Frontend.

    Gibt {speaker, playlist_name, song_id, queue_remaining} oder {} wenn nichts spielt.
    Schneller als /api/sonos/now-playing — geht direkt in den Backend-Cache.
    queue_remaining = Anzahl der noch kommenden Songs in der Sonos-Queue (nach dem aktuellen).
    """
    sp = _currently_playing_speaker()
    if not sp:
        return {"speaker": None}
    info = _sonos_playing.get(sp, {})
    # Queue-Länge live von Sonos holen (kostet 1x SOAP-Call pro Polling-Cycle, gecached)
    queue_remaining = 0
    try:
        speakers = _get_sonos_speakers()
        spk_info = speakers.get(sp, {})
        ip = spk_info.get("ip")
        if ip:
            import soco
            device = soco.SoCo(ip)
            # queue_size liefert die GESAMTE Queue inkl. aktuellem Track
            total = device.queue_size
            # Aktuelle Position: CurrentTrackURI-Nummer, 1-basiert
            current_track = device.get_current_track_info().get("playlist_position")
            if current_track and total and total > 0:
                queue_remaining = max(0, int(total) - int(current_track))
    except Exception:
        # Sonos-Subprocess kann flaky sein — 0 ist OK (lieber kein Hinweis als 500)
        pass
    return {
        "speaker": sp,
        "playlist_name": info.get("playlist_name"),
        "song_id": info.get("song_id"),
        "queue_remaining": queue_remaining,
    }


@app.get("/api/sonos/queue-info")
def sonos_queue_info(speaker: str):
    """Queue-Länge live von Sonos holen — unabhängig vom Backend-Cache.

    Wird vom Queue-Hint in der Now-Playing-Bar genutzt, weil der
    _sonos_playing-Cache manchmal leer ist, obwohl grad ein Song spielt
    (z.B. direkt nach Next, oder wenn die Now-Playing-Bar den Speaker
    kennt, der Cache aber noch nicht aktualisiert wurde).

    Liefert {speaker, queue_remaining, total, current_track}.
    queue_remaining = Anzahl der noch kommenden Songs (ohne aktuellen).
    """
    sp = _get_speaker_name(speaker)
    queue_remaining = 0
    total = 0
    current_track = 0
    try:
        speakers = _get_sonos_speakers()
        spk_info = speakers.get(sp, {})
        ip = spk_info.get("ip")
        if ip:
            import soco
            device = soco.SoCo(ip)
            total = device.queue_size
            current_track = device.get_current_track_info().get("playlist_position") or 0
            if total and current_track:
                queue_remaining = max(0, int(total) - int(current_track))
    except Exception:
        pass
    return {
        "speaker": sp,
        "queue_remaining": queue_remaining,
        "total": total,
        "current_track": current_track,
    }


def _local_ip() -> str:
    """Eigene LAN-IP herausfinden (für HTTP-URLs an Sonos)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _http_url_for_song(song) -> str:
    """Baut die HTTP-Stream-URL für einen Song.

    Wir betten die DB-Song-ID als Query-String in die URL ein, damit der
    now-playing-Endpoint den gespielten Song zu 100% identifizieren kann
    — auch wenn artist/title kaputt sind (NUL-Bytes, Truncation).

    Format: http://IP:PORT/radio/.../song.mp3?sid=12345
                                    \_______________/  \____/
                                       Webserver-Pfad   ID-Marker

    Sonos schickt die URI vollständig zurück. Wir parsen sid=NNNN, holen
    die DB-Zeile direkt — keine Heuristik, keine ilike %x%.

    Der Webserver (SimpleHTTP) ignoriert Query-Strings beim File-Mapping,
    d.h. die Datei wird ganz normal aus /tmp/nas-musik/... gestreamt.
    """
    from urllib.parse import quote
    rel = song.filepath.replace(NAS_MUSIC_PATH, "").lstrip("/")
    quoted = quote(rel, safe="/")
    return f"http://{_local_ip()}:{NAS_HTTP_PORT}/{quoted}?sid={song.id}"


def _extract_song_id_from_uri(uri: str):
    """Parst die sid=NNNN aus einer Sonos-Track-URI.

    Gibt die song_id (int) zurück, oder None wenn keine sid da ist
    (z.B. bei x-file-cifs:// Streams oder Radio-URLs, die nicht von uns kommen).
    """
    if not uri or "sid=" not in uri:
        return None
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(uri).query)
        sid_list = qs.get("sid", [])
        if sid_list:
            return int(sid_list[0])
    except (ValueError, TypeError):
        return None
    return None


@app.post("/api/sonos/pause")
def sonos_pause(cmd: SonosCommand):
    """Wiedergabe toggeln: PLAYING -> pause, sonst -> play (resume).

    State kommt aus dem 5s-Cache (instant), Toggle-Command ist 1 Subprocess-Call
    (~1,8s). Bei Cache-Miss (Speaker noch nie gepollt) wird frisch abgefragt.
    Bei STOPPED + play ist 'play' no-op — das nächste Polling korrigiert den State.
    """
    speaker = _get_speaker_name(cmd.speaker)

    state = _get_speaker_transport_state(speaker)
    if state == "PLAYING":
        r = _sonos_cmd(["pause", "--name", speaker])
        new_state = "PAUSED_PLAYBACK"
        action = "paused"
    else:
        r = _sonos_cmd(["play", "--name", speaker])
        new_state = "PLAYING"
        action = "resumed"

    return {"status": action, "speaker": speaker, "state": new_state}


def _get_speaker_transport_state(speaker: str) -> str:
    """Aktuellen Transport-State (PLAYING / PAUSED_PLAYBACK / STOPPED / ...) eines Speakers holen.

    Nutzt den 5s-Cache aus list_sonos_speakers() (instant), fällt sonst auf eine
    frische sonos status-Abfrage zurück.
    """
    cached = _speaker_status_cache.get(speaker)
    if cached and "state" in cached and (time.time() - cached["_ts"]) < _SPEAKER_STATUS_TTL:
        return cached.get("state", "")
    try:
        st = _sonos_cmd(["status", "--name", speaker, "--format", "json"], timeout=5)
        if st.returncode == 0 and st.stdout.strip():
            data = json.loads(st.stdout)
            return data.get("transport", {}).get("State", "")
    except Exception:
        pass
    return ""


@app.post("/api/sonos/stop")
def sonos_stop(cmd: SonosCommand):
    """Wiedergabe stoppen"""
    speaker = _get_speaker_name(cmd.speaker)
    r = _sonos_cmd(["stop", "--name", speaker])
    # Playing-Info aufräumen
    _sonos_playing.pop(speaker, None)
    return {"status": "stopped", "speaker": speaker}


@app.post("/api/sonos/next")
def sonos_next(cmd: SonosCommand):
    """Zum nächsten Track in der Queue springen.

    Nutzt `sonos next` CLI (UPnP-AVTransport.Next). Funktioniert nur, wenn
    eine Queue mit >=2 Einträgen existiert (Playlist/Radio/Manuelle Queue).
    Bei Radio-Streams oder leerer Queue ist der Aufruf ein No-Op — wir
    returnen trotzdem success, damit das UI nicht meckert.
    """
    speaker = _get_speaker_name(cmd.speaker)
    r = _sonos_cmd(["next", "--name", speaker])
    if r.returncode != 0:
        # Kein Fehler werfen — der User hat wahrscheinlich auf "Next" gedrückt
        # obwohl keine Queue da ist. Stilles Toast im Frontend.
        return {"status": "no_queue", "speaker": speaker}
    return {"status": "skipped", "speaker": speaker}


# === Seek (zu einer bestimmten Position im Track springen) ===
# UPnP-AVTransport.Seek mit Unit="REL_TIME" und Target="HH:MM:SS".
# Die `sonos`-CLI unterstützt kein seek — wir machen den UPnP-Call selbst,
# analog zu _enqueue_cifs.

class SeekRequest(BaseModel):
    speaker: str
    position_sec: float  # Position in Sekunden; wird zu HH:MM:SS formatiert


def _sec_to_hms(sec: float) -> str:
    """Sekunden (float) → 'H:MM:SS' Format, das Sonos erwartet."""
    if sec < 0:
        sec = 0
    total = int(sec)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _sonos_seek(speaker_ip: str, target_hms: str) -> None:
    """UPnP-AVTransport.Seek an einen Speaker senden."""
    import requests as _requests
    soap = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        f'<Unit>REL_TIME</Unit>'
        f'<Target>{target_hms}</Target>'
        '</u:Seek></s:Body></s:Envelope>'
    )
    r = _requests.post(
        f"http://{speaker_ip}:1400/MediaRenderer/AVTransport/Control",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#Seek"',
        },
        data=soap.encode("utf-8"),
        timeout=5,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Seek HTTP {r.status_code}: {r.text[:200]}")
    if "<s:Fault" in r.text or "Fault" in r.text[:300]:
        raise RuntimeError(f"Seek UPnP fault: {r.text[:200]}")


@app.post("/api/sonos/seek")
def sonos_seek_endpoint(req: SeekRequest):
    """Springe zu einer bestimmten Position (in Sekunden) im aktuellen Track."""
    speaker = _get_speaker_name(req.speaker)
    speakers = _get_sonos_speakers()
    ip = speakers.get(speaker, {}).get("ip")
    if not ip:
        raise HTTPException(status_code=404, detail=f"Speaker-IP für '{speaker}' nicht gefunden")
    if req.position_sec < 0:
        raise HTTPException(status_code=400, detail="position_sec muss >= 0 sein")
    target_hms = _sec_to_hms(req.position_sec)
    try:
        _sonos_seek(ip, target_hms)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Seek fehlgeschlagen: {e}")
    return {"status": "seeked", "speaker": speaker, "position_sec": req.position_sec, "target": target_hms}


@app.get("/api/sonos/now-playing")
def sonos_now_playing(db: Session = Depends(get_db)):
    """Aktuelle Wiedergabe auf allen Speakern (für Now-Playing-Bar)"""
    import time as _t
    _t0 = _t.time()
    print(f"[now-playing] ENTER", flush=True)
    speakers = _get_sonos_speakers()
    print(f"[now-playing] speakers={list(speakers.keys())} (took {_t.time()-_t0:.2f}s)", flush=True)
    result = []
    for name, info in speakers.items():
        if "fernsehraum" in name.lower() or "tv" in name.lower():
            continue
        _t1 = _t.time()
        print(f"[now-playing] start {name}", flush=True)
        try:
            st = _sonos_cmd(["status", "--name", name, "--format", "json"])
            if st.returncode != 0 or not st.stdout.strip():
                continue
            import json as _json
            data = _json.loads(st.stdout)
            transport = data.get("transport", {})
            state = transport.get("State", "")
            if state not in ("PLAYING", "PAUSED_PLAYBACK"):
                # STOPPED / OFFLINE / ... → Info aufräumen falls vorhanden
                if name in _sonos_playing and state == "STOPPED":
                    _sonos_playing.pop(name, None)
                continue

            np = data.get("nowPlaying", {})
            uri = np.get("uri", "")
            title = np.get("title", "")
            volume = data.get("volume")

            # Cache mit State füllen, damit /api/sonos/pause schnell toggeln kann
            _speaker_status_cache[name] = {
                "volume": volume,
                "status": state,
                "state": state,
                "_ts": time.time(),
            }

            # Versuche Song aus DB anhand des URIs zu finden.
            # Drei Wege, in der Reihenfolge ihrer Zuverlässigkeit:
            # 1. HTTP-URL mit ?sid=NNNN (von uns gebaut) → direkter Lookup
            # 2. x-file-cifs:// (alter SMB-Weg) → Filepath-Lookup
            # 3. Keine Zuordnung möglich → song_info bleibt None (UI zeigt Filename)
            song_info = None
            song = None
            sid = _extract_song_id_from_uri(uri)
            if sid is not None:
                song = db.query(Song).filter(Song.id == sid).first()
            if song is None and uri and uri.startswith("x-file-cifs://"):
                rel_path = uri.replace(f"x-file-cifs://{NAS_SMB_HOST}/{NAS_SMB_SHARE}", "")
                from urllib.parse import unquote as _unquote
                local_path = NAS_MUSIC_PATH + _unquote(rel_path)
                song = db.query(Song).filter(Song.filepath == local_path).first()
            # Fallback 3: HTTP-URL mit Filename am Ende → in DB nach filename suchen
            # (Sonos strippt manchmal ?sid= aus der URI; mit Filename-Match treffen
            # wir den Song auch dann, wenn er gerade aktiv spielt)
            if song is None and uri and uri.startswith("http"):
                from urllib.parse import unquote as _unquote2
                fname = _unquote2(uri.rsplit("/", 1)[-1].split("?")[0])
                if fname:
                    # Exakter Match auf filename zuerst
                    song = db.query(Song).filter(Song.filename == fname).first()
                    if song is None:
                        # Substring-Match: manchmal hat die Sonos-URI Pfad-Präfixe
                        # wie "/sylvia/..." die in DB nicht stehen. Suche nach Dateinamen-Ende.
                        song = db.query(Song).filter(
                            Song.filename == fname.rsplit("/", 1)[-1]
                        ).first()
            if song is not None:
                # NUL-Bytes defensiv rauswerfen (UI kann sie nicht anzeigen)
                def _clean_db(s, fallback=""):
                    return (s or "").replace("\x00", "").strip() or fallback
                song_info = {
                    "id": song.id,
                    "artist": _clean_db(song.artist, "—"),
                    "title": _clean_db(song.title, song.filename or "—"),
                    "album": _clean_db(song.album, ""),
                    "score": song.score if song.score is not None else 50.0,
                }

            # Playlist-Name aus Memory
            playlist_name = _sonos_playing.get(name, {}).get("playlist_name")

            # Position-Info
            position = data.get("position", {})
            track_duration = position.get("TrackDuration", "")
            rel_time = position.get("RelTime", "")

            result.append({
                "speaker": name,
                "state": state,
                "volume": volume,
                "title": title,
                "uri": uri,
                "song": song_info,
                "playlist_name": playlist_name,
                "track_duration": track_duration,
                "rel_time": rel_time,
            })
        except Exception as e:
            import traceback
            print(f"[now-playing] {name}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            continue
        finally:
            print(f"[now-playing] end {name} (took {_t.time()-_t1:.2f}s)", flush=True)
    print(f"[now-playing] EXIT (total {_t.time()-_t0:.2f}s, {len(result)} speakers)", flush=True)
    return result


@app.get("/api/sonos/state")
def sonos_state(speaker: str = Query(..., description="Speaker-Name")):
    """Aktuellen Transport-State eines Speakers abfragen (PLAYING / PAUSED_PLAYBACK / STOPPED).

    Wird vom Frontend gebraucht, um den Pause-Button zwischen '⏸ Pause' und '▶ Weiter' umzuschalten.
    """
    speaker = _get_speaker_name(speaker)
    return {"speaker": speaker, "state": _get_speaker_transport_state(speaker)}


@app.post("/api/sonos/volume")
def sonos_volume(speaker: str, volume: int):
    """Lautstärke setzen (0-100)"""
    speaker = _get_speaker_name(speaker)
    if not 0 <= volume <= 100:
        raise HTTPException(status_code=400, detail="Lautstärke muss 0-100 sein")
    _sonos_cmd(["volume", "set", "--name", speaker, str(volume)])
    return {"speaker": speaker, "volume": volume}


@app.get("/api/sonos/status/{speaker}")
def sonos_status(speaker: str):
    """Sonos-Status abfragen"""
    speaker = _get_speaker_name(speaker)
    r = _sonos_cmd(["status", "--name", speaker, "--format", "json"])
    if r.returncode != 0:
        return {"speaker": speaker, "status": "offline"}
    try:
        import json as _json
        data = _json.loads(r.stdout)
        transport = data.get("transport", {})
        np = data.get("nowPlaying", {})
        return {
            "speaker": speaker,
            "state": transport.get("State", "UNKNOWN"),
            "volume": data.get("volume"),
            "title": np.get("title", ""),
            "uri": np.get("uri", ""),
            "ip": data.get("device", {}).get("ip", ""),
        }
    except Exception:
        return {"speaker": speaker, "status": r.stdout.strip()[:200]}


# === M3U Playlist Import ===

from urllib.parse import unquote

NAS_MUSIC_PATH = "/tmp/nas-musik"  # already defined above, but just for clarity

def _m3u_uri_to_filepath(uri):
    """Konvertiere x-file-cifs:// URI oder lokalen Pfad aus M3U zu lokalem Dateipfad."""
    if uri.startswith("x-file-cifs://"):
        # x-file-cifs://192.168.0.7/Musik/Pfad/Datei.mp3 → /tmp/nas-musik/Pfad/Datei.mp3
        # oder x-file-cifs://MUSIK/Pfad/Datei.mp3 → /tmp/nas-musik/Pfad/Datei.mp3
        without_scheme = uri[len("x-file-cifs://"):]
        # Entferne Host+Share: 192.168.0.7/Musik/ oder MUSIK/
        # Finde den ersten / nach dem Host
        parts = without_scheme.split("/", 2)
        if len(parts) >= 3:
            rel_path = parts[2]  # Alles nach Host/Share/
        elif len(parts) == 2:
            rel_path = parts[1]  # Nur Share/Pfad
        else:
            rel_path = parts[0]
        rel_path = unquote(rel_path)
        return os.path.join(NAS_MUSIC_PATH, rel_path)
    elif uri.startswith("/"):
        return uri
    else:
        # Relativer Pfad
        return uri


@app.get("/api/import/m3u")
def import_m3u_playlists():
    """Alle M3U-Dateien von der NAS importieren"""
    nas_path = NAS_MUSIC_PATH
    imported = []
    skipped = []

    for root, dirs, files in os.walk(nas_path):
        for f in sorted(files):
            if not f.lower().endswith('.m3u'):
                continue
            filepath = os.path.join(root, f)
            playlist_name = os.path.splitext(f)[0]

            # M3U lesen
            try:
                song_paths = []
                with open(filepath, 'r', encoding='utf-8-sig', errors='ignore') as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        local_path = _m3u_uri_to_filepath(line)
                        song_paths.append(local_path)
            except Exception as e:
                skipped.append({"name": playlist_name, "error": str(e)})
                continue

            if not song_paths:
                continue

            # Playlist in DB anlegen oder aktualisieren
            db = SessionLocal()
            try:
                existing = db.query(Playlist).filter(Playlist.name == playlist_name).first()
                if existing:
                    # Bestehende Tracks löschen und neu anlegen
                    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == existing.id).delete()
                    pl = existing
                    # M3U-Dateipfad aktualisieren
                    pl.m3u_filepath = filepath
                    pl.updated_at = datetime.now().isoformat()
                else:
                    pl = Playlist(
                        name=playlist_name,
                        m3u_filepath=filepath,
                        created_at=datetime.now().isoformat(),
                        updated_at=datetime.now().isoformat()
                    )
                    db.add(pl)
                    db.flush()

                matched = 0
                unmatched = 0
                pos = 1
                for sp in song_paths:
                    # Song in DB suchen (nach filepath)
                    song = db.query(Song).filter(Song.filepath == sp).first()
                    if not song:
                        # Versuche Dateinamen-Match
                        basename = os.path.basename(sp)
                        song = db.query(Song).filter(Song.filename == basename).first()
                    if song:
                        pt = PlaylistTrack(playlist_id=pl.id, song_id=song.id, position=pos)
                        db.add(pt)
                        matched += 1
                        pos += 1
                    else:
                        unmatched += 1

                db.commit()
                imported.append({
                    "name": playlist_name,
                    "tracks": matched,
                    "unmatched": unmatched,
                    "total_in_file": len(song_paths)
                })
            except Exception as e:
                db.rollback()
                skipped.append({"name": playlist_name, "error": str(e)})
            finally:
                db.close()

    return {
        "imported": imported,
        "skipped": skipped,
        "total_files": len(imported) + len(skipped),
    }


# === Background-Jobs: Scan + Enrichment ===
from .scanner import run_scan as _run_scan
from .enrich import run_enrich_genres as _run_enrich_genres
from .enrich import run_enrich_album_year as _run_enrich_album_year
from .database import DB_PATH
from .jobs import get_job_manager

# NAS-Mount-Pfad für den Scanner (sollte mit /tmp/nas-musik gemounted sein)
SCAN_MUSIC_DIR = NAS_MUSIC_PATH


class ScanRequest(BaseModel):
    force_rescan: bool = False


class EnrichRequest(BaseModel):
    """Welche Enrichment-Phasen laufen sollen.

    mode = "genres"       -- nur Genres pro Artist (schneller, ~1h bei 2k Artists)
    mode = "album_year"   -- nur Album + Year pro Song (~3h bei 10k Songs)
    mode = "all"          -- erst genres, dann album_year (default)
    """
    mode: str = "all"


@app.post("/api/scan/start")
def start_scan(req: ScanRequest = ScanRequest()):
    """Startet den NAS-Scanner als Background-Job.

    Liefert 409 wenn bereits ein Job läuft.
    """
    manager = get_job_manager()
    try:
        job = manager.start(
            name="scan",
            fn=_run_scan,
            music_dir=SCAN_MUSIC_DIR,
            db_path=str(DB_PATH),
            force_rescan=req.force_rescan,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "started", "job": job.to_dict()}


@app.post("/api/enrich/start")
def start_enrich(req: EnrichRequest = EnrichRequest()):
    """Startet MusicBrainz-Enrichment als Background-Job.

    "genres"     – 1 req/Artist,  ~1h bei 2000 Artists
    "album_year" – 1 req/Song,    ~3h bei 10000 Songs
    "all"        – erst genres, dann album_year (default)
    """
    if req.mode not in ("genres", "album_year", "all"):
        raise HTTPException(
            status_code=400,
            detail=f"Ungültiger mode '{req.mode}'. Erlaubt: genres, album_year, all",
        )

    manager = get_job_manager()
    db_path = str(DB_PATH)

    if req.mode == "genres":
        fn = _run_enrich_genres
        kwargs = {"db_path": db_path}
        name = "enrich_genres"
    elif req.mode == "album_year":
        fn = _run_enrich_album_year
        kwargs = {"db_path": db_path}
        name = "enrich_album_year"
    else:
        # "all" – wir wrappen in eine Job-Funktion, die nacheinander läuft
        def _run_all(_db_path, on_progress=None, should_stop=None):
            log_lines = []
            def _progress(cur, total, msg):
                log_lines.append(f"[genres] {cur}/{total} {msg}")
                if on_progress:
                    on_progress(cur, total, f"[genres] {msg}")
            r1 = _run_enrich_genres(db_path=_db_path, on_progress=_progress,
                                     should_stop=should_stop)
            if should_stop and should_stop():
                return {"phase": "genres", "stopped": True, **r1}
            if on_progress:
                on_progress(0, 0, f"genres fertig ({r1.get('updated', 0)} updates). Starte album_year...")
            def _progress2(cur, total, msg):
                if on_progress:
                    on_progress(cur, total, f"[album_year] {msg}")
            r2 = _run_enrich_album_year(db_path=_db_path, on_progress=_progress2,
                                         should_stop=should_stop)
            return {"phase": "all", "genres": r1, "album_year": r2}
        fn = _run_all
        kwargs = {"_db_path": db_path}
        name = "enrich_all"

    try:
        job = manager.start(name=name, fn=fn, **kwargs)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "started", "job": job.to_dict()}


@app.post("/api/jobs/stop")
def stop_job():
    """Bricht den aktuell laufenden Job ab (sofort, aber graceful)."""
    manager = get_job_manager()
    if manager.stop():
        return {"status": "stop_requested"}
    raise HTTPException(status_code=404, detail="Kein laufender Job")


@app.get("/api/jobs/status")
def job_status():
    """Status des aktuellen Jobs (oder None) + Liste der letzten Jobs."""
    manager = get_job_manager()
    current = manager.current()
    return {
        "current": current.to_dict() if current else None,
        "history": [j.to_dict() for j in manager.history()],
    }


# === Statische Dateien (Frontend) ===
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pathlib

STATIC_DIR = pathlib.Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_frontend():
    """Frontend ausliefern (ohne Browser-Cache, damit Fixes sofort wirken)"""
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                 "Pragma": "no-cache",
                 "Expires": "0"},
    )


# === Gesundheits-Check ===
@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}