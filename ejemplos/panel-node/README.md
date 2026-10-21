# Panel de ejemplo (Node, sin dependencias)

Un panel que ya funciona contra la API de Edge: muestra cada unidad
(conexión, pasajeros, velocidad, posición en el mapa), abre el video y tiene
el botón de **apagar** con confirmación. Sirve para probar y como base para
la plataforma real.

```
navegador ──HTTP + usuario/clave──► server.js (guarda el token) ──HTTPS + token + ca.crt──► API de Edge
```

## Arrancar

Necesita Node 18 o más nuevo.

```bash
cd ejemplos/panel-node
cp .env.example .env
nano .env          # EDGE_API_TOKEN, PANEL_USUARIO y PANEL_CLAVE
node server.js     # o: npm start
```

Abre `http://localhost:3000`. El navegador te pide el usuario y la clave
del `.env`.

## Qué hay

| Archivo | |
|---|---|
| `server.js` | Servidor del panel. Guarda el token, confía en `ca.crt` y expone **solo** estas rutas: `GET /api/unidades`, `GET /api/unidades/:id/telemetria`, `POST /api/unidades/:id/video`, `POST /api/unidades/:id/cerrar-video`, `POST /api/unidades/:id/apagar` (exige `{"confirmar": "<id>"}`) y `GET /api/comandos/:id` |
| `public/index.html` | La página: refresca cada 5 s; abre el video en una pestaña nueva; el botón de apagar pide escribir el nombre de la unidad y sigue el estado de la orden |
| `.env.example` | Plantilla de configuración. El `.env` real no va al repo |

## Probado

El 21-oct-2026, contra `api.py`, `ingesta.py` y el agente real de la Jetson
corriendo en local:
- sin usuario/clave el panel responde 401;
- lista la unidad con su conexión y su última muestra;
- apagar sin confirmación → 400; con confirmación → la orden llega a la
  unidad y queda `en_curso` firmada con `PANEL_USUARIO`;
- si la unidad no puede abrir el video, la página muestra el motivo
  (`detail` del comando);
- no reenvía rutas arbitrarias a la API (no es un proxy abierto).

## Para llevarlo a la plataforma real

- Cambia el usuario/clave de `.env` por el inicio de sesión de tu plataforma, y
  usa el usuario real como `issued_by`.
- Sirve la plataforma con HTTPS. El visor de video seguirá abriéndose en
  pestaña nueva mientras la VM no tenga HTTPS en el puerto 8889.
- Si usas Express, Next.js, Django, etc., la lógica es la misma: copia la
  función `api()` de `server.js` y las rutas de la tabla.
