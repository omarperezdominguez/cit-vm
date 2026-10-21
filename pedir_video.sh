#!/usr/bin/env bash
# =============================================================================
# pedir_video.sh -- manda un media.open a una unidad, como lo hara la API
#
#   este script --(usuario api, :1883 local)--> Mosquitto --> cit/v1/<unidad>/cmd --> Jetson
#   y espera el resultado en cit/v1/<unidad>/resultado
#
# Uso:
#   ./pedir_video.sh                 # cit-001, 60 s
#   ./pedir_video.sh cit-001 30      # otra duracion (<= 60)
#
# Gasta una sesion de la cuota mensual de la unidad. Pide sudo solo para leer
# la contrasena del usuario api.
# =============================================================================
set -euo pipefail

UNIDAD="${1:-cit-001}"
SEGUNDOS="${2:-60}"
CLAVE=$(sudo grep '^MQTT_API=' /etc/cit-vm/secretos.env | cut -d= -f2)
ID=$(openssl rand -hex 4)
AHORA=$(date -u +%Y-%m-%dT%H:%M:%SZ)

ORDEN=$(jq -cn --arg id "$ID" --arg u "$UNIDAD" --arg t "$AHORA" --argjson s "$SEGUNDOS" \
    --arg quien "${USER}@vm" \
    '{command_id:$id, device_id:$u, type:"media.open", issued_by:$quien,
      ts_issued:$t, ttl_s:30, reason:"prueba de visualizacion desde la VM",
      params:{camera:"cam0", duration_s:$s}}')

# Se escucha el resultado ANTES de mandar la orden, para no perderlo
mosquitto_sub -h 127.0.0.1 -u api -P "$CLAVE" -t "cit/v1/$UNIDAD/resultado" -q 1 \
    -W 15 -C 5 2>/dev/null > /tmp/resultado_$ID.txt &
ESCUCHA=$!
sleep 0.5
mosquitto_pub -h 127.0.0.1 -u api -P "$CLAVE" -t "cit/v1/$UNIDAD/cmd" -q 1 -m "$ORDEN"
echo "Orden $ID enviada: media.open $SEGUNDOS s a $UNIDAD"

# Espera hasta que aparezca un estado final de esta orden (o 15 s)
for _ in $(seq 30); do
    if grep "\"$ID\"" /tmp/resultado_$ID.txt 2>/dev/null | grep -qvE '"en_curso"'; then break; fi
    sleep 0.5
done
kill $ESCUCHA 2>/dev/null || true
grep "\"$ID\"" /tmp/resultado_$ID.txt 2>/dev/null | tail -1 | jq -c '{status, detail, data}' \
    || echo "Sin respuesta en 15 s: la unidad esta desconectada o no recibio la orden."
rm -f /tmp/resultado_$ID.txt
