#!/usr/bin/env python3
# =============================================================================
# prueba_humo.py -- comprueba la fase 2 de la VM sin la Jetson
#
#   "unidad" (simula al agente) --TLS :8883--> Mosquitto <--:1883-- ingesta / api
#   ffmpeg (simula al contador) --SRT :8890--> MediaMTX --> ffprobe (lectura local)
#
# Cada prueba imprime OK o FALLA; al final, el total. Sale con 1 si algo fallo.
#
# Uso (necesita leer /etc/cit-vm/secretos.env):
#   sudo python3 prueba_humo.py                 # broker por 127.0.0.1
#   sudo HOST=34.51.107.234 python3 prueba_humo.py   # por la IP publica
#   sudo SIN_REINICIO=1 python3 prueba_humo.py  # no reinicia mosquitto
# =============================================================================
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import paho.mqtt.client as mqtt

HOST = os.environ.get("HOST", "127.0.0.1")
UNIDAD = "cit-000"          # unidad ficticia (instalar.sh la da de alta siempre)
# Nunca se usa una unidad real: el broker echaria al agente en linea, la prueba
# le robaria su sesion persistente y ensuciaria su estado en la BD.
ID_PRUEBA = UNIDAD + "-prueba"
CA = "/etc/mosquitto/certs/ca.crt"
SECRETOS = "/etc/cit-vm/secretos.env"

resultados = []


def informar(nombre, ok, detalle=""):
    resultados.append(ok)
    print(("  OK    " if ok else "  FALLA ") + nombre + (f"  ({detalle})" if detalle else ""))


def leer_secretos():
    s = {}
    with open(SECRETOS) as f:
        for linea in f:
            if "=" in linea:
                k, v = linea.strip().split("=", 1)
                s[k] = v
    return s


# --- Clientes MQTT -----------------------------------------------------------

class Cliente:
    """Envoltura minima: conecta, guarda lo recibido y espera por condicion."""

    def __init__(self, client_id, usuario, contrasena, host, puerto, tls,
                 limpia=True, testamento=None):
        self.recibidos = []
        self.rc = None
        self.conectado = threading.Event()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             clean_session=limpia, protocol=mqtt.MQTTv311)
        self.c.username_pw_set(usuario, contrasena)
        if tls:
            self.c.tls_set(ca_certs=CA, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if testamento:
            self.c.will_set(*testamento, qos=1, retain=True)
        self.c.on_connect = self._al_conectar
        self.c.on_message = lambda c, u, m: self.recibidos.append((m.topic, m.payload.decode(), m.retain))
        self.host, self.puerto = host, puerto

    def _al_conectar(self, c, u, flags, rc, props=None):
        self.rc = rc
        self.conectado.set()

    def conectar(self, espera=5):
        try:
            self.c.connect(self.host, self.puerto, keepalive=5)
        except Exception as e:  # TLS rechazado, puerto cerrado...
            self.rc = e
            return False
        self.c.loop_start()
        self.conectado.wait(espera)
        return self.rc is not None and not isinstance(self.rc, Exception) and not self.rc.is_failure

    def esperar(self, cond, espera=5):
        fin = time.time() + espera
        while time.time() < fin:
            if any(cond(t, p, r) for t, p, r in self.recibidos):
                return True
            time.sleep(0.1)
        return False

    def cerrar(self):
        self.c.disconnect()
        self.c.loop_stop()


def ahora_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def prueba_mqtt(s):
    print(f"\n== MQTT (unidad por {HOST}:8883 con TLS, servicios por 127.0.0.1:1883) ==")
    tema = f"cit/v1/{UNIDAD}"

    # Ingesta: sesion persistente, como exige el PLAN (fase 3)
    ingesta = Cliente("ingesta-prueba", "ingesta", s["MQTT_INGESTA"], "127.0.0.1", 1883, False, limpia=False)
    informar("ingesta conecta por localhost", ingesta.conectar())
    ingesta.c.subscribe([("cit/v1/+/telemetria", 1), ("cit/v1/+/resultado", 1), ("cit/v1/+/estado", 1)])

    api = Cliente("api-prueba", "api", s["MQTT_API"], "127.0.0.1", 1883, False)
    informar("api conecta por localhost", api.conectar())

    # La unidad, igual que agente.py: TLS, sesion persistente, testamento retenido
    unidad = Cliente(ID_PRUEBA, UNIDAD, s["MQTT_CIT_000"], HOST, 8883, True, limpia=False,
                     testamento=(f"{tema}/estado", "desconectada"))
    informar("unidad conecta con TLS y valida el certificado", unidad.conectar(), f"rc={unidad.rc}")
    unidad.c.subscribe(f"{tema}/cmd", qos=1)
    time.sleep(0.5)

    # Telemetria de subida
    unidad.c.publish(f"{tema}/estado", "en linea", qos=1, retain=True)
    muestra = {"schema_version": 1, "device_id": UNIDAD, "sample_id": uuid.uuid4().hex,
               "ts_device": ahora_utc(), "pax_in": 0, "pax_out": 0, "buffered": False}
    unidad.c.publish(f"{tema}/telemetria", json.dumps(muestra), qos=1).wait_for_publish(5)
    informar("telemetria llega a la ingesta",
             ingesta.esperar(lambda t, p, r: t == f"{tema}/telemetria" and muestra["sample_id"] in p))
    informar("estado 'en linea' llega retenido o en vivo",
             ingesta.esperar(lambda t, p, r: t == f"{tema}/estado" and p == "en linea"))

    # Comando de bajada
    cmd = {"command_id": uuid.uuid4().hex[:8], "device_id": UNIDAD, "type": "diagnostics.request",
           "issued_by": "prueba_humo", "ts_issued": ahora_utc(), "ttl_s": 60, "reason": "prueba"}
    api.c.publish(f"{tema}/cmd", json.dumps(cmd), qos=1).wait_for_publish(5)
    informar("comando de la api llega a la unidad",
             unidad.esperar(lambda t, p, r: t == f"{tema}/cmd" and cmd["command_id"] in p))

    # --- ACL: una unidad no puede ver ni escribir lo de otra ---
    otra_marca = uuid.uuid4().hex
    unidad.c.publish("cit/v1/cit-002/telemetria", otra_marca, qos=1).wait_for_publish(5)
    informar("ACL: la unidad NO puede publicar como otra unidad",
             not ingesta.esperar(lambda t, p, r: otra_marca in p, espera=2))
    unidad.c.subscribe("cit/v1/cit-002/cmd", qos=1)
    time.sleep(0.5)
    marca2 = uuid.uuid4().hex
    api.c.publish("cit/v1/cit-002/cmd", marca2, qos=1).wait_for_publish(5)
    informar("ACL: la unidad NO recibe comandos de otra unidad",
             not unidad.esperar(lambda t, p, r: marca2 in p, espera=2))
    marca3 = uuid.uuid4().hex
    unidad.c.publish(f"{tema}/cmd", marca3, qos=1).wait_for_publish(5)
    informar("ACL: la unidad NO puede mandarse comandos a si misma",
             not unidad.esperar(lambda t, p, r: marca3 in p, espera=2))

    # --- Comando mandado sin senal llega al volver (sesion persistente) ---
    unidad.cerrar()
    time.sleep(0.5)
    cmd2 = dict(cmd, command_id=uuid.uuid4().hex[:8])
    api.c.publish(f"{tema}/cmd", json.dumps(cmd2), qos=1).wait_for_publish(5)
    reinicio = os.environ.get("SIN_REINICIO") != "1"
    if reinicio:
        # Con el comando encolado, se reinicia el broker: prueba la persistencia en disco
        api.cerrar(); ingesta.cerrar()
        subprocess.run(["systemctl", "restart", "mosquitto"], check=True)
        time.sleep(1.5)
        ingesta = Cliente("ingesta-prueba", "ingesta", s["MQTT_INGESTA"], "127.0.0.1", 1883, False, limpia=False)
        ingesta.conectar()
    unidad = Cliente(ID_PRUEBA, UNIDAD, s["MQTT_CIT_000"], HOST, 8883, True, limpia=False,
                     testamento=(f"{tema}/estado", "desconectada"))
    unidad.conectar()
    informar("comando mandado sin senal llega al reconectar"
             + (" (con reinicio del broker de por medio)" if reinicio else ""),
             unidad.esperar(lambda t, p, r: cmd2["command_id"] in p))

    # --- Testamento: corte brusco -> 'desconectada' retenido ---
    unidad.c.publish(f"{tema}/estado", "en linea", qos=1, retain=True).wait_for_publish(5)
    time.sleep(0.5)
    ingesta.recibidos.clear()
    unidad.c.loop_stop()
    unidad.c.socket().close()          # sin DISCONNECT: como perder la senal
    informar("corte brusco publica 'desconectada' (testamento)",
             ingesta.esperar(lambda t, p, r: t == f"{tema}/estado" and p == "desconectada", espera=12))
    ingesta.cerrar()
    # Borra la sesion persistente de prueba: si no, el broker le seguiria
    # guardando telemetria real (hasta 1000 mensajes) para nadie.
    borrar = Cliente("ingesta-prueba", "ingesta", s["MQTT_INGESTA"], "127.0.0.1", 1883, False, limpia=True)
    if borrar.conectar():
        borrar.cerrar()

    # --- Rechazos de autenticacion ---
    mala = Cliente("intruso", UNIDAD, "contrasena-mala", HOST, 8883, True)
    informar("contrasena incorrecta se rechaza", not mala.conectar(), f"rc={mala.rc}")
    mala.c.loop_stop()
    anon = Cliente("anonimo", None, None, HOST, 8883, True)
    informar("conexion anonima se rechaza", not anon.conectar(), f"rc={anon.rc}")
    anon.c.loop_stop()
    plano = Cliente("plano", UNIDAD, s["MQTT_CIT_000"], HOST, 8883, False)
    informar("sin TLS en 8883 no conecta", not plano.conectar(espera=3))
    plano.c.loop_stop()

    # Limpia el retenido de prueba (el agente real lo repone al instante) y,
    # con sesion limpia, borra la sesion persistente de prueba
    limpiar = Cliente(ID_PRUEBA, UNIDAD, s["MQTT_CIT_000"], HOST, 8883, True, limpia=True)
    if limpiar.conectar():
        limpiar.c.publish(f"{tema}/estado", b"", qos=1, retain=True).wait_for_publish(5)
        limpiar.cerrar()


# --- SRT / MediaMTX ------------------------------------------------------------

def estado_path(nombre):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:9997/v3/paths/get/{nombre}", timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def publicar_srt(path, passphrase, segundos):
    url = f"srt://127.0.0.1:8890?streamid=publish:{path}&pkt_size=1316"
    if passphrase:
        url += f"&passphrase={passphrase}"
    # Igual que el contador: H.264 640x480 a 8 c/s en MPEG-TS
    return subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", f"testsrc=size=640x480:rate=8:duration={segundos}",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "16",
         "-b:v", "400k", "-f", "mpegts", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def prueba_srt(s):
    print("\n== SRT / MediaMTX ==")
    pub = publicar_srt(UNIDAD, s["SRT_CIT_000"], 12)
    listo = False
    for _ in range(40):
        e = estado_path(UNIDAD)
        if e and e.get("ready"):
            listo = True
            break
        time.sleep(0.25)
    informar("la unidad publica con su passphrase y el path queda listo", listo)
    if listo:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height", "-of", "csv=p=0",
             f"srt://127.0.0.1:8890?streamid=read:{UNIDAD}"],
            capture_output=True, text=True, timeout=20)
        salida = (r.stdout.strip().splitlines() or [""])[0]
        informar("el flujo se puede leer desde la VM", salida.startswith("h264,640,480"),
                 salida or r.stderr.strip()[-120:])
    pub.terminate(); pub.wait()

    time.sleep(1)
    mal = publicar_srt(UNIDAD, "passphrase-equivocada", 4)
    time.sleep(3)
    e = estado_path(UNIDAD)
    informar("passphrase incorrecta no publica", not (e and e.get("ready")))
    mal.terminate(); mal.wait()

    ajeno = publicar_srt("cit-999", s["SRT_CIT_000"], 4)
    time.sleep(3)
    e = estado_path("cit-999")
    informar("un path no dado de alta no publica", not (e and e.get("ready")))
    ajeno.terminate(); ajeno.wait()


def limpiar_bd():
    """Lo que la ingesta guardo de la unidad ficticia no es un dato real."""
    import sqlite3
    ruta = os.environ.get("CIT_DB", "/var/lib/cit-vm/cit.db")
    if os.path.exists(ruta):
        c = sqlite3.connect(ruta, timeout=15)
        for t in ("telemetria", "resultados", "comandos", "unidades"):
            c.execute(f"DELETE FROM {t} WHERE device_id=?", (UNIDAD,))
        c.commit()


if __name__ == "__main__":
    secretos = leer_secretos()
    prueba_mqtt(secretos)
    prueba_srt(secretos)
    limpiar_bd()
    bien = sum(resultados)
    print(f"\n{bien}/{len(resultados)} pruebas correctas")
    sys.exit(0 if bien == len(resultados) else 1)
