"""Datenbank-Modell und Verbindung für den Musik-Manager"""

from sqlalchemy import create_engine, Column, Integer, Float, Text, String, Index, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "musik.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Song(Base):
    __tablename__ = "songs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filepath = Column(Text, unique=True, nullable=False)
    filename = Column(Text)
    artist = Column(Text)
    title = Column(Text)
    album = Column(Text)
    genre = Column(Text)
    year = Column(Text)
    duration_sec = Column(Float)
    bitrate_kbps = Column(Integer)
    filesize = Column(Integer)
    bpm = Column(Float)
    energy = Column(Float)
    valence = Column(Float)
    danceability = Column(Float)
    loudness = Column(Float)
    key = Column(Text)
    mood = Column(Text)
    tags = Column(Text)
    spotify_id = Column(Text)
    lastfm_tags = Column(Text)
    analyzed_at = Column(Text)
    created_at = Column(Text)
    score = Column(Float, default=50.0, nullable=False)  # Ranking-Score, EWMA-geglättet

    def to_dict(self):
        return {
            "id": self.id,
            "artist": self.artist or "",
            "title": self.title or "",
            "album": self.album or "",
            "genre": self.genre or "",
            "year": self.year or "",
            "duration_sec": self.duration_sec or 0,
            "bitrate_kbps": self.bitrate_kbps or 0,
            "filesize": self.filesize or 0,
            "bpm": self.bpm,
            "energy": self.energy,
            "valence": self.valence,
            "danceability": self.danceability,
            "mood": self.mood or "",
            "tags": self.tags or "",
            "filepath": self.filepath,
            "score": self.score if self.score is not None else 50.0,
        }


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    m3u_filepath = Column(Text, nullable=True)  # Pfad zur M3U-Datei auf der NAS (relativ oder absolut)
    created_at = Column(Text)
    updated_at = Column(Text)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "m3u_filepath": self.m3u_filepath or "",
            "created_at": self.created_at or "",
            "updated_at": self.updated_at or "",
        }


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    playlist_id = Column(Integer, nullable=False)
    song_id = Column(Integer, nullable=False)
    position = Column(Integer, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "playlist_id": self.playlist_id,
            "song_id": self.song_id,
            "position": self.position,
        }


def init_db():
    """Neue Tabellen erstellen (falls nicht vorhanden) und Migrationen durchführen"""
    Base.metadata.create_all(bind=engine, tables=[Playlist.__table__, PlaylistTrack.__table__])
    # Migration: m3u_filepath-Spalte hinzufügen (falls noch nicht vorhanden)
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE playlists ADD COLUMN m3u_filepath TEXT"))
            conn.commit()
        except Exception:
            pass  # Spalte existiert bereits
        # Migration: score-Spalte in songs (Ranking)
        try:
            conn.execute(text("ALTER TABLE songs ADD COLUMN score REAL DEFAULT 50.0 NOT NULL"))
            conn.commit()
        except Exception:
            pass  # Spalte existiert bereits