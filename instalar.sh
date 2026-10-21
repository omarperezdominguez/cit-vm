#!/usr/bin/env bash
# =============================================================================
# instalar.sh -- VM de Edge Mobility (ADR-001, PLAN_VM.md fases 2 y 3)
#
#   Jetson --MQTT+TLS :8883--> Mosquitto --:1883 (local)--> cit-ingesta --> SQLite
#                                  ^                                          |
#   RuTi --HTTPS :8443 + token--> cit-api -----------------------------------'
#   Jetson --SRT :8890--> MediaMTX --WebRTC :8889 (ticket de cit-api)--> navegador
#
# Que hace (idempotente: se puede correr varias veces sin romper nada):
#   1. /etc/cit-vm/ca/        CA propia (se crea una sola vez)
#   2. /etc/cit-vm/secretos.env  contrasenas MQTT y passphrase SRT (una sola vez)
#   3. /etc/mosquitto/certs/  certificado del servidor con la IP publica en el SAN
#   4. Mosquitto: conf.d/cit.conf, passwd y acl a partir de secretos.env
#   5. MediaMTX: /etc/mediamtx/mediamtx.yml y servicio systemd
#   6. Fase 3: usuario citvm, codigo en /opt/cit-vm, servicios cit-ingesta y
#      cit-api, BD en /var/lib/cit-vm/cit.db
#
# Uso:
#   sudo ./instalar.sh                 # IP publica por metadatos de GCP
#   sudo IP_PUBLICA=1.2.3.4 ./instalar.sh
#   sudo UNIDADES="cit-001 cit-002" ./instalar.sh   # alta de unidades
#   sudo RENOVAR_CERT=1 ./instalar.sh  # vuelve a emitir el cert del servidor
#
# Los secretos NUNCA se imprimen. Para leerlos: sudo cat /etc/cit-vm/secretos.env
# =============================================================================
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
DIR_CIT=/etc/cit-vm
DIR_CA=$DIR_CIT/ca
SECRETOS=$DIR_CIT/secretos.env
UNIDADES="${UNIDADES:-cit-001}"
# cit-000 = unidad ficticia de prueba_humo.py: siempre existe, para que las
# pruebas nunca toquen los temas, la sesion ni el video de una unidad real.
[[ " $UNIDADES " == *" cit-000 "* ]] || UNIDADES="cit-000 $UNIDADES"

if [[ $EUID -ne 0 ]]; then
    echo "Correr con sudo." >&2
    exit 1
fi

# --- IP publica -------------------------------------------------------------
# Va en el SAN del certificado: la Jetson conecta por IP y paho valida el nombre.
if [[ -z "${IP_PUBLICA:-}" ]]; then
    IP_PUBLICA=$(curl -s -m 3 -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" || true)
fi
if [[ ! "$IP_PUBLICA" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "No se pudo saber la IP publica; pasala con IP_PUBLICA=..." >&2
    exit 1
fi
echo "IP publica: $IP_PUBLICA   unidades: $UNIDADES"

install -d -m 700 -o root -g root "$DIR_CIT" "$DIR_CA"

# --- Secretos ---------------------------------------------------------------
# Se generan una sola vez; las altas nuevas solo anaden lineas. Cambiar una
# contrasena = borrar su linea y volver a correr el script.
aleatorio() { openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c "$1"; }

touch "$SECRETOS"; chmod 600 "$SECRETOS"
asegurar_secreto() {   # nombre longitud
    grep -q "^$1=" "$SECRETOS" || echo "$1=$(aleatorio "$2")" >> "$SECRETOS"
}
asegurar_secreto MQTT_INGESTA 32
asegurar_secreto MQTT_API 32
asegurar_secreto API_TOKEN 40
for u in $UNIDADES; do
    v=$(echo "$u" | tr 'a-z-' 'A-Z_')
    asegurar_secreto "MQTT_$v" 32
    # SRT exige passphrase de 10 a 79 caracteres
    asegurar_secreto "SRT_$v" 32
done
# shellcheck disable=SC1090
source "$SECRETOS"

# --- CA propia (una sola vez) ------------------------------------------------
# La llave de la CA no sale de /etc/cit-vm/ca. A la Jetson solo va ca.crt.
if [[ ! -f $DIR_CA/ca.key ]]; then
    echo "Creando la CA propia..."
    openssl ecparam -name prime256v1 -genkey -noout -out $DIR_CA/ca.key
    openssl req -x509 -new -key $DIR_CA/ca.key -sha256 -days 3650 \
        -subj "/O=CIT Mobility/CN=CIT Edge CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -out $DIR_CA/ca.crt
    chmod 600 $DIR_CA/ca.key
fi

# --- Certificado del servidor --------------------------------------------------
DIR_CERTS=/etc/mosquitto/certs
SAN="IP:$IP_PUBLICA,IP:127.0.0.1,DNS:localhost,DNS:$(hostname)"
emitir=0
[[ -f $DIR_CERTS/servidor.crt ]] || emitir=1
[[ "${RENOVAR_CERT:-0}" == "1" ]] && emitir=1
# Si la IP cambio, el cert viejo ya no sirve: se reemite solo
if [[ $emitir == 0 ]] && ! openssl x509 -in $DIR_CERTS/servidor.crt -noout -ext subjectAltName | grep -q "$IP_PUBLICA"; then
    echo "La IP publica no esta en el certificado actual: se reemite."
    emitir=1
fi
if [[ $emitir == 1 ]]; then
    echo "Emitiendo el certificado del servidor ($SAN)..."
    openssl ecparam -name prime256v1 -genkey -noout -out $DIR_CERTS/servidor.key
    openssl req -new -key $DIR_CERTS/servidor.key -subj "/O=CIT Mobility/CN=$IP_PUBLICA" \
        -out /tmp/servidor.csr
    printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n" \
        "$SAN" > /tmp/servidor.ext
    openssl x509 -req -in /tmp/servidor.csr -CA $DIR_CA/ca.crt -CAkey $DIR_CA/ca.key \
        -CAcreateserial -days 825 -sha256 -extfile /tmp/servidor.ext -out $DIR_CERTS/servidor.crt
    rm -f /tmp/servidor.csr /tmp/servidor.ext
fi
cp $DIR_CA/ca.crt $DIR_CERTS/ca.crt
chown root:mosquitto $DIR_CERTS/servidor.key $DIR_CERTS/servidor.crt $DIR_CERTS/ca.crt
chmod 640 $DIR_CERTS/servidor.key
chmod 644 $DIR_CERTS/servidor.crt $DIR_CERTS/ca.crt

# --- Mosquitto ---------------------------------------------------------------
install -m 644 "$AQUI/config/mosquitto-cit.conf" /etc/mosquitto/conf.d/cit.conf

# passwd: se rehace entero desde secretos.env (fuente de verdad)
PASSWD=/etc/mosquitto/passwd
# Nace con 600: mosquitto_passwd se queja (y versiones futuras rechazan) si es legible
( umask 077; : > $PASSWD.nuevo )
mosquitto_passwd -b $PASSWD.nuevo ingesta "$MQTT_INGESTA"
mosquitto_passwd -b $PASSWD.nuevo api "$MQTT_API"
for u in $UNIDADES; do
    v="MQTT_$(echo "$u" | tr 'a-z-' 'A-Z_')"
    mosquitto_passwd -b $PASSWD.nuevo "$u" "${!v}"
done
mv $PASSWD.nuevo $PASSWD
chown mosquitto:mosquitto $PASSWD
chmod 600 $PASSWD

# acl: bloque fijo de servicios + uno por unidad. Una unidad solo ve lo suyo.
ACL=/etc/mosquitto/acl
{
    echo "# Generado por instalar.sh -- no editar a mano"
    echo "user ingesta"
    echo "topic read cit/v1/+/telemetria"
    echo "topic read cit/v1/+/resultado"
    echo "topic read cit/v1/+/estado"
    echo
    echo "user api"
    echo "topic write cit/v1/+/cmd"
    echo "topic read cit/v1/+/resultado"
    echo "topic read cit/v1/+/estado"
    for u in $UNIDADES; do
        echo
        echo "user $u"
        echo "topic write cit/v1/$u/telemetria"
        echo "topic write cit/v1/$u/resultado"
        echo "topic write cit/v1/$u/estado"
        echo "topic read cit/v1/$u/cmd"
    done
} > $ACL.nuevo
mv $ACL.nuevo $ACL
chown mosquitto:mosquitto $ACL
chmod 600 $ACL

systemctl enable mosquitto >/dev/null
systemctl restart mosquitto

# --- MediaMTX ----------------------------------------------------------------
YML=/etc/mediamtx/mediamtx.yml
[[ -f $YML.original ]] || { [[ -f $YML ]] && cp $YML $YML.original; }
{
    sed "s/__IP_PUBLICA__/$IP_PUBLICA/" "$AQUI/config/mediamtx.yml"
    for u in $UNIDADES; do
        v="SRT_$(echo "$u" | tr 'a-z-' 'A-Z_')"
        echo "  $u:"
        echo "    srtPublishPassphrase: ${!v}"
    done
} > $YML.nuevo
mv $YML.nuevo $YML
chown root:mediamtx $YML
chmod 640 $YML

# --- Fase 3: ingesta y API --------------------------------------------------------
id citvm >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin citvm

# Codigo: se copia a /opt (los servicios no pueden leer /home)
install -d -m 755 /opt/cit-vm /opt/cit-vm/cit_vm
install -m 644 "$AQUI/ingesta.py" "$AQUI/api.py" /opt/cit-vm/
install -m 644 "$AQUI/cit_vm/__init__.py" "$AQUI/cit_vm/db.py" /opt/cit-vm/cit_vm/

# TLS de la API: el mismo certificado del broker, legible por citvm
install -d -m 750 -o root -g citvm /etc/ssl/cit-vm
install -m 644 -o root -g citvm $DIR_CERTS/servidor.crt /etc/ssl/cit-vm/servidor.crt
install -m 640 -o root -g citvm $DIR_CERTS/servidor.key /etc/ssl/cit-vm/servidor.key
echo "CIT_IP_PUBLICA=$IP_PUBLICA" > $DIR_CIT/api.env

install -m 644 "$AQUI/config/cit-ingesta.service" "$AQUI/config/cit-api.service" /etc/systemd/system/
install -m 644 "$AQUI/config/mediamtx.service" /etc/systemd/system/mediamtx.service
systemctl daemon-reload
systemctl enable cit-ingesta cit-api mediamtx >/dev/null 2>&1
systemctl restart cit-ingesta cit-api
sleep 1
systemctl restart mediamtx

sleep 2
echo
for s in mosquitto cit-ingesta cit-api mediamtx; do printf "%-12s %s\n" "$s" "$(systemctl is-active $s)"; done
ss -ltnu | awk 'NR==1 || /:8883|:1883|:8890|:9997|:8443|:8081|:8889|:8189|:8888/'
echo
echo "Listo. Secretos en $SECRETOS (sudo cat). CA para la Jetson y RuTi: $DIR_CERTS/ca.crt"
