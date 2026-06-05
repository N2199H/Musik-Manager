# 🎵 Musik-Manager

Self-hosted Web-App zur Verwaltung und Wiedergabe einer NAS-Musiksammlung auf Sonos-Lautsprechern.

Mit dem Musik-Manager kannst du:
- deine NAS-Musikbibliothek scannen und in einer SQLite-Datenbank indizieren
- Metadaten automatisch von [MusicBrainz](https://musicbrainz.org) anreichern (Genre, Album, Erscheinungsjahr)
- Playlists erstellen, sortieren und auf einem oder mehreren Sonos-Speakern abspielen
- Radio-Streams (TuneIn) und lokale MP3-Dateien mischen

Optimiert für die Steuerung per Handy im Heimnetz.

## Screenshot / Features

- **Mobile-First Web-UI** (keine App-Installation nötig)
- **Multi-Speaker-Grouping** (Party-Modus): wähle mehrere Speaker aus, ein Lied läuft synchron
- **Now-Playing-Bar** mit Live-Position, Pause/Resume
- **Playlist-Editor** mit Drag-Logik und Live-Sync zum NAS (M3U-Export)
- **Background-Jobs** für Scan und MusicBrainz-Enrichment (über Swagger-UI oder HTTP-API)

## Architektur

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────┐
│  Browser (Phone)│───▶│  FastAPI Backend │───▶│  Sonos LAN   │
│  index.html     │    │  app/main.py     │    │  (UPnP)      │
└─────────────────┘    └────────┬─────────┘    └──────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
        ┌──────────────┐ ┌──────────┐  ┌──────────────┐
        │  SQLite      │ │  NAS     │  │  Home-       │
        │  musik.db    │ │  (SMB)   │  │  Assistant   │
        └──────────────┘ └──────────┘  └──────────────┘
                ▲
                │
        ┌───────┴────────┐
        │  Background    │
        │  Jobs (Scan,   │
        │  Enrichment)   │
        └────────────────┘
```

**Backend:** FastAPI + SQLAlchemy + SoCo (Sonos-Library) + Mutagen (ID3-Tags)
**Datenbank:** SQLite (`musik.db`) – Pfad und Schema siehe `app/database.py`
**NAS:** SMB-Mount nach `/tmp/nas-musik` (siehe Setup)
**Sonos:** Steuerung via [SoCo](https://github.com/SoCo/SoCo) (UPnP) und der externen [`sonos` CLI](https://github.com/iancleary/sonos) für Group-Operationen

## Setup

### Voraussetzungen

- Linux (getestet auf Debian 13)
- Python 3.11+
- Sonos-Lautsprecher im selben Netzwerk
- NAS mit SMB-Share (oder lokale Musikordner)
- Optional: HomeAssistant (für Library-Playlist-Wiedergabe)

### 1. Repository klonen

```bash
git clone https://github.com/N2199H/Musik-Manager.git
cd Musik-Manager
```

### 2. Python-Umgebung

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. NAS mounten

Die App erwartet die Musiksammlung unter `/tmp/nas-musik` (konfigurierbar via `NAS_MUSIC_PATH`).

Beispiel mit `mount.cifs`:

```bash
sudo mkdir -p /tmp/nas-musik
sudo mount -t cifs //NAS_IP/Musik /tmp/nas-musik \
  -o credentials=/etc/nas-credentials,uid=$(id -u),gid=$(id -g),vers=2.0
```

**Wichtig:** SMB `vers=2.0` ist bei vielen NAS-Modellen nötig (vers=3.0 wirft Permission Denied).

### 4. Konfiguration

Kopiere `.env.example` zu `.env` und passe die Werte an:

```bash
cp .env.example .env
# edit .env
```

Oder setze die Umgebungsvariablen direkt. Ohne `.env` werden die Defaults in `app/main.py` und `app/ha_config.py` verwendet.

### 5. Sonos-CLI installieren

Die App nutzt die [iancleary/sonos](https://github.com/iancleary/sonos) CLI für Group-Operationen:

```bash
go install github.com/iancleary/sonos@latest
# Binary liegt dann unter ~/go/bin/sonos
```

Falls das Binary woanders liegt, setze `SONOS_CLI` in der `.env`.

### 6. Starten

```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8900
```

Die App ist erreichbar unter `http://localhost:8900/` und (im LAN) unter `http://<host-ip>:8900/`.

### 7. Systemd-Service (optional)

Eine `systemd --user` Service-Datei liegt nicht im Repo (sie ist host-spezifisch). Beispiel:

```ini
# ~/.config/systemd/user/musik-manager.service
[Unit]
Description=Musik-Manager FastAPI Backend
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/<user>/Musik-Manager
ExecStart=/home/<user>/Musik-Manager/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8900
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Erste Schritte

1. **Musik scannen:** Swagger-UI öffnen → `http://<host>:8900/docs` → `POST /api/scan/start` triggert einen Background-Scan
2. **MusicBrainz-Enrichment:** `POST /api/enrich/start` mit `{"mode": "all"}` – Genre + Album + Jahr werden nachgeschlagen (MusicBrainz-Rate-Limit: 1 req/s, das dauert bei 10k Songs ca. 3 Stunden)
3. **Im UI abspielen:** `http://<host>:8900/` im Browser → Song/Playlist suchen → Play-Button → Speaker auswählen → ▶ Abspielen

## API-Übersicht

| Endpoint | Methode | Zweck |
|---|---|---|
| `/api/songs` | GET | Songs suchen (`?q=`, `?artist=`, `?genre=`) |
| `/api/songs/{id}` | GET | Einzelnen Song abrufen |
| `/api/artists` | GET | Alle Künstler mit Song-Anzahl |
| `/api/genres` | GET | Alle Genres mit Song-Anzahl |
| `/api/albums` | GET | Alle Alben |
| `/api/playlists` | GET, POST | Playlists auflisten / anlegen |
| `/api/playlists/{id}` | PATCH, DELETE | Playlist umbenennen / löschen |
| `/api/playlists/{id}/tracks` | GET, POST, DELETE | Tracks verwalten |
| `/api/playlists/{id}/tracks/reorder` | POST | Track-Reihenfolge ändern |
| `/api/sonos/speakers` | GET | Sonos-Speaker + Status |
| `/api/sonos/speakers/rediscover` | POST | Discovery neu starten |
| `/api/sonos/play` | POST | Song/Playlist/Radio abspielen (einer oder mehrere Speaker) |
| `/api/sonos/pause` | POST | Pause/Resume Toggle |
| `/api/sonos/stop` | POST | Stop |
| `/api/sonos/now-playing` | GET | Now-Playing-Bar Daten (für Polling) |
| `/api/sonos/state` | GET | Transport-State eines Speakers |
| **`/api/scan/start`** | POST | NAS-Scan-Job starten |
| **`/api/enrich/start`** | POST | MusicBrainz-Enrichment-Job starten |
| **`/api/jobs/status`** | GET | Laufender Job + History |
| **`/api/jobs/stop`** | POST | Laufenden Job abbrechen |

Interaktive Doku: `http://<host>:8900/docs`

## Konfiguration via Umgebungsvariablen

| Variable | Default | Zweck |
|---|---|---|
| `HA_URL` | `http://homeassistant.local:8123` | HomeAssistant-URL (für Library-Playlists) |
| `HA_TOKEN` | (leer → fallback `~/.ha_token`) | HA Long-lived access token |
| `NAS_SMB_HOST` | `192.168.0.7` | IP/Hostname des NAS |
| `NAS_SMB_SHARE` | `Musik` | Name des SMB-Shares |
| `NAS_MUSIC_PATH` | `/tmp/nas-musik` | Lokaler Mount-Punkt des NAS |
| `SONOS_CLI` | `/home/openclaw/go/bin/sonos` | Pfad zum `sonos` CLI binary |

## Module-Übersicht

```
app/
├── main.py              # FastAPI-App + alle Endpoints (~1800 Zeilen)
├── database.py          # SQLAlchemy-Model (Song, Playlist, PlaylistTrack)
├── ha_config.py         # HomeAssistant-Config (URL, Token, Speaker-Mapping)
├── jobs.py              # Background-Job-Manager (ThreadPool + Progress)
├── scanner/             # NAS-Scanner
│   ├── nas.py           # ID3-Extract, DB-Schema, run_scan()
│   └── service.py       # Service-Layer (zukünftiges Caching, Locking)
├── enrich/              # MusicBrainz-Enrichment
│   ├── musicbrainz.py   # MB-API-Client, run_enrich_genres(), run_enrich_album_year()
│   └── service.py       # Service-Layer
└── static/
    └── index.html       # Frontend (Single-Page, mobile-first)
```

## Lizenz

Dieses Projekt steht unter der **GNU General Public License v3.0** – siehe [LICENSE](LICENSE) für Details.

Kurzfassung: Du darfst den Code verwenden, verändern und weitergeben, aber alle abgeleiteten Werke müssen ebenfalls unter GPL v3 stehen und der Quellcode muss offengelegt werden.
