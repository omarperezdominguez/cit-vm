# cit-vm — nube y API de Edge Mobility

La VM que recibe los datos de las unidades Edge (Jetson a bordo de los camiones)
y la **API** con la que una plataforma web consulta pasajeros y posición, abre
el video de la cámara y manda órdenes, incluido **apagar la unidad**.

El código de la Jetson está en
[omarperezdominguez/cit-edge](https://github.com/omarperezdominguez/cit-edge).

## ¿Qué quieres hacer?

| Si quieres… | Lee |
|---|---|
| **Conectar un sitio web o una app** | [INTEGRACION_WEB.md](INTEGRACION_WEB.md) y el panel de [`ejemplos/panel-node/`](ejemplos/panel-node/) |
| Probar la API con `curl` | [GUIA_API.md](GUIA_API.md) |
| Ver cada ruta y campo en detalle | [API.md](API.md) |
| Operar o reinstalar la VM | [OPERACION_VM.md](OPERACION_VM.md) |
| Darle contexto a un asistente de IA | [CLAUDE.md](CLAUDE.md) |

## En una imagen

```
 Jetson (camión) ──MQTT+TLS :8883──► Mosquitto ──► cit-ingesta ──► SQLite (7 días)
        ▲                                                              │
        └──── órdenes ◄── Mosquitto ◄── cit-api :8443 (HTTPS + token) ◄┘◄── tu servidor ◄── tu página
 Jetson ──video SRT :8890──► MediaMTX ──WebRTC :8889 (liga con ticket)──────────────────────► tu página
```

## Lo que necesitas para integrarte

- Dirección: `https://34.51.107.234:8443` (entorno de **prueba**).
- [`ca.crt`](ca.crt): certificado de la CA de Edge, en este repo.
- El **token**: lo da el equipo de Edge por un canal privado. **No está en
  el repo y no debe estarlo.**

## Archivos

```
├── INTEGRACION_WEB.md     guía para conectar una plataforma
├── GUIA_API.md            ejemplos con curl
├── API.md                 contrato v1 completo
├── OPERACION_VM.md        la máquina, servicios, instalación, pendientes
├── CLAUDE.md              contexto para asistentes de IA
├── ca.crt                 CA pública de Edge (no es secreto)
├── ejemplos/panel-node/   panel de ejemplo: servidor Node + página
├── api.py                 la API (corre en la VM como cit-api)
├── ingesta.py             guarda telemetría, resultados y estado (cit-ingesta)
├── cit_vm/db.py           esquema SQLite y retención
├── config/                mosquitto, mediamtx y servicios systemd
├── instalar.sh            instala o reaplica todo en la VM
├── prueba_humo.py         pruebas de broker y video en la VM
└── pedir_video.sh         pide video desde la propia VM
```

Los secretos (`/etc/cit-vm/secretos.env` en la VM: contraseñas MQTT,
passphrases SRT y token de la API) **nunca** van a este repo.
