#!/bin/bash
# CIFS-Mount-Tuning für Musik-Manager NAS
# Ziel: Reconnects reduzieren durch weniger aggressive Cache-Invalidation
# Vorher (aggressiv):  cache=strict,actimeo=1,closetimeo=1   → ~1780 reconnects
# Nachher (entspannt):  cache=loose,actimeo=60,closetimeo=60
#
# Was das macht:
# - cache=loose:     Client cached Metadaten großzügiger (statt jede Sekunde Server fragen)
# - actimeo=60:      60 Sekunden Cache-Timeout für attribute (vorher: 1 Sekunde)
# - closetimeo=60:   SMB-Handle 60s offen halten statt nach 1s schließen
#
# Hinweis: closetimeo=60000 wurde getestet, der QNAP-Server mag den Wert
# nicht (mount schlaegt fehl mit "read-only"). 60s ist der sichere Wert.
#
# Risiko: Falls du auf der NAS was umbenennst, sieht der Client das bis zu
# 60s verzögert. Für unseren Use-Case (Music Manager liest/schreibt Files)
# ist das OK weil wir proaktiv die richtigen Pfade kennen.
#
# Anwendung:
#   bash scripts/cifs-tune.sh apply    # Remount mit neuen Optionen (Server vorher stoppen!)
#   bash scripts/cifs-tune.sh status   # Aktuelle Mount-Optionen + Reconnect-Stats
#   bash scripts/cifs-tune.sh revert   # Zurück zu alten (aggressiven) Optionen
#
# Voraussetzungen: Root oder sudo (mount/umount brauchen das).
# Credentials: /etc/nas-credentials (mode 0600, NAS-User hermes).

set -e

NAS_REMOTE="//192.168.0.7/Musik"
MOUNT_POINT="/tmp/nas-musik"
CRED_FILE="/etc/nas-credentials"

NEW_OPTS="rw,vers=3.1.1,sec=ntlmssp,user=hermes,uid=1000,forceuid,gid=1000,forcegid,iocharset=utf8,soft,nounix,mapposix,file_mode=0755,dir_mode=0755,cache=loose,actimeo=60,closetimeo=60,echo_interval=60"
OLD_OPTS="rw,vers=3.1.1,sec=ntlmssp,user=hermes,uid=1000,forceuid,gid=1000,forcegid,iocharset=utf8,soft,nounix,mapposix,file_mode=0755,dir_mode=0755,cache=strict,actimeo=1,closetimeo=1,echo_interval=60"

cmd_status() {
    echo "=== Mount-Status ==="
    findmnt "$MOUNT_POINT" 2>/dev/null || echo "  nicht gemounted"
    echo ""
    echo "=== CIFS-Stats (/proc/fs/cifs/Stats) ==="
    grep -E "Session|Share|reconnects|Maximum" /proc/fs/cifs/Stats 2>/dev/null \
        || echo "  Stats nicht lesbar (kein /proc/fs/cifs/Stats?)"
}

cmd_apply() {
    if [ ! -f "$CRED_FILE" ]; then
        echo "ERROR: $CRED_FILE fehlt. Bitte NAS-credentials dort ablegen:"
        echo "  sudo tee $CRED_FILE <<<EOF"
        echo "  username=hermes"
        echo "  password=<NAS-PASSWORD>"
        echo "  EOF"
        echo "  sudo chmod 600 $CRED_FILE"
        exit 1
    fi
    if ! mountpoint -q "$MOUNT_POINT"; then
        echo "ERROR: $MOUNT_POINT ist nicht gemounted. Mount zuerst manuell."
        exit 1
    fi
    echo "WICHTIG: Server (uvicorn) muss vorher gestoppt sein, sonst hält er"
    echo "offene File-Handles die umount blockieren."
    echo ""
    read -p "Server ist gestoppt, fortfahren? [j/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Jj]$ ]]; then
        echo "Abgebrochen."
        exit 1
    fi
    echo ""
    echo "1) umount $MOUNT_POINT ..."
    umount "$MOUNT_POINT"
    echo "2) mount mit neuen Optionen ..."
    mount -t cifs "$NAS_REMOTE" "$MOUNT_POINT" -o "credentials=$CRED_FILE,$NEW_OPTS"
    echo "3) verify:"
    cmd_status
    echo ""
    echo "✅ Mount erfolgreich. Server kann jetzt wieder gestartet werden."
    echo "   Reconnect-Stats checken: bash scripts/cifs-tune.sh status"
}

cmd_revert() {
    if ! mountpoint -q "$MOUNT_POINT"; then
        echo "ERROR: $MOUNT_POINT ist nicht gemounted."
        exit 1
    fi
    echo "Server muss vorher gestoppt sein."
    read -p "Server ist gestoppt, fortfahren? [j/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Jj]$ ]]; then
        echo "Abgebrochen."
        exit 1
    fi
    umount "$MOUNT_POINT"
    mount -t cifs "$NAS_REMOTE" "$MOUNT_POINT" -o "credentials=$CRED_FILE,$OLD_OPTS"
    cmd_status
    echo ""
    echo "✅ Zurück zu alten (aggressiven) Optionen."
}

case "${1:-status}" in
    apply)  cmd_apply ;;
    revert) cmd_revert ;;
    status) cmd_status ;;
    *) echo "Usage: $0 {apply|revert|status}"; exit 1 ;;
esac
