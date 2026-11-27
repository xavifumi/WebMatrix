#!/usr/bin/env bash
# Simula dos encoders: MAIN por SRT y BACKUP por RTMP. Requiere ffmpeg, curl y python3.
# El canal debe existir (créalo en el panel con «Nuevo canal»).
# Uso: ./test-sources.sh [host] [canal]
HOST=${1:-localhost}; CH=${2:-evento}
STATE=$(curl -fsS "http://$HOST:8080/api/state") || { echo "No puedo leer el panel en http://$HOST:8080"; exit 1; }
key() { echo "$STATE" | python3 -c "import sys,json;d=json.load(sys.stdin);print([c for c in d['channels'] if c['name']=='$CH'][0]['keys']['$1'])" 2>/dev/null; }
KM=$(key main); KB=$(key backup)
[ -n "$KM" ] || { echo "El canal '$CH' no existe: créalo en el panel."; exit 1; }
MAIN_PID=""; BACK_PID=""
start_main() {
  ffmpeg -loglevel error -re -f lavfi -i "testsrc2=size=1280x720:rate=25" -f lavfi -i "sine=frequency=440:sample_rate=48000" \
    -c:v libx264 -preset veryfast -b:v 2500k -g 50 -pix_fmt yuv420p -c:a aac -b:a 128k -f mpegts \
    "srt://$HOST:8890?streamid=publish:$CH/main:verneta:$KM&pkt_size=1316" & MAIN_PID=$!
}
start_back() {
  ffmpeg -loglevel error -re -f lavfi -i "testsrc=size=1280x720:rate=25" -f lavfi -i "sine=frequency=880:sample_rate=48000" \
    -c:v libx264 -preset veryfast -b:v 1500k -g 50 -pix_fmt yuv420p -c:a aac -b:a 96k -f flv \
    "rtmp://$HOST:1935/$CH/backup?user=verneta&pass=$KB" & BACK_PID=$!
}
trap 'kill $MAIN_PID $BACK_PID 2>/dev/null; exit' INT TERM EXIT
start_main; start_back
echo "Canal '$CH' emitiendo (main SRT + backup RTMP)."
while read -r -p "[m] cortar/reanudar main · [b] cortar/reanudar backup · [q] salir > " k; do
  case $k in
    m) if kill -0 $MAIN_PID 2>/dev/null; then kill $MAIN_PID; echo "main cortado"; else start_main; echo "main reanudado"; fi;;
    b) if kill -0 $BACK_PID 2>/dev/null; then kill $BACK_PID; echo "backup cortado"; else start_back; echo "backup reanudado"; fi;;
    q) break;;
  esac
done
