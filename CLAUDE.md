# CLAUDE.md — contexto para asistentes de IA

Este repo es el **lado de la nube de Edge Mobility** (CIT Mobility): la VM que
recibe los datos de las unidades (Jetson a bordo de camiones) y la **API HTTPS**
con la que una plataforma web consulta pasajeros y posición, abre el video de
la cámara y manda órdenes (incluido apagar la unidad).

Lo más probable es que te pidan **conectar un sitio web o una app a esta API**.
Para eso lee primero, en este orden:

1. [INTEGRACION_WEB.md](INTEGRACION_WEB.md): cómo integrarse, paso a paso.
2. [API.md](API.md): el contrato exacto, campo por campo.
3. [`ejemplos/panel-node/`](ejemplos/panel-node/): un panel que ya funciona
   (servidor Node sin dependencias + una página). Probado contra esta API.

El resto (`api.py`, `ingesta.py`, `instalar.sh`, `config/`) es el código que
**corre en la VM**. No hace falta tocarlo para integrar una plataforma; ver
[OPERACION_VM.md](OPERACION_VM.md) si te piden cambiar la VM.

## Datos fijos

- API: `https://34.51.107.234:8443` (prueba). Certificado de una CA propia:
  [`ca.crt`](ca.crt). El certificado del servidor es para la **IP**: conectar
  por IP, no por nombre.
- Autenticación: `Authorization: Bearer <token>`. El token **no está en el
  repo**; lo da el equipo de Edge.
- Unidades: `cit-NNN` (hoy `cit-001`). Horas en **UTC**, ISO 8601 con `Z`.

## Reglas que no se rompen al integrar

1. **El token vive solo en el servidor** (variable de entorno). Nunca en el
   JavaScript del navegador, nunca en el repo, nunca en un `.env` versionado.
   Con él se puede **apagar** una unidad.
2. **El navegador no llama a la API directo.** La API no manda CORS a
   propósito. Haz rutas propias en el backend, una por acción; nada de proxy
   genérico que reenvíe cualquier ruta.
3. **Nunca desactivar la verificación TLS** (`rejectUnauthorized: false`,
   `verify=False`, `curl -k`). Usar `ca.crt`.
4. **`issued_by` y `reason` son obligatorios** en toda orden y quedan en la
   bitácora de la unidad. `issued_by` lo pone el servidor con el usuario que
   inició sesión, no el navegador.
5. **Lista cerrada de órdenes**: `media.open`, `media.close`, `app.restart`,
   `module.restart`, `module.shutdown`, `diagnostics.request`. Otra cosa → `400`.
6. **Video**: sesiones de máx. 60 s que **no se renuevan**; cada una gasta una
   del tope mensual (`sessions_left`). La liga `visor` es `http://` y caduca
   con la sesión; en un sitio `https://` se abre en pestaña nueva (un `<iframe>`
   se bloquearía por contenido mixto).
7. **Apagado (`module.shutdown`)**: la unidad **no vuelve a encender sola**.
   La interfaz debe pedir confirmación escrita, evitar doble clic y mostrar el
   estado (`en_curso` mientras está apagada; `aplicada` cuando la reconectan).
8. **Interpretación de datos**: `lat/lon/speed_kph` son `null` sin GPS (no
   arrastrar la última posición); `speed_kph` no se recalcula; `pax_in/pax_out`
   cuentan desde que encendió la unidad (una bajada brusca = reinicio). Los
   datos se guardan 7 días.
9. **Cada orden se ejecuta una sola vez.** Para reintentar se emite otra. Hay
   un límite de 3 reinicios/apagados por hora por unidad.

## Para probar sin la unidad real

`GET /v1/salud` no pide token y dice si la API está viva. Para no gastar
sesiones de video ni apagar la unidad real mientras desarrollas, simula las
respuestas de la API con los ejemplos de [API.md](API.md).
