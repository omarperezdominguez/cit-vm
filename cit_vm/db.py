# =============================================================================
# cit_vm/db.py -- base de datos de Edge en la VM (SQLite)
#
# Un solo archivo: /var/lib/cit-vm/cit.db (CIT_DB lo mueve). Lo escriben la
# ingesta (telemetria, resultados, estado) y la API (comandos emitidos).
# WAL permite que ambos escriban y lean a la vez sin bloquearse.
#
# Regla de conservacion (prueba): todo lo que tenga mas de RETENCION_DIAS se
# borra. La hace la ingesta cada hora (borrar_viejos).
# =============================================================================
import os
import sqlite3
import time

RUTA = os.environ.get("CIT_DB", "/var/lib/cit-vm/cit.db")
RETENCION_DIAS = float(os.environ.get("CIT_RETENCION_DIAS", "7"))

ESQUEMA = """
-- Una fila por muestra de la tabla 3 del ADR. sample_id deduplica los
-- reenvios (la Jetson garantiza al menos una entrega, no exactamente una).
CREATE TABLE IF NOT EXISTS telemetria (
    sample_id    TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    vehicle_id   TEXT,
    ts_device    TEXT NOT NULL,          -- ISO UTC de la Jetson: el tiempo que vale
    recibido     REAL NOT NULL,          -- epoch de llegada: solo para la retencion
    lat          REAL,
    lon          REAL,
    speed_kph    REAL,
    heading_deg  REAL,
    gnss_fix     TEXT,
    pax_in       INTEGER,
    pax_out      INTEGER,
    pax_onboard  INTEGER,
    buffered     INTEGER,
    json         TEXT NOT NULL           -- la muestra completa, tal cual llego
);
CREATE INDEX IF NOT EXISTS telemetria_disp_ts ON telemetria(device_id, ts_device);
CREATE INDEX IF NOT EXISTS telemetria_recibido ON telemetria(recibido);

-- Cada orden emitida por la API y su ultimo estado conocido.
CREATE TABLE IF NOT EXISTS comandos (
    command_id   TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    type         TEXT NOT NULL,
    issued_by    TEXT NOT NULL,
    ts_issued    TEXT NOT NULL,
    ttl_s        INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    params       TEXT,
    status       TEXT NOT NULL,          -- pendiente | en_curso | aplicada | ...
    detail       TEXT,
    data         TEXT,
    ts_applied   TEXT,
    actualizado  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS comandos_disp ON comandos(device_id, ts_issued);
CREATE INDEX IF NOT EXISTS comandos_actualizado ON comandos(actualizado);

-- Cada mensaje de resultado, deduplicado por (command_id, status).
CREATE TABLE IF NOT EXISTS resultados (
    command_id   TEXT NOT NULL,
    status       TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    recibido     REAL NOT NULL,
    json         TEXT NOT NULL,
    PRIMARY KEY (command_id, status)
);
CREATE INDEX IF NOT EXISTS resultados_recibido ON resultados(recibido);

-- Ultimo estado conocido de cada unidad (una fila por unidad; no se borra).
CREATE TABLE IF NOT EXISTS unidades (
    device_id         TEXT PRIMARY KEY,
    conexion          TEXT,              -- 'en linea' | 'desconectada'
    ts_conexion       REAL,
    ultima_muestra    TEXT,              -- json de la muestra mas reciente por ts_device
    ts_ultima_muestra TEXT,
    ultimo_recibido   REAL
);
"""


def conectar():
    """Conexion lista para usar. Cada hilo debe abrir la suya."""
    os.makedirs(os.path.dirname(RUTA), exist_ok=True)
    c = sqlite3.connect(RUTA, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    # NORMAL con WAL: un corte de luz puede perder la ultima transaccion, pero
    # la ingesta solo confirma (PUBACK) despues del commit, asi que el broker
    # la vuelve a entregar.
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript(ESQUEMA)
    return c


def borrar_viejos(c, dias=RETENCION_DIAS):
    """Borra lo que llego hace mas de `dias`. Devuelve filas borradas por tabla.

    Se usa la hora de llegada y no ts_device: un reloj de la Jetson desfasado
    no debe hacer que se borren datos recien llegados.
    """
    limite = time.time() - dias * 86400
    borradas = {}
    for tabla, columna in (("telemetria", "recibido"), ("resultados", "recibido"),
                           ("comandos", "actualizado")):
        cur = c.execute(f"DELETE FROM {tabla} WHERE {columna} < ?", (limite,))
        borradas[tabla] = cur.rowcount
    # El espacio liberado se reutiliza; no hace falta VACUUM (bloquearia la BD).
    return borradas
