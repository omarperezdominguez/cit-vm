# API de Edge Mobility — contrato v1 para RuTi

API HTTPS de la VM de Edge (ADR-001, fase 3). Con ella la plataforma consulta
posición, velocidad y conteo de cada unidad, manda las órdenes de la lista
cerrada y abre sesiones de video.

Última actualización: **21-oct-2026** (se añade `module.shutdown`). Entorno de **prueba**.

## Conexión

| | |
|---|---|
| Base | `https://34.51.107.234:8443` |
| TLS | Certificado de la **CA propia de Edge** (`ca.crt`, se entrega aparte). Sin ella el cliente rechaza el certificado. **No desactivar la verificación** |
| Autenticación | Cabecera `Authorization: Bearer <API_TOKEN>` en todo menos `/v1/salud` |
| Formato | JSON UTF-8. Todas las horas en **UTC**, ISO 8601 con `Z`. La conversión a hora local es de la capa de presentación |

```bash
export API=https://34.51.107.234:8443  TOKEN=<API_TOKEN>
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" $API/v1/unidades
```

Errores: `{"error": "..."}` con `400` (petición mal formada), `401` (token),
`404`, `409` (la unidad rechazó la orden), `503` (broker caído), `504`.

## Rutas

### `GET /v1/salud`
Sin token. `{"ok": true, "mqtt": true, "hora_utc": "..."}`.

### `GET /v1/unidades`
Estado actual de todas las unidades.

```json
{"unidades": [{
  "device_id": "cit-001",
  "conexion": "en linea",
  "visto_hace_s": 3.2,
  "ultima_muestra": { ...muestra completa de la tabla 3 del ADR... }
}]}
```

- `conexion`: `en linea` / `desconectada` (la unidad perdió la conexión o se apagó) / `desconocida`.
- `visto_hace_s`: segundos desde la última muestra recibida. Detenida, la
  unidad manda una cada 10 s; en marcha, una por segundo.
- `ultima_muestra`: la más reciente **por hora de la unidad**, no la última en
  llegar (tras un corte llegan muestras atrasadas con `buffered: true`).

### `GET /v1/unidades/<id>`
Lo mismo para una unidad. `404` si nunca ha mandado datos.

### `GET /v1/unidades/<id>/telemetria`

| Parámetro | Default | |
|---|---|---|
| `desde`, `hasta` | — | ISO UTC, por `ts_device`. Con `desde` se devuelven desde ahí hacia adelante |
| `limite` | 500 | máx. 10 000 |
| `completo` | 0 | `1` devuelve cada muestra completa (salud, sesión de video, último comando) |

Sin rango: las últimas `limite`. Siempre en orden cronológico.

```json
{"device_id": "cit-001", "n": 2, "muestras": [
 {"ts_device": "2026-09-28T04:43:00.488Z", "lat": 19.0286719, "lon": -98.2480798,
  "speed_kph": 0.0, "heading_deg": null, "gnss_fix": "gps",
  "pax_in": 0, "pax_out": 0, "pax_onboard": 0, "buffered": false}]}
```

Reglas de interpretación (ADR-001):
- `speed_kph` es la del receptor GNSS: **no recalcular** a partir de posiciones.
- `lat`/`lon`/`speed_kph` son `null` sin fix (`gnss_fix: "none"`). No arrastrar la última posición.
- `pax_in`/`pax_out` acumulan **desde que arrancó el módulo**. Un salto hacia
  abajo con `health.uptime_s` pequeño es un servicio nuevo, no gente que bajó.
- **Se conservan 7 días.** Lo anterior se borra solo.

### `POST /v1/unidades/<id>/comandos`
Emite una orden. Cuerpo:

| Campo | Obligatorio | |
|---|---|---|
| `type` | sí | `media.open`, `media.close`, `app.restart`, `module.restart`, `module.shutdown`, `diagnostics.request`. Cualquier otro → `400` |
| `issued_by` | sí | Quién la pide (usuario de la plataforma). Queda en la bitácora |
| `reason` | sí | Por qué. Queda en la bitácora |
| `ttl_s` | no | Vigencia (5–3600). Default 30 para video, 120 reinicios y apagado, 60 diagnóstico |
| `params` | no | Solo `media.open`: `{"camera":"cam0","duration_s":60}`. Mejor usar `/video` |

`?esperar=N` (0–30 s) espera el resultado. Respuesta `200` si ya es final,
`202` si sigue en curso; en ambos casos el comando:

```json
{"command_id": "4f6a789db204", "device_id": "cit-001", "type": "diagnostics.request",
 "issued_by": "ana@flota", "ts_issued": "2026-09-28T04:43:11Z", "ttl_s": 60,
 "reason": "revisar unidad", "status": "aplicada", "detail": null,
 "ts_applied": "2026-09-28T04:43:11.374Z", "params": null, "data": { ... }}
```

| `status` | |
|---|---|
| `pendiente` | Emitida, sin respuesta de la unidad. Si además `unidad_sin_conexion: true`, la unidad está desconectada: la orden le llega al volver si su `ttl_s` no ha vencido |
| `en_curso` | La unidad la recibió y la está ejecutando (los reinicios se quedan aquí hasta que el equipo vuelve) |
| `aplicada` | Hecha |
| `rechazada` | La unidad no la ejecuta; el motivo en `detail` (límite de 3 reinicios/hora, sesión de video en curso, cuota agotada…) |
| `caducada` | Llegó después de `ts_issued + ttl_s` |
| `fallida` | Se intentó y falló; motivo en `detail` |

Cada orden se ejecuta **una sola vez**. Para reintentar, emitir otra.

**`module.shutdown`** (21-oct-2026) apaga la Jetson con `systemctl poweroff`.
Se queda `en_curso` mientras el equipo está apagado y pasa a `aplicada`
(`"el equipo se apago y volvio a encender"`) cuando alguien lo vuelve a
conectar. **No vuelve a encender solo.** Comparte el límite de 3 por hora con
los reinicios.

### `GET /v1/comandos/<command_id>` y `GET /v1/unidades/<id>/comandos?limite=50`
Estado de una orden / las últimas órdenes de una unidad.

### `POST /v1/unidades/<id>/video`
Abre una sesión de video y devuelve la liga para verla.

```json
{"issued_by": "ana@flota", "reason": "revisar incidente", "duration_s": 60}
```

`duration_s`: 5–60 (default 60). Tarda ~2 s: manda `media.open`, espera a que
la unidad lo aplique y a que el video llegue al servidor de medios.

```json
{"command_id": "4c368106951c", "status": "aplicada", "duration_s": 20, "sessions_left": 96,
 "ticket": "rSpE...", "expira": "2026-09-28T04:44:25Z",
 "visor": "http://34.51.107.234:8889/cit-001/?ticket=rSpE...",
 "whep":  "http://34.51.107.234:8889/cit-001/whep?ticket=rSpE..."}
```

- `visor`: página lista para abrir o incrustar en un `<iframe>` (WebRTC, < 1 s de retraso).
- `whep`: para un reproductor WebRTC propio (protocolo WHEP).
- El ticket vale **solo para esa unidad y hasta `expira`**. La unidad corta el
  video sola al cumplirse `duration_s`.
- **No se renueva.** Pedir otra sesión con una abierta → `409` con
  `detail: "sesion en curso, restan N s"`. Para seguir viendo, esperar el
  cierre y pedir otra (cuenta como otra sesión).
- `sessions_left`: sesiones que le quedan a la unidad este mes (tope
  provisional 100). Agotadas → `409`.
- Para cortar antes: `POST .../comandos` con `{"type":"media.close", ...}`.

## Puertos que usa la plataforma

| Puerto | Para qué |
|---|---|
| 8443/tcp | Esta API |
| 8889/tcp | Página y señalización WebRTC del video |
| 8189/udp (y tcp de respaldo) | Medios WebRTC |
