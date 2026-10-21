# Integrar un sitio web o una app con Edge Mobility

Guía paso a paso para conectar un sitio web (o una app) con las unidades
Edge: ver pasajeros y posición, abrir el video de la cámara y apagar la
unidad. Si solo quieres los comandos con `curl`, ve a [GUIA_API.md](GUIA_API.md);
el contrato completo campo por campo está en [API.md](API.md).

Hay un **panel de ejemplo que ya funciona** en
[`ejemplos/panel-node/`](ejemplos/panel-node/): úsalo como punto de partida
o copia de ahí lo que necesites.

---

## 1. Cómo encaja todo

```
 CAMIÓN                          NUBE (VM de Edge)                     TU PLATAFORMA
 ──────                          ─────────────────                     ─────────────
 Jetson ──datos cada 1–10 s──►   API  https://34.51.107.234:8443  ◄──  TU SERVIDOR (backend)  ◄──  navegador
        ◄──órdenes─────────────        (guarda 7 días de datos)          guarda el TOKEN            (tu página)
        ──video (solo si se pide)►  servidor de video :8889  ──────────────────────────────────►  navegador
                                                                         (liga "visor" con ticket)
```

- **La Jetson nunca recibe conexiones**: ella se conecta a la VM. Tu
  plataforma **solo habla con la API de la VM**, nunca con la Jetson.
- **Tu navegador NO llama a la API directamente.** Llama a **tu servidor**, y
  tu servidor llama a la API con el token. Hay tres razones:
  1. El token da **control total** de las unidades, incluido apagarlas. En el
     JavaScript del navegador cualquiera lo podría copiar.
  2. La API no manda cabeceras CORS: el navegador **bloquea** las llamadas
     directas desde otro dominio, aunque tengas el token.
  3. El certificado HTTPS de la API es de una CA propia (`ca.crt`). Tu
     servidor lo puede confiar; el navegador de un usuario no.
- **El video sí lo abre el navegador directo**, con la liga `visor` que te
  devuelve la API. Esa liga lleva un ticket de un solo uso que caduca con la
  sesión, así que no expone nada.

---

## 2. Lo que necesitas

| Qué | Dónde está |
|---|---|
| Dirección de la API | `https://34.51.107.234:8443` |
| Token | Te lo pasa el equipo de Edge por un canal privado. Va en una **variable de entorno de tu servidor**, nunca en el código ni en el repo |
| Certificado de la CA | [`ca.crt`](ca.crt), en la raíz de este repo. Es público; no es secreto |
| Identificador de la unidad | `cit-001` (formato `cit-NNN`) |

Prueba rápida desde tu terminal:

```bash
export TOKEN=<el token>
curl --cacert ca.crt https://34.51.107.234:8443/v1/salud                       # sin token
curl --cacert ca.crt -H "Authorization: Bearer $TOKEN" https://34.51.107.234:8443/v1/unidades
```

---

## 3. Llamar a la API desde tu servidor

Siempre con el `ca.crt`. **Nunca desactives la verificación del certificado**
(`rejectUnauthorized: false`, `verify=False`, `curl -k`): con eso cualquiera en
medio podría robar el token.

### Node.js (sin librerías)

```js
const https = require('node:https');
const fs = require('node:fs');

const agente = new https.Agent({ ca: fs.readFileSync('ca.crt') });
const API = 'https://34.51.107.234:8443';

function api(metodo, ruta, cuerpo) {
  return new Promise((ok, mal) => {
    const datos = cuerpo ? JSON.stringify(cuerpo) : null;
    const req = https.request(API + ruta, {
      method: metodo, agent: agente,
      headers: { Authorization: `Bearer ${process.env.EDGE_API_TOKEN}`,
                 ...(datos && { 'Content-Type': 'application/json' }) },
    }, (res) => {
      let t = ''; res.on('data', (c) => (t += c));
      res.on('end', () => ok({ codigo: res.statusCode, json: JSON.parse(t || '{}') }));
    });
    req.on('error', mal);
    if (datos) req.write(datos);
    req.end();
  });
}

// const { json } = await api('GET', '/v1/unidades');
```

Con `fetch` (Node 18+) también funciona, pero hay que arrancar Node con
`NODE_EXTRA_CA_CERTS=ca.crt` para que confíe en la CA.

### Python (`requests`)

```python
import os, requests

API = "https://34.51.107.234:8443"
S = requests.Session()
S.headers["Authorization"] = f"Bearer {os.environ['EDGE_API_TOKEN']}"
S.verify = "ca.crt"            # NUNCA verify=False

unidades = S.get(f"{API}/v1/unidades", timeout=10).json()["unidades"]
```

### Exponer solo lo que tu página necesita

No hagas un proxy que reenvíe cualquier ruta a la API. Crea **rutas propias**,
una por acción, y pon tú el `issued_by` con el usuario que inició sesión en tu
plataforma. Así es como lo hace el panel de ejemplo:

| Tu ruta (la llama tu página) | Lo que hace tu servidor en la API |
|---|---|
| `GET /api/unidades` | `GET /v1/unidades` |
| `GET /api/unidades/:id/telemetria` | `GET /v1/unidades/:id/telemetria?limite=50` |
| `POST /api/unidades/:id/video` | `POST /v1/unidades/:id/video` |
| `POST /api/unidades/:id/cerrar-video` | `POST /v1/unidades/:id/comandos` con `type: media.close` |
| `POST /api/unidades/:id/apagar` | `POST /v1/unidades/:id/comandos?esperar=10` con `type: module.shutdown` |
| `GET /api/comandos/:command_id` | `GET /v1/comandos/:command_id` |

Y protege **tus** rutas con el inicio de sesión de tu plataforma: quien llegue
a `/api/unidades/:id/apagar` puede apagar la unidad.

---

## 4. Mostrar el estado de las unidades

`GET /v1/unidades` devuelve todas con su última muestra. Para algo "en vivo",
pídelo **cada 5 segundos** (la unidad manda una muestra por segundo en marcha
y una cada 10 s detenida; no hay WebSocket).

```json
{"unidades": [{
  "device_id": "cit-001",
  "conexion": "en linea",
  "visto_hace_s": 3.2,
  "ultima_muestra": {
    "ts_device": "2026-10-21T07:32:22.343Z",
    "lat": 19.0286719, "lon": -98.2480798, "speed_kph": 0.0, "gnss_fix": "gps",
    "pax_in": 12, "pax_out": 9, "pax_onboard": 3,
    "health": {"cameras_active": 1, "temp_c": 51.2, "uptime_s": 3600, "...": "..."}
  }
}]}
```

| Para mostrar | Campo | Cuidado con |
|---|---|---|
| ¿Está conectada? | `conexion` (`en linea` / `desconectada`) y `visto_hace_s` | Si `visto_hace_s` pasa de ~30 s, trátala como desconectada aunque diga `en linea` |
| Pasajeros a bordo | `ultima_muestra.pax_onboard` | |
| Subieron / bajaron | `pax_in` / `pax_out` | Cuentan **desde que se encendió la unidad**. Si bajan de golpe es porque se reinició, no porque bajó gente |
| Posición | `lat`, `lon` | Son `null` sin GPS (`gnss_fix: "none"`). **No muestres la última posición vieja** como si fuera actual |
| Velocidad | `speed_kph` | Viene del GPS; no la recalcules con las posiciones |
| Hora del dato | `ts_device` | Está en **UTC**. Conviértela a hora local en la página (`new Date(ts).toLocaleString()`) |

Para un mapa, Leaflet + OpenStreetMap funciona sin claves: un marcador en
`[lat, lon]` que se mueve cada vez que refrescas.

Historial (gráficas, recorridos): `GET /v1/unidades/:id/telemetria?desde=…&hasta=…`
(horas en UTC, máximo 10 000 muestras por consulta, **solo se guardan 7 días**).

---

## 5. Ver el video

1. Tu servidor llama `POST /v1/unidades/:id/video` con
   `{"issued_by": "<usuario>", "reason": "<por qué>", "duration_s": 60}`.
   Tarda ~2 s: la API espera a que la unidad abra la cámara y a que el video
   llegue al servidor.
2. Responde con la liga `visor` (y `whep` para un reproductor propio):
   ```json
   {"status": "aplicada", "duration_s": 60, "sessions_left": 97,
    "expira": "2026-10-21T07:41:19Z",
    "visor": "http://34.51.107.234:8889/cit-001/?ticket=...",
    "whep":  "http://34.51.107.234:8889/cit-001/whep?ticket=..."}
   ```
3. Tu página abre `visor`.

**Ojo con HTTPS.** Hoy el visor es `http://`. Si tu sitio es `https://`, el
navegador **bloquea un `<iframe>` con la liga http** (contenido mixto). Mientras
el visor no tenga HTTPS (pendiente en la VM, ver [OPERACION_VM.md](OPERACION_VM.md)):
- ábrelo **en una pestaña nueva** (`window.open`), que sí se permite; o
- si tu sitio es `http://` (por ejemplo en pruebas), el `<iframe>` funciona:
  ```html
  <iframe src="LIGA_VISOR" allow="autoplay" width="640" height="480"></iframe>
  ```

Truco para que el navegador no bloquee la pestaña: ábrela **en el mismo clic**
(`const v = window.open('', '_blank')`) y, cuando llegue la respuesta, asígnale
la liga (`v.location = visor`). Así lo hace el panel de ejemplo.

Reglas que impone la unidad (no las puede cambiar la plataforma):
- Máximo **60 s** por sesión. **No se alarga**: para seguir viendo, espera a
  que termine y pide otra.
- Pedir con una sesión abierta → `409` con `detail: "sesion en curso, restan N s"`.
- Cada pedido gasta **una sesión del mes** (`sessions_left`, tope provisional
  100). Agotadas → `409`. Muestra `sessions_left` en tu página.
- Cortar antes: `POST /v1/unidades/:id/comandos` con `{"type":"media.close", ...}`.

---

## 6. El botón de apagar

```
POST /v1/unidades/cit-001/comandos?esperar=10
{"type": "module.shutdown", "issued_by": "<usuario>", "reason": "<por qué>"}
```

**La unidad no vuelve a encender sola.** Alguien tiene que ir al camión a
reconectarla. Por eso:

1. **Pide confirmación fuerte** antes de mandarla (por ejemplo, que escriban
   `cit-001`), no un simple "¿Seguro?".
2. **Desactiva el botón** mientras se manda: cada clic es otra orden.
3. **Sigue el estado** con `GET /v1/comandos/:command_id` cada 2 s y muéstralo:

| `status` | Qué decirle al usuario |
|---|---|
| `pendiente` | "Orden enviada, esperando a la unidad". Si además `unidad_sin_conexion: true`: "La unidad está desconectada; si vuelve en menos de 2 min se apagará, si no la orden caduca" |
| `en_curso` | "La unidad se está apagando". **Se queda así** mientras esté apagada |
| `aplicada` | "Apagado confirmado" (aparece cuando alguien la vuelve a encender) |
| `rechazada` | No se ejecutó; el motivo en `detail` (por ejemplo, más de 3 reinicios/apagados en una hora) |
| `caducada` | Llegó tarde (la unidad estaba apagada más de 2 min). **No** se apaga al volver a encenderla |
| `fallida` | Se intentó y falló; motivo en `detail` |

La orden se puede ver también en la lista de comandos de la unidad:
`GET /v1/unidades/:id/comandos?limite=20`.

Otras órdenes con el mismo endpoint (misma forma, distinto `type`):
`app.restart` (reinicia los programas, ~10 s), `module.restart` (reinicia la
Jetson, 1–2 min), `diagnostics.request` (devuelve en `data` procesos,
temperaturas, disco, etc.).

---

## 7. Errores y qué hacer

| HTTP | Significa | En tu plataforma |
|---|---|---|
| `400` | Falta un campo (`issued_by`, `reason`) o un valor no vale | Error de programación: revisa el cuerpo |
| `401` | Token ausente o incorrecto | Revisa la variable de entorno del servidor |
| `404` | Unidad sin datos o comando inexistente | Muestra "sin datos" |
| `409` | La unidad rechazó la orden (video en curso, cuota agotada, contador sin responder) | Muestra `comando.detail` al usuario |
| `503` / `504` | La VM no pudo hablar con el broker o el video no llegó | Reintenta en unos segundos |
| Error de certificado | Tu servidor no está usando `ca.crt` | Configura la CA (sección 3) |

Todas las respuestas son JSON; los errores traen `{"error": "..."}`.

---

## 8. Lista para tu integración

- [ ] El token está en una variable de entorno del **servidor** y fuera del repo.
- [ ] El servidor usa `ca.crt` y verifica el certificado.
- [ ] La página nunca llama a `34.51.107.234:8443` directo; solo a tu servidor.
- [ ] Tus rutas exigen inicio de sesión y ponen `issued_by` con el usuario real.
- [ ] Las horas se muestran en hora local (la API da UTC).
- [ ] Posición `null` se muestra como "sin GPS", no con la última conocida.
- [ ] El video se abre en pestaña nueva si tu sitio es HTTPS.
- [ ] El botón de apagar pide confirmación escrita, se desactiva al usarlo y muestra el estado.
