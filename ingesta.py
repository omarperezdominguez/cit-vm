#!/usr/bin/env python3
# =============================================================================
# ingesta.py -- guarda en la BD todo lo que las unidades suben (ADR-001, fase 3)
#
#   Mosquitto :1883 (local) --QoS 1, sesion persistente--> ingesta.py --> SQLite
#       cit/v1/+/telemetria  -> tabla telemetria (dedup por sample_id)
#       cit/v1/+/resultado   -> tablas resultados y comandos (dedup por command_id+status)
#       cit/v1/+/estado      -> tabla unidades (en linea / desconectada)
#
# Garantias:
#   - Sesion persistente (client_id fijo, clean_session=False): lo que llega
#     mientras la ingesta esta caida o reiniciando lo guarda el broker.
#   - Confirmacion manual: el PUBACK al broker sale DESPUES del commit. Si la
#     ingesta muere a mitad, el broker vuelve a entregar; el dedup lo absorbe.
#   - Cada hora borra lo que tenga mas de CIT_RETENCION_DIAS (7) dias.
#
# Uso:  python3 ingesta.py            (en produccion lo lanza systemd: cit-ingesta)
#
# Variables:
#   CIT_MQTT_HOST / CIT_MQTT_PUERTO   127.0.0.1 / 1883
#   MQTT_INGESTA                      contrasena del usuario ingesta (secretos.env)
#   CIT_INGESTA_ID                    client_id de la sesion persistente (ingesta)
#   CIT_DB, CIT_RETENCION_DIAS        ver cit_vm/db.py
# =============================================================================
import json
import os
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cit_vm import db  # noqa: E402

HOST = os.environ.get("CIT_MQTT_HOST", "127.0.0.1")
PUERTO = int(os.environ.get("CIT_MQTT_PUERTO", "1883"))
USUARIO = "ingesta"
CONTRASENA = os.environ.get("MQTT_INGESTA", "")
CLIENT_ID = os.environ.get("CIT_INGESTA_ID", "ingesta")
CADA_LIMPIEZA_S = 3600

# Un estado final no debe volver a en_curso si llega un reenvio atrasado
FINALES = {"aplicada", "rechazada", "caducada", "fallida"}

cuenta = {"telemetria": 0, "duplicadas": 0, "resultado": 0, "estado": 0, "invalidos": 0}
_bd = None           # conexion del hilo de red de paho (on_message)
_fin = threading.Event()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg, flush=True)


# --- Guardado -------------------------------------------------------------------

def guardar_telemetria(dispositivo, m):
    if not isinstance(m, dict) or not m.get("sample_id") or not m.get("ts_device"):
        raise ValueError("muestra sin sample_id o ts_device")
    if m.get("device_id") != dispositivo:
        raise ValueError(f"device_id {m.get('device_id')!r} no coincide con el tema")
    ahora = time.time()
    cur = _bd.execute(
        """INSERT OR IGNORE INTO telemetria
           (sample_id, device_id, vehicle_id, ts_device, recibido, lat, lon, speed_kph,
            heading_deg, gnss_fix, pax_in, pax_out, pax_onboard, buffered, json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (m["sample_id"], dispositivo, m.get("vehicle_id"), m["ts_device"], ahora,
         m.get("lat"), m.get("lon"), m.get("speed_kph"), m.get("heading_deg"),
         m.get("gnss_fix"), m.get("pax_in"), m.get("pax_out"), m.get("pax_onboard"),
         1 if m.get("buffered") else 0, json.dumps(m, separators=(",", ":"))))
    if cur.rowcount == 0:
        cuenta["duplicadas"] += 1
        return
    cuenta["telemetria"] += 1
    # "Ultima muestra" = la mas reciente por ts_device, no la ultima en llegar:
    # un atraso vaciado tras un corte no debe pisar la posicion actual.
    _bd.execute(
        """INSERT INTO unidades (device_id, ultima_muestra, ts_ultima_muestra, ultimo_recibido)
           VALUES (?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET
             ultimo_recibido = excluded.ultimo_recibido,
             ultima_muestra = CASE WHEN ts_ultima_muestra IS NULL
                                    OR excluded.ts_ultima_muestra > ts_ultima_muestra
                               THEN excluded.ultima_muestra ELSE ultima_muestra END,
             ts_ultima_muestra = MAX(COALESCE(ts_ultima_muestra, ''), excluded.ts_ultima_muestra)""",
        (dispositivo, json.dumps(m, separators=(",", ":")), m["ts_device"], ahora))


def guardar_resultado(dispositivo, r):
    if not isinstance(r, dict) or not r.get("command_id") or not r.get("status"):
        raise ValueError("resultado sin command_id o status")
    ahora = time.time()
    cur = _bd.execute(
        "INSERT OR IGNORE INTO resultados (command_id, status, device_id, recibido, json) VALUES (?,?,?,?,?)",
        (r["command_id"], r["status"], dispositivo, ahora, json.dumps(r, separators=(",", ":"))))
    cuenta["resultado"] += 1
    if cur.rowcount == 0:
        return  # mismo (command_id, status) ya visto: reenvio o duplicate:true
    data = json.dumps(r["data"]) if r.get("data") is not None else None
    # Si la orden no la emitio esta API (p. ej. mosquitto_pub a mano), se da de
    # alta con lo que trae el resultado, para que tambien quede trazada.
    _bd.execute(
        """INSERT INTO comandos (command_id, device_id, type, issued_by, ts_issued, ttl_s,
                                 reason, params, status, detail, data, ts_applied, actualizado)
           VALUES (?,?,?,?,?,0,'',NULL,?,?,?,?,?)
           ON CONFLICT(command_id) DO UPDATE SET
             status = CASE WHEN comandos.status IN ('aplicada','rechazada','caducada','fallida')
                                AND excluded.status NOT IN ('aplicada','rechazada','caducada','fallida')
                           THEN comandos.status ELSE excluded.status END,
             detail = COALESCE(excluded.detail, comandos.detail),
             data = COALESCE(excluded.data, comandos.data),
             ts_applied = COALESCE(excluded.ts_applied, comandos.ts_applied),
             actualizado = excluded.actualizado""",
        (r["command_id"], dispositivo, r.get("type") or "?", r.get("issued_by") or "?",
         r.get("ts_received") or "", r["status"], r.get("detail"), data,
         r.get("ts_applied"), ahora))


def guardar_estado(dispositivo, texto):
    if not texto:
        return  # retenido borrado: no dice nada
    # El agente manda {"status":"en linea",...}; el testamento puede ser texto plano
    try:
        d = json.loads(texto)
        if isinstance(d, dict) and d.get("status"):
            texto = d["status"]
    except ValueError:
        pass
    cuenta["estado"] += 1
    _bd.execute(
        """INSERT INTO unidades (device_id, conexion, ts_conexion) VALUES (?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET conexion=excluded.conexion, ts_conexion=excluded.ts_conexion""",
        (dispositivo, texto, time.time()))


# --- MQTT ------------------------------------------------------------------------

def al_conectar(cliente, _u, flags, rc, _p=None):
    if rc.is_failure:
        log(f"conexion rechazada por el broker: {rc}")
        return
    log(f"conectada a {HOST}:{PUERTO} como {CLIENT_ID} (sesion previa: {flags.session_present})")
    cliente.subscribe([("cit/v1/+/telemetria", 1), ("cit/v1/+/resultado", 1), ("cit/v1/+/estado", 1)])


def al_desconectar(_c, _u, _f, rc, _p=None):
    if not _fin.is_set():
        log(f"desconectada del broker ({rc}); paho reintenta solo")


def al_recibir(cliente, _u, msg):
    partes = msg.topic.split("/")
    try:
        if len(partes) != 4 or partes[0] != "cit" or partes[1] != "v1":
            raise ValueError("tema fuera del contrato")
        dispositivo, tipo = partes[2], partes[3]
        texto = msg.payload.decode("utf-8")
        _bd.execute("BEGIN")
        try:
            if tipo == "telemetria":
                guardar_telemetria(dispositivo, json.loads(texto))
            elif tipo == "resultado":
                guardar_resultado(dispositivo, json.loads(texto))
            elif tipo == "estado":
                guardar_estado(dispositivo, texto)
            _bd.execute("COMMIT")
        except Exception:
            _bd.execute("ROLLBACK")
            raise
    except (ValueError, UnicodeDecodeError) as e:
        # Mensaje invalido: se confirma igual; reentregarlo no lo arreglaria.
        cuenta["invalidos"] += 1
        log(f"descartado {msg.topic}: {e}")
    except Exception as e:
        # Error de la BD (disco lleno, bloqueo): NO se confirma. Se sale sin
        # despedirse; systemd reinicia y el broker reentrega lo no confirmado.
        log(f"ERROR guardando {msg.topic}: {e!r}; se sale para reintentar")
        os._exit(1)
    cliente.ack(msg.mid, msg.qos)


# --- Retencion y resumen -----------------------------------------------------------

def mantenimiento():
    c = db.conectar()
    siguiente_limpieza = 0.0
    while not _fin.wait(60):
        log("resumen: " + " ".join(f"{k}={v}" for k, v in cuenta.items()))
        if time.time() >= siguiente_limpieza:
            try:
                b = db.borrar_viejos(c)
                log(f"retencion {db.RETENCION_DIAS:g} dias: borradas " + " ".join(f"{k}={v}" for k, v in b.items()))
            except Exception as e:
                log(f"ERROR en la retencion: {e!r}")
            siguiente_limpieza = time.time() + CADA_LIMPIEZA_S


def main():
    global _bd
    if not CONTRASENA:
        sys.exit("Falta MQTT_INGESTA (contrasena del usuario ingesta).")
    _bd = db.conectar()
    # Limpieza al arrancar tambien: si la VM estuvo apagada dias
    log(f"BD {db.RUTA}; retencion al arrancar: {db.borrar_viejos(_bd)}")

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                          clean_session=False, protocol=mqtt.MQTTv311, manual_ack=True)
    cliente.username_pw_set(USUARIO, CONTRASENA)
    cliente.on_connect = al_conectar
    cliente.on_disconnect = al_desconectar
    cliente.on_message = al_recibir
    cliente.reconnect_delay_set(1, 20)

    def terminar(*_):
        _fin.set()
        cliente.disconnect()
    signal.signal(signal.SIGTERM, terminar)
    signal.signal(signal.SIGINT, terminar)

    threading.Thread(target=mantenimiento, daemon=True).start()
    while not _fin.is_set():
        try:
            cliente.connect(HOST, PUERTO, keepalive=30)
            break
        except OSError as e:
            log(f"broker no disponible ({e}); reintento en 5 s")
            _fin.wait(5)
    cliente.loop_forever(retry_first_connection=True)
    log("ingesta detenida")


if __name__ == "__main__":
    main()
