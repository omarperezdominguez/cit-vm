#!/usr/bin/env python3
# =============================================================================
# api.py -- API de Edge para la plataforma RuTi (ADR-001, fase 3)
#
#   RuTi --HTTPS :8443 + token--> api.py --lee--> SQLite (lo que guardo ingesta.py)
#                                    |--publica QoS 1--> Mosquitto --> cit/v1/<unidad>/cmd --> Jetson
#                                    '--emite ticket de video (vive <= la sesion)
#   navegador --WebRTC :8889?ticket=...--> MediaMTX --pregunta--> api.py :8081 (solo local)
#
# Rutas publicas (todas menos /v1/salud piden "Authorization: Bearer <API_TOKEN>"):
#   GET  /v1/salud
#   GET  /v1/unidades
#   GET  /v1/unidades/<id>
#   GET  /v1/unidades/<id>/telemetria?desde=&hasta=&limite=&completo=
#   GET  /v1/unidades/<id>/comandos?limite=
#   POST /v1/unidades/<id>/comandos?esperar=<s>   {type, issued_by, reason, ttl_s?, params?}
#   GET  /v1/comandos/<command_id>
#   POST /v1/unidades/<id>/video                  {issued_by, reason, duration_s?, camera?}
# Ruta interna (127.0.0.1:8081, sin token):
#   POST /mediamtx/auth    autenticacion HTTP de MediaMTX
#
# Contrato completo con ejemplos: API.md
#
# Variables:
#   API_TOKEN, MQTT_API              secretos.env
#   CIT_API_PUERTO / CIT_API_ESCUCHAR   8443 / 0.0.0.0
#   CIT_API_CERT / CIT_API_LLAVE     /etc/ssl/cit-vm/servidor.{crt,key}; CIT_API_TLS=0 lo apaga (banco)
#   CIT_INTERNO_PUERTO               8081 (siempre en 127.0.0.1)
#   CIT_IP_PUBLICA                   para armar las ligas de video
#   CIT_DB                           ver cit_vm/db.py
# =============================================================================
import hmac
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cit_vm import db  # noqa: E402

API_TOKEN = os.environ.get("API_TOKEN", "")
MQTT_CLAVE = os.environ.get("MQTT_API", "")
PUERTO = int(os.environ.get("CIT_API_PUERTO", "8443"))
ESCUCHAR = os.environ.get("CIT_API_ESCUCHAR", "0.0.0.0")
TLS = os.environ.get("CIT_API_TLS", "1") == "1"
CERT = os.environ.get("CIT_API_CERT", "/etc/ssl/cit-vm/servidor.crt")
LLAVE = os.environ.get("CIT_API_LLAVE", "/etc/ssl/cit-vm/servidor.key")
PUERTO_INTERNO = int(os.environ.get("CIT_INTERNO_PUERTO", "8081"))
IP_PUBLICA = os.environ.get("CIT_IP_PUBLICA", "127.0.0.1")
MEDIAMTX_API = "http://127.0.0.1:9997"

# Lista cerrada del ADR y ttl por omision de cada tipo
TIPOS = {"media.open": 30, "media.close": 30, "app.restart": 120,
         "module.restart": 120, "module.shutdown": 120, "diagnostics.request": 60}
FINALES = {"aplicada", "rechazada", "caducada", "fallida"}
RE_UNIDAD = re.compile(r"^cit-[0-9]{3}$")

_local = threading.local()
_mqtt = None
_mqtt_conectado = threading.Event()

# ticket -> (path, expira_epoch). En memoria: un reinicio los invalida, lo cual
# solo obliga a pedir otra sesion.
_tickets = {}
_tickets_lock = threading.Lock()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg, flush=True)


def bd():
    """Una conexion SQLite por hilo (ThreadingHTTPServer crea un hilo por peticion)."""
    if not hasattr(_local, "c"):
        _local.c = db.conectar()
    return _local.c


def ahora_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ErrorApi(Exception):
    def __init__(self, codigo, mensaje, **extra):
        super().__init__(mensaje)
        self.codigo, self.cuerpo = codigo, {"error": mensaje, **extra}


# --- Lecturas ---------------------------------------------------------------------

def fila_unidad(r):
    muestra = json.loads(r["ultima_muestra"]) if r["ultima_muestra"] else None
    return {
        "device_id": r["device_id"],
        "conexion": r["conexion"] or "desconocida",
        "visto_hace_s": round(time.time() - r["ultimo_recibido"], 1) if r["ultimo_recibido"] else None,
        "ultima_muestra": muestra,
    }


def listar_unidades():
    return [fila_unidad(r) for r in bd().execute("SELECT * FROM unidades ORDER BY device_id")]


def una_unidad(dev):
    r = bd().execute("SELECT * FROM unidades WHERE device_id=?", (dev,)).fetchone()
    if not r:
        raise ErrorApi(404, f"unidad {dev} sin datos")
    return fila_unidad(r)


def telemetria(dev, q):
    limite = min(max(int(q.get("limite", "500")), 1), 10000)
    desde, hasta = q.get("desde"), q.get("hasta")
    sql = "SELECT * FROM telemetria WHERE device_id=?"
    args = [dev]
    if desde:
        sql += " AND ts_device >= ?"; args.append(desde)
    if hasta:
        sql += " AND ts_device <= ?"; args.append(hasta)
    # Sin rango: las mas recientes. Siempre se devuelven en orden cronologico.
    sql += " ORDER BY ts_device " + ("ASC" if desde else "DESC") + " LIMIT ?"
    args.append(limite)
    filas = bd().execute(sql, args).fetchall()
    if not desde:
        filas.reverse()
    if q.get("completo") == "1":
        muestras = [json.loads(f["json"]) for f in filas]
    else:
        campos = ("ts_device", "lat", "lon", "speed_kph", "heading_deg", "gnss_fix",
                  "pax_in", "pax_out", "pax_onboard")
        muestras = [{k: f[k] for k in campos} | {"buffered": bool(f["buffered"])} for f in filas]
    return {"device_id": dev, "n": len(muestras), "muestras": muestras}


def fila_comando(r):
    d = {k: r[k] for k in ("command_id", "device_id", "type", "issued_by", "ts_issued",
                           "ttl_s", "reason", "status", "detail", "ts_applied")}
    d["params"] = json.loads(r["params"]) if r["params"] else None
    d["data"] = json.loads(r["data"]) if r["data"] else None
    # "unidad sin conexion" del ADR: estado de la plataforma, no del equipo
    if d["status"] == "pendiente":
        u = bd().execute("SELECT conexion FROM unidades WHERE device_id=?", (r["device_id"],)).fetchone()
        d["unidad_sin_conexion"] = bool(u and u["conexion"] == "desconectada")
    return d


def un_comando(cid):
    r = bd().execute("SELECT * FROM comandos WHERE command_id=?", (cid,)).fetchone()
    if not r:
        raise ErrorApi(404, f"comando {cid} no existe")
    return fila_comando(r)


def comandos_de(dev, q):
    limite = min(max(int(q.get("limite", "50")), 1), 500)
    filas = bd().execute("SELECT * FROM comandos WHERE device_id=? ORDER BY ts_issued DESC LIMIT ?",
                         (dev, limite)).fetchall()
    return {"device_id": dev, "comandos": [fila_comando(r) for r in filas]}


# --- Ordenes ----------------------------------------------------------------------

def emitir(dev, cuerpo):
    """Valida, registra como 'pendiente' y publica la orden. Devuelve el command_id."""
    tipo = cuerpo.get("type")
    if tipo not in TIPOS:
        raise ErrorApi(400, "type fuera de la lista cerrada", permitidos=sorted(TIPOS))
    for campo in ("issued_by", "reason"):
        if not isinstance(cuerpo.get(campo), str) or not cuerpo[campo].strip():
            raise ErrorApi(400, f"falta {campo} (el ADR lo exige para la trazabilidad)")
    ttl = int(cuerpo.get("ttl_s") or TIPOS[tipo])
    if not 5 <= ttl <= 3600:
        raise ErrorApi(400, "ttl_s debe estar entre 5 y 3600")
    params = cuerpo.get("params")
    if params is not None and not isinstance(params, dict):
        raise ErrorApi(400, "params debe ser un objeto")
    if not _mqtt_conectado.is_set():
        raise ErrorApi(503, "sin conexion con el broker MQTT")

    orden = {"command_id": uuid.uuid4().hex[:12], "device_id": dev, "type": tipo,
             "issued_by": cuerpo["issued_by"].strip(), "ts_issued": ahora_iso(),
             "ttl_s": ttl, "reason": cuerpo["reason"].strip()}
    if params:
        orden["params"] = params
    # Se registra ANTES de publicar: el resultado puede llegar antes de que
    # esta funcion termine y la ingesta necesita encontrar la fila.
    bd().execute(
        """INSERT INTO comandos (command_id, device_id, type, issued_by, ts_issued, ttl_s,
                                 reason, params, status, actualizado)
           VALUES (?,?,?,?,?,?,?,?, 'pendiente', ?)""",
        (orden["command_id"], dev, tipo, orden["issued_by"], orden["ts_issued"], ttl,
         orden["reason"], json.dumps(params) if params else None, time.time()))
    info = _mqtt.publish(f"cit/v1/{dev}/cmd", json.dumps(orden), qos=1, retain=False)
    try:
        info.wait_for_publish(5)
    except Exception:
        pass
    if not info.is_published():
        bd().execute("DELETE FROM comandos WHERE command_id=? AND status='pendiente'", (orden["command_id"],))
        raise ErrorApi(503, "el broker no confirmo la orden")
    log(f"orden {orden['command_id']} {tipo} -> {dev} por {orden['issued_by']}")
    return orden["command_id"]


def esperar_final(cid, segundos):
    fin = time.time() + segundos
    while True:
        c = un_comando(cid)
        if c["status"] in FINALES or time.time() >= fin:
            return c
        time.sleep(0.25)


def pedir_video(dev, cuerpo):
    dur = int(cuerpo.get("duration_s") or 60)
    if not 5 <= dur <= 60:
        raise ErrorApi(400, "duration_s debe estar entre 5 y 60 (ADR: sesion acotada)")
    orden = {"type": "media.open", "issued_by": cuerpo.get("issued_by"), "reason": cuerpo.get("reason"),
             "ttl_s": 30, "params": {"camera": cuerpo.get("camera") or "cam0", "duration_s": dur}}
    cid = emitir(dev, orden)
    c = esperar_final(cid, 15)
    if c["status"] != "aplicada":
        raise ErrorApi(409, "la unidad no abrio el video", comando=c)
    # La Jetson confirmo; se espera a que el flujo este de verdad en MediaMTX
    # para no dar una liga que todavia no muestra nada.
    listo = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{MEDIAMTX_API}/v3/paths/get/{dev}", timeout=2) as r:
                if json.load(r).get("ready"):
                    listo = True
                    break
        except Exception:
            pass
        time.sleep(0.25)
    if not listo:
        raise ErrorApi(504, "la unidad acepto pero el video no llego a MediaMTX", comando=c)
    # El ticket caduca con la sesion (ADR: vigencia <= duration_s)
    ticket = secrets.token_urlsafe(24)
    expira = time.time() + dur
    with _tickets_lock:
        for t in [t for t, (_, e) in _tickets.items() if e < time.time()]:
            del _tickets[t]
        _tickets[ticket] = (dev, expira)
    base = f"http://{IP_PUBLICA}:8889/{dev}"
    return {"command_id": cid, "status": "aplicada", "duration_s": dur,
            "sessions_left": (c.get("data") or {}).get("sessions_left"),
            "ticket": ticket,
            "expira": datetime.fromtimestamp(expira, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "visor": f"{base}/?ticket={ticket}",
            "whep": f"{base}/whep?ticket={ticket}"}


# --- Autenticacion de MediaMTX -------------------------------------------------------

def autorizar_mediamtx(p):
    """Decide si MediaMTX deja pasar una accion. True = 200, False = 401."""
    ip = p.get("ip") or ""
    accion, path = p.get("action"), p.get("path") or ""
    if ip in ("127.0.0.1", "::1"):
        return True                     # API, metricas y lectura por tunel SSH
    if accion == "publish":
        # La proteccion real es la passphrase SRT del path (mediamtx.yml)
        return bool(RE_UNIDAD.match(path))
    if accion == "read":
        q = urllib.parse.parse_qs(p.get("query") or "")
        candidatos = q.get("ticket", []) + [p.get("token") or "", p.get("password") or ""]
        with _tickets_lock:
            for t in candidatos:
                if t and t in _tickets:
                    dueno, expira = _tickets[t]
                    if dueno == path and expira > time.time():
                        return True
    return False


# --- HTTP ------------------------------------------------------------------------------

class Manejador(BaseHTTPRequestHandler):
    server_version = "cit-api/1"
    interno = False

    def log_message(self, fmt, *args):
        if not self.interno:
            log(f"{self.client_address[0]} {fmt % args}")

    def _responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo, ensure_ascii=False, indent=1).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ErrorApi(413, "cuerpo demasiado grande")
        if not n:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            raise ErrorApi(400, "JSON invalido")
        if not isinstance(d, dict):
            raise ErrorApi(400, "se esperaba un objeto JSON")
        return d

    def _autorizado(self):
        h = self.headers.get("Authorization", "")
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), API_TOKEN.encode())

    def _despachar(self, metodo):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        partes = [p for p in url.path.split("/") if p]
        try:
            if self.interno:
                if metodo == "POST" and partes == ["mediamtx", "auth"]:
                    p = self._cuerpo()
                    ok = autorizar_mediamtx(p)
                    if not ok:
                        log(f"MediaMTX niega {p.get('action')} {p.get('path')} desde {p.get('ip')} ({p.get('protocol')})")
                    return self._responder(200 if ok else 401, {})
                raise ErrorApi(404, "ruta inexistente")

            if partes == ["v1", "salud"] and metodo == "GET":
                return self._responder(200, {"ok": True, "mqtt": _mqtt_conectado.is_set(),
                                             "hora_utc": ahora_iso()})
            if not self._autorizado():
                raise ErrorApi(401, "falta o no vale el token (Authorization: Bearer ...)")
            if len(partes) >= 3 and partes[:2] == ["v1", "unidades"] and not RE_UNIDAD.match(partes[2]):
                raise ErrorApi(400, "device_id invalido (cit-NNN)")

            if metodo == "GET":
                if partes == ["v1", "unidades"]:
                    return self._responder(200, {"unidades": listar_unidades()})
                if len(partes) == 3 and partes[1] == "unidades":
                    return self._responder(200, una_unidad(partes[2]))
                if len(partes) == 4 and partes[1] == "unidades" and partes[3] == "telemetria":
                    return self._responder(200, telemetria(partes[2], q))
                if len(partes) == 4 and partes[1] == "unidades" and partes[3] == "comandos":
                    return self._responder(200, comandos_de(partes[2], q))
                if len(partes) == 3 and partes[1] == "comandos":
                    return self._responder(200, un_comando(partes[2]))
            if metodo == "POST":
                if len(partes) == 4 and partes[1] == "unidades" and partes[3] == "comandos":
                    cid = emitir(partes[2], self._cuerpo())
                    espera = min(max(float(q.get("esperar", "0")), 0), 30)
                    c = esperar_final(cid, espera) if espera else un_comando(cid)
                    return self._responder(202 if c["status"] not in FINALES else 200, c)
                if len(partes) == 4 and partes[1] == "unidades" and partes[3] == "video":
                    return self._responder(200, pedir_video(partes[2], self._cuerpo()))
            raise ErrorApi(404, "ruta inexistente")
        except ErrorApi as e:
            self._responder(e.codigo, e.cuerpo)
        except ValueError as e:
            self._responder(400, {"error": f"parametro invalido: {e}"})
        except Exception as e:
            log(f"ERROR {metodo} {self.path}: {e!r}")
            self._responder(500, {"error": "error interno"})

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")


class ManejadorInterno(Manejador):
    interno = True


class ServidorTLS(ThreadingHTTPServer):
    """El handshake TLS se hace en el hilo de cada conexion, no en el que acepta:
    un cliente lento o un escaneo no bloquea a los demas."""
    daemon_threads = True
    ctx = None

    def finish_request(self, request, client_address):
        request.settimeout(30)
        if self.ctx:
            request = self.ctx.wrap_socket(request, server_side=True)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        log(f"{client_address[0]} conexion descartada: {sys.exc_info()[1]!r}")


# --- MQTT ------------------------------------------------------------------------------

def arrancar_mqtt():
    global _mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"api-{os.getpid()}",
                    clean_session=True, protocol=mqtt.MQTTv311)
    c.username_pw_set("api", MQTT_CLAVE)

    def al_conectar(_c, _u, _f, rc, _p=None):
        if rc.is_failure:
            log(f"broker rechazo a la API: {rc}")
        else:
            _mqtt_conectado.set()
            log("API conectada al broker")

    def al_desconectar(*_a):
        _mqtt_conectado.clear()
        log("API desconectada del broker; reintentando")

    c.on_connect, c.on_disconnect = al_conectar, al_desconectar
    c.reconnect_delay_set(1, 10)
    c.connect_async("127.0.0.1", 1883, keepalive=30)
    c.loop_start()
    _mqtt = c


def main():
    if not API_TOKEN or not MQTT_CLAVE:
        sys.exit("Faltan API_TOKEN o MQTT_API (secretos.env).")
    bd()  # crea el esquema si la ingesta aun no lo hizo
    arrancar_mqtt()

    interno = ServidorTLS(("127.0.0.1", PUERTO_INTERNO), ManejadorInterno)
    threading.Thread(target=interno.serve_forever, daemon=True).start()

    publico = ServidorTLS((ESCUCHAR, PUERTO), Manejador)
    if TLS:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, LLAVE)
        publico.ctx = ctx
    log(f"API en {'https' if TLS else 'http'}://{ESCUCHAR}:{PUERTO}  (interna 127.0.0.1:{PUERTO_INTERNO})")
    try:
        publico.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
