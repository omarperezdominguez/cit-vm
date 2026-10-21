# Operación de la VM de Edge Mobility (ADR-001)

> Para integrar una plataforma web, ve a [INTEGRACION_WEB.md](INTEGRACION_WEB.md).
> Este documento es para quien opera o reinstala la VM.

Lado de la nube del ADR-001 v1.6. Contraparte de la Jetson descrita en
`AGENTS.md` y `PLAN_VM.md` (repo de la unidad). Aquí viven la **fase 2**
(broker MQTT con TLS y servidor de medios) y la **fase 3** (ingesta, base de
datos y API para la plataforma RuTi). El contrato de la API está en
**[API.md](API.md)**.

Última actualización: **28-sep-2026**. Entorno de **prueba**.

## La máquina

| | |
|---|---|
| GCP | proyecto `edge-desarrollo`, VM `nube-edge-2026`, `northamerica-south1-a`, e2-medium, 10 GB |
| SO | Debian 13 |
| IP | `34.51.107.234`, **estática** (reservada) |
| Firewall GCP | etiqueta `vm-edge`. Regla `permitir-trafico-edge`: tcp 22, 8883; udp 8890. Regla `cit-plataforma`: tcp 8443, 8889, 8189; udp 8189 |

`gcloud` **no funciona desde dentro de la VM** (la cuenta de servicio no
tiene permisos); se usa Cloud Shell.

## Qué corre

```
 Jetson ──MQTT+TLS :8883──► Mosquitto ──:1883 (local)──► cit-ingesta ──► SQLite /var/lib/cit-vm/cit.db
                               ▲                                               │
 RuTi ──HTTPS :8443 + token──► cit-api ◄───────────────────────────────────────┘
                               │  └─ publica órdenes en cit/v1/<id>/cmd
                               └─ emite tickets de video
 Jetson ──SRT :8890──► MediaMTX ──WebRTC :8889 (?ticket=)──► navegador de RuTi
                          │  └─ pregunta cada acceso a cit-api (127.0.0.1:8081)
                          └─ HLS 127.0.0.1:8888 ──► túnel SSH (pruebas)
```

| Servicio | Qué hace | Config |
|---|---|---|
| `mosquitto` | Broker. Persistencia (autoguardado 30 s), cola de 1000 por cliente, sesiones que expiran a los 14 días | `/etc/mosquitto/conf.d/cit.conf`, `passwd`, `acl` |
| `cit-ingesta` | Guarda telemetría, resultados y estado. Sesión persistente y confirmación **después** de guardar: no pierde datos si se reinicia. Dedup por `sample_id`. **Borra lo de más de 7 días** cada hora | `/opt/cit-vm/ingesta.py` |
| `cit-api` | API HTTPS para RuTi y autenticación de MediaMTX. Si se cae, MediaMTX niega todo (incluido publicar): `Restart=always` | `/opt/cit-vm/api.py`, `/etc/cit-vm/api.env` |
| `mediamtx` | Recibe SRT; entrega WebRTC con ticket y HLS en local. RTSP/RTMP/MoQ apagados | `/etc/mediamtx/mediamtx.yml` (original en `.original`) |

Usuarios MQTT (ACL):

| Usuario | Puede |
|---|---|
| `cit-NNN` | escribir `cit/v1/cit-NNN/{telemetria,resultado,estado}`, leer `cit/v1/cit-NNN/cmd`. Nada de otra unidad |
| `ingesta` | leer `cit/v1/+/{telemetria,resultado,estado}` |
| `api` | escribir `cit/v1/+/cmd`, leer `resultado` y `estado` |

**`cit-000` es una unidad ficticia** que solo usa `prueba_humo.py`, para que
las pruebas nunca toquen la sesión, el estado ni el video de una unidad
real. El script la da de alta siempre y borra de la BD lo que genera.

## Base de datos

SQLite en `/var/lib/cit-vm/cit.db` (modo WAL). Tablas: `telemetria` (una fila
por muestra + la muestra completa en JSON), `comandos` (cada orden y su último
estado), `resultados` (cada mensaje de resultado), `unidades` (último estado
de cada una). ≈ 1 KB por muestra: ~400 MB/semana con la unidad en marcha
16 h/día a 1 Hz; ~60 MB detenida.

**Retención: 7 días**, por hora de llegada (`CIT_RETENCION_DIAS` en el
servicio para cambiarla). El espacio liberado se reutiliza; el archivo no
encoge.

```bash
sudo sqlite3 /var/lib/cit-vm/cit.db "select count(*), min(ts_device), max(ts_device) from telemetria"
sudo sqlite3 -header -column /var/lib/cit-vm/cit.db "select ts_device, lat, lon, speed_kph, gnss_fix, pax_onboard from telemetria order by ts_device desc limit 10"
```

## Archivos

```
~/cit-vm/
├── instalar.sh              aplica TODO (fases 2 y 3). Idempotente
├── ingesta.py               -> /opt/cit-vm/
├── api.py                   -> /opt/cit-vm/
├── cit_vm/db.py             esquema y retencion (compartido)
├── prueba_humo.py           18 pruebas de broker y medios (usa cit-000)
├── pedir_video.sh           media.open a mano (sin API), para el tunel HLS
├── API.md                   contrato para RuTi
└── config/                  mosquitto-cit.conf, mediamtx.yml, *.service
/etc/cit-vm/                 SOLO ROOT
├── secretos.env             contrasenas MQTT, passphrases SRT, API_TOKEN
├── api.env                  IP publica para las ligas de video
└── ca/ca.key, ca.crt        CA propia. ca.key no sale de aqui
/etc/mosquitto/certs/        servidor.crt (SAN: IP publica, 127.0.0.1), servidor.key, ca.crt
/etc/ssl/cit-vm/             copia del cert para cit-api
/var/lib/cit-vm/cit.db       la base de datos
```

Los servicios corren desde `/opt/cit-vm`: **después de editar el código en
`~/cit-vm`, correr `sudo ./instalar.sh`** para copiarlo y reiniciar.

**No editar a mano** `passwd`, `acl` ni `mediamtx.yml`: se regeneran.

## Operación

```bash
sudo ./instalar.sh                                   # reaplicar todo
sudo UNIDADES="cit-001 cit-002" ./instalar.sh        # alta de unidades (lista completa)
sudo RENOVAR_CERT=1 ./instalar.sh                    # reemitir el cert (vence en 825 dias)
sudo python3 prueba_humo.py                          # prueba local (reinicia mosquitto a mitad)
sudo cat /etc/cit-vm/secretos.env                    # credenciales
systemctl status mosquitto cit-ingesta cit-api mediamtx
journalctl -u cit-ingesta -f                         # resumen cada minuto, retencion cada hora
journalctl -u cit-api -f
```

Probar la API desde la VM (por `127.0.0.1`; la IP pública no se alcanza a sí
misma en los puertos nuevos):

```bash
T=$(sudo grep ^API_TOKEN= /etc/cit-vm/secretos.env | cut -d= -f2)
curl -s --cacert /etc/mosquitto/certs/ca.crt -H "Authorization: Bearer $T" https://127.0.0.1:8443/v1/unidades | jq
```

## Ver el video

**Como lo verá RuTi** (WebRTC, < 1 s): `POST /v1/unidades/cit-001/video` y
abrir la liga `visor` de la respuesta (ver API.md).

**Por túnel SSH** (HLS, 2–5 s, sin API):

```bash
ssh -i C:\Users\damia\.ssh\gcp_vm -L 8888:127.0.0.1:8888 damia@34.51.107.234
~/cit-vm/pedir_video.sh               # dentro de esa sesion
# navegador:  http://localhost:8888/cit-001/
```

Cada sesión gasta una de la cuota mensual de la unidad (100, provisional).

## Qué hay que poner en la Jetson

`ca.crt` en `~/Desktop/cit/vm_ca/` (no en `certs/`, que es la reserva de AWS) y en `unidad.env`:

```
VM_HOST=34.51.107.234
VM_MQTT_PUERTO=8883
VM_MQTT_TLS=1
VM_MQTT_CA=/home/nelsonkanzela/Desktop/cit/vm_ca/ca.crt
VM_MQTT_USUARIO=cit-001
VM_MQTT_CONTRASENA=<MQTT_CIT_001 de secretos.env>
VM_SRT_PUERTO=8890
VM_SRT_PASSPHRASE=<SRT_CIT_001 de secretos.env>
VM_SRT_STREAMID=publish:cit-001
```

## Pendiente

- Credenciales definitivas (las actuales son de prueba y ya circularon).
- `module.shutdown` se añadió a la lista cerrada de `api.py` el 21-oct-2026,
  editando a mano `/opt/cit-vm/api.py` (respaldo en `api.py.antes`). Al
  reaplicar `instalar.sh` se copia este `api.py`, que ya lo incluye.
- Disco de 10 GB: suficiente para una unidad en prueba; ampliarlo para varias.
- Video por HTTPS (`webrtcEncryption`): hoy la página del visor es HTTP. Con
  un dominio y Let's Encrypt se resuelve para API, broker y video a la vez.
