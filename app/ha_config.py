"""HomeAssistant-Konfiguration für Musik-Manager.

Wird vom Backend gelesen, um Sonos via HA Sonos-Integration zu steuern
(insbesondere für Library-Playlists — direkter sonos-CLI-PlayURI akzeptiert
keine S://-URIs, nur HA's play_media spricht die richtige UPnP-Sprache).

Konfiguration via Umgebungsvariablen:
  HA_URL    — z. B. http://homeassistant.local:8123
  HA_TOKEN  — Long-lived access token aus HA-Profil

Fallback: token aus /home/openclaw/.ha_token lesen (mode 0600).
"""
import os
import pathlib

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

if not HA_TOKEN:
    token_file = pathlib.Path("/home/openclaw/.ha_token")
    if token_file.exists():
        HA_TOKEN = token_file.read_text().strip()

# Speaker-Name → HA-Entity-ID Mapping
SPEAKER_TO_ENTITY = {
    "Küche": "media_player.kuche",
    "Bad": "media_player.bad",
    "Balkon": "media_player.balkon",
    "Flur": "media_player.flur",
    "Schlafzimmer": "media_player.schlafzimmer",
    "Wohnzimmer": "media_player.wohnzimmer",
    "Fernsehraum": "media_player.fernsehraum",
    "Laufband": "media_player.laufband",
}
