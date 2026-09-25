#!/bin/bash
# KORKO — lance la démo complète (cloud + station A) en un double-clic.
# Mac sur le wifi de la station. Control+C pour tout arrêter.
cd "$(dirname "$0")"

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
[ -z "$IP" ] && IP=192.168.8.170
export KORKO_DEMO=1
export KORKO_ACCEL=${KORKO_ACCEL:-15}   # 1 vraie minute = 15 min de session (mettre 1 pour le temps réel)
export KORKO_ETAT=etat_pitch.json
export KORKO_URL=http://$IP:9000

# un ancien cloud qui tournerait encore sur le port 9000 : on l'arrête
OLD=$(lsof -ti tcp:9000 2>/dev/null); [ -n "$OLD" ] && kill $OLD 2>/dev/null && sleep 1

python3 mon_cloud.py &
CLOUD=$!
trap 'kill $CLOUD 2>/dev/null' EXIT
sleep 1

echo
echo "  Site client  : http://$IP:9000/p/K1"
echo "  Écran jury   : http://localhost:9000/live   (à projeter)"
echo "  Tableau bord : http://localhost:9000/admin"
if [ "$IP" != "192.168.8.170" ]; then
  echo
  echo "  ⚠  L'adresse du Mac a changé ($IP au lieu de 192.168.8.170) :"
  echo "     les QR imprimés ne marchent plus. Scanne plutôt http://$IP:9000/p/K1"
fi
echo

# la station : si le Raspberry Pi est injoignable, on réessaie sans couper le site
while true; do
  python3 ma_station.py --source 192.168.8.100:8420
  echo "  Station A injoignable (wifi de la station ? Pi allumé ?) — nouvel essai dans 5 s…"
  sleep 5
done
