# Guía rápida de la API de Edge Mobility

Cómo consultar la posición y la velocidad de las unidades, ver el video de la
cámara y mandar un reinicio. Para el detalle completo (todos los campos y
estados), ver [API.md](API.md).

Entorno de **prueba**.

---

## 1. Lo que necesitas

| | |
|---|---|
| Dirección de la API | `https://34.51.107.234:8443` |
| Token | Te lo pasa el equipo de Edge por un canal privado. **Nunca va en el repo ni en el navegador** |
| Certificado `ca.crt` | Abajo. Sirve para que la conexión HTTPS sea válida |

> **Entorno de prueba:** este token es el mismo para todas las plataformas.
> Cualquier plataforma nueva usa este mismo token; no hay que pedir otro.
> Antes de pasar a producción se cambiará por uno por plataforma.

En los ejemplos el token va en una variable de la terminal, así no se escribe en ningún archivo:

```bash
export TOKEN=<el token que te pasaron>
```

Cada petición lleva esta cabecera:

```
Authorization: Bearer <TOKEN>
```

Sin ella, la API responde `401`.

Aun en pruebas, conviene usar el token desde el servidor de la plataforma y no
en el código del navegador: así ya queda hecho como será en producción.

### El certificado `ca.crt`

Copia este texto tal cual en un archivo llamado `ca.crt`:

```
-----BEGIN CERTIFICATE-----
MIIBvzCCAWWgAwIBAgIUPuHiiOb5BQHDqPI9D7HwrhYdARQwCgYIKoZIzj0EAwIw
LTEVMBMGA1UECgwMQ0lUIE1vYmlsaXR5MRQwEgYDVQQDDAtDSVQgRWRnZSBDQTAe
Fw0yNjA5MjgwMzEyMzBaFw0zNjA5MjUwMzEyMzBaMC0xFTATBgNVBAoMDENJVCBN
b2JpbGl0eTEUMBIGA1UEAwwLQ0lUIEVkZ2UgQ0EwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAATrJwD9x00Ifnr9yS7tnKQbgc1S4TB7aY84YJtKGw1xnnqbS76LWbv3
NOhkFHMteb8Sa8dKVfjFerK/JHJVDQl7o2MwYTAdBgNVHQ4EFgQUAzANQUC/LgK8
HUE+uOGsxVQ7y+YwHwYDVR0jBBgwFoAUAzANQUC/LgK8HUE+uOGsxVQ7y+YwDwYD
VR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAQYwCgYIKoZIzj0EAwIDSAAwRQIg
Ftt6X4yWPBI/p8lF8SfWV6mcZ4yyUTiQX5uTOV2Sl70CIQDp+dfZGsetxgOR68Lx
eV1ekfYyD5i7PudhYlVTQ0WVVA==
-----END CERTIFICATE-----
```

Si solo quieres probar rápido sin el archivo, `curl -k` (o `curl.exe -k` en
Windows) se salta la verificación del certificado. Sirve para pruebas, no
para una integración real.

En los ejemplos se usa `curl`. En **Windows** usa `curl.exe` y agrega
`--ssl-no-revoke`:

```bash
curl.exe --ssl-no-revoke --cacert ca.crt -H "Authorization: Bearer $TOKEN" https://34.51.107.234:8443/v1/unidades
```

---

## 2. Consultar los datos (posición, velocidad, pasajeros)

### Estado actual de todas las unidades

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" \
  https://34.51.107.234:8443/v1/unidades
```

Respuesta (resumida):

```json
{"unidades": [{
  "device_id": "cit-001",
  "conexion": "en linea",
  "visto_hace_s": 4.1,
  "ultima_muestra": {
    "ts_device": "2026-09-29T02:27:42.690Z",
    "lat": 19.028706,
    "lon": -98.248179,
    "speed_kph": 0.0,
    "gnss_fix": "gps",
    "pax_onboard": 0
  }
}]}
```

| Campo | Qué es |
|---|---|
| `conexion` | `en linea` o `desconectada` |
| `visto_hace_s` | Segundos desde el último dato recibido |
| `lat`, `lon` | Posición. `null` si no hay señal GPS |
| `speed_kph` | Velocidad en km/h |
| `gnss_fix` | `gps` = posición válida; `none` = sin posición |
| `pax_onboard` | Pasajeros a bordo |
| `ts_device` | Hora del dato, en **UTC** |

Para tener la posición "en vivo", repite esta consulta cada 2–5 segundos.

### Historial de una unidad

Últimas 100 muestras:

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" \
  "https://34.51.107.234:8443/v1/unidades/cit-001/telemetria?limite=100"
```

Entre dos horas (en UTC):

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" \
  "https://34.51.107.234:8443/v1/unidades/cit-001/telemetria?desde=2026-09-29T02:00:00Z&hasta=2026-09-29T03:00:00Z"
```

Los datos se guardan **7 días**.

---

## 3. Ver el video (stream)

### Paso 1: pedir la sesión

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" -X POST \
  -d '{"issued_by":"ana@flota","reason":"revisar incidente","duration_s":60}' \
  https://34.51.107.234:8443/v1/unidades/cit-001/video
```

| Campo | |
|---|---|
| `issued_by` | Quién lo pide (obligatorio) |
| `reason` | Por qué (obligatorio) |
| `duration_s` | Segundos de video, de 5 a 60 |

Respuesta:

```json
{
  "status": "aplicada",
  "duration_s": 60,
  "sessions_left": 95,
  "expira": "2026-09-29T02:31:06Z",
  "visor": "http://34.51.107.234:8889/cit-001/?ticket=C-yRj0...",
  "whep":  "http://34.51.107.234:8889/cit-001/whep?ticket=C-yRj0..."
}
```

### Paso 2: mostrar el video

- **Lo más fácil:** abrir la liga `visor` en el navegador, o ponerla en un
  `<iframe>` dentro de tu página:

  ```html
  <iframe src="LIGA_VISOR" allow="autoplay" width="640" height="480"></iframe>
  ```

- **Reproductor propio:** usar la liga `whep` con cualquier reproductor
  WebRTC compatible con WHEP.

### Reglas del video

- La liga **solo sirve hasta la hora de `expira`**. Hay que abrirla enseguida.
- La sesión dura como máximo **60 segundos** y **no se puede alargar**. Para
  seguir viendo, espera a que termine y pide otra.
- Si pides video con una sesión abierta, la respuesta es `409` (`sesion en curso`).
- Cada pedido gasta una sesión del **tope mensual** de la unidad
  (`sessions_left`).

### Cerrar el video antes de tiempo

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" -X POST \
  -d '{"type":"media.close","issued_by":"ana@flota","reason":"ya no se necesita"}' \
  https://34.51.107.234:8443/v1/unidades/cit-001/comandos
```

---

## 4. Mandar una orden de reinicio

Hay dos tipos de reinicio:

| `type` | Qué reinicia | Tarda |
|---|---|---|
| `app.restart` | Solo los programas de la unidad (cámara, GPS, envío) | ~10 s |
| `module.restart` | El equipo completo (Jetson) | 1–2 min |

Ejemplo, reiniciar la aplicación y esperar hasta 20 s la respuesta:

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" -X POST \
  -d '{"type":"app.restart","issued_by":"ana@flota","reason":"la camara no responde"}' \
  "https://34.51.107.234:8443/v1/unidades/cit-001/comandos?esperar=20"
```

Respuesta:

```json
{"command_id": "49955b99a1c2", "type": "app.restart", "status": "aplicada", "detail": null}
```

Para consultar después cómo terminó una orden:

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" \
  https://34.51.107.234:8443/v1/comandos/49955b99a1c2
```

### Qué significa el `status`

| `status` | Significado |
|---|---|
| `pendiente` | Enviada; la unidad aún no responde (puede estar sin conexión) |
| `en_curso` | La unidad la recibió y la está ejecutando |
| `aplicada` | Hecha |
| `rechazada` | La unidad no la ejecutó. El motivo está en `detail` |
| `caducada` | Llegó demasiado tarde |
| `fallida` | Se intentó y falló. El motivo está en `detail` |

### Límites

- **Máximo 3 reinicios por hora** por unidad, sumando los dos tipos. El cuarto
  sale `rechazada`. Esto lo controla la propia unidad para evitar que quede
  reiniciándose sin parar.
- **Apagar el equipo** (desde el 21-oct-2026): `type: "module.shutdown"`.
  La Jetson se apaga y **no vuelve a encender sola**: hay que ir al camión
  (o esperar a que la ignición la encienda, cuando exista). Ver la sección 5.

---

## 5. Apagar la unidad

```bash
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -X POST \
  -d '{"type":"module.shutdown","issued_by":"ana@flota","reason":"fin de jornada"}' \
  "https://34.51.107.234:8443/v1/unidades/cit-001/comandos?esperar=20"
```

Qué esperar:

| Momento | `status` |
|---|---|
| La unidad recibe la orden y empieza a apagarse | `en_curso` (se queda así mientras esté apagada) |
| La unidad vuelve a encender (alguien la conecta) | `aplicada`, `detail: "el equipo se apago y volvio a encender"` |
| La unidad ya estaba apagada o sin señal | `pendiente` con `unidad_sin_conexion: true`. Si vuelve antes de `ttl_s` (120 s) se apaga; si no, queda `caducada` y **no** se apaga al prenderla |

- Cuenta para el límite de 3 reinicios/apagados por hora.
- Mandarla **una sola vez**: un segundo clic emite otra orden.
- En un sitio web, pedir confirmación antes de mandarla (ver
  [INTEGRACION_WEB.md](INTEGRACION_WEB.md)).

---

## 6. Errores frecuentes

| Código | Causa | Qué hacer |
|---|---|---|
| `401` | Falta el token o es incorrecto | Revisar la cabecera `Authorization` |
| `400` | Falta un campo (`issued_by`, `reason`) o un valor no es válido | Leer el mensaje de `error` |
| `409` | La unidad rechazó la orden (sesión de video abierta, cuota agotada…) | Leer `detail` |
| `503` / `504` | Falla interna de la VM o el video no llegó | Reintentar en unos segundos |
| Error de certificado | Falta `--cacert ca.crt` (y en Windows, `--ssl-no-revoke`) | Agregar las opciones |
