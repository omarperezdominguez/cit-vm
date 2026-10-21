// =============================================================================
// Panel de ejemplo para la API de Edge Mobility
//
//   navegador ──HTTP──► este servidor (guarda el TOKEN) ──HTTPS + token──► API de la VM
//
// Por que hay un servidor en medio y no se llama a la API desde el navegador:
//   1. El token da control total de las unidades (incluido APAGARLAS). Si va en
//      el JavaScript de la pagina, cualquiera que la abra puede copiarlo.
//   2. La API no manda cabeceras CORS: el navegador bloquea las llamadas
//      directas desde otro dominio.
//   3. La API usa un certificado de la CA propia de Edge (ca.crt). Un servidor
//      lo puede confiar; un navegador no.
//
// Sin dependencias: Node 18 o mas nuevo.
//   cp .env.example .env    # y llenarlo
//   node server.js          # http://localhost:3000
//
// Este servidor NO es un proxy abierto: solo expone las acciones del panel, y
// el "issued_by" lo pone el servidor, no el navegador.
// =============================================================================
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');

// --- Configuracion (.env) -----------------------------------------------------
function cargarEnv(archivo) {
  if (!fs.existsSync(archivo)) return;
  for (const linea of fs.readFileSync(archivo, 'utf8').split(/\r?\n/)) {
    const m = linea.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
}
cargarEnv(path.join(__dirname, '.env'));

const API_URL = (process.env.EDGE_API_URL || 'https://34.51.107.234:8443').replace(/\/$/, '');
const TOKEN = process.env.EDGE_API_TOKEN || '';
const CA = process.env.EDGE_CA || path.join(__dirname, '..', '..', 'ca.crt');
const PUERTO = Number(process.env.PUERTO || 3000);
const USUARIO = process.env.PANEL_USUARIO || '';     // quien firma las ordenes (issued_by)
const CLAVE = process.env.PANEL_CLAVE || '';         // clave de acceso al panel

if (!TOKEN) { console.error('Falta EDGE_API_TOKEN en .env'); process.exit(1); }
if (!fs.existsSync(CA)) { console.error(`No encuentro el certificado ${CA}`); process.exit(1); }
if (!USUARIO || !CLAVE) {
  console.error('Faltan PANEL_USUARIO y PANEL_CLAVE en .env: sin ellos cualquiera que ' +
                'llegue a este panel podria apagar las unidades.');
  process.exit(1);
}

// Agente HTTPS que confia en la CA de Edge. NUNCA usar rejectUnauthorized:false.
const agente = new https.Agent({ ca: fs.readFileSync(CA), keepAlive: true });

// --- Llamar a la API de Edge ---------------------------------------------------
function api(metodo, ruta, cuerpo) {
  return new Promise((resolver) => {
    const datos = cuerpo ? JSON.stringify(cuerpo) : null;
    const pet = https.request(API_URL + ruta, {
      method: metodo, agent: agente, timeout: 35000,
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        ...(datos ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(datos) } : {}),
      },
    }, (res) => {
      let texto = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { texto += c; });
      res.on('end', () => {
        let json;
        try { json = JSON.parse(texto); } catch { json = { error: 'respuesta no es JSON', texto } }
        resolver({ codigo: res.statusCode, json });
      });
    });
    pet.on('timeout', () => pet.destroy(new Error('la API no respondio a tiempo')));
    pet.on('error', (e) => resolver({ codigo: 502, json: { error: `no se pudo hablar con la API: ${e.message}` } }));
    if (datos) pet.write(datos);
    pet.end();
  });
}

// --- Servidor del panel ----------------------------------------------------------
const RE_UNIDAD = /^cit-[0-9]{3}$/;
const RE_COMANDO = /^[0-9a-f]{6,32}$/;

function responder(res, codigo, cuerpo) {
  const datos = JSON.stringify(cuerpo);
  res.writeHead(codigo, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(datos);
}

function autorizado(req) {
  const h = req.headers.authorization || '';
  if (!h.startsWith('Basic ')) return false;
  const [u, ...resto] = Buffer.from(h.slice(6), 'base64').toString().split(':');
  const c = resto.join(':');
  const igual = (a, b) => a.length === b.length && require('node:crypto').timingSafeEqual(Buffer.from(a), Buffer.from(b));
  return igual(u, USUARIO) && igual(c, CLAVE);
}

function leerCuerpo(req) {
  return new Promise((resolver) => {
    let texto = '';
    req.on('data', (c) => { texto += c; if (texto.length > 10000) req.destroy(); });
    req.on('end', () => { try { resolver(texto ? JSON.parse(texto) : {}); } catch { resolver({}); } });
  });
}

const servidor = http.createServer(async (req, res) => {
  if (!autorizado(req)) {
    res.writeHead(401, { 'WWW-Authenticate': 'Basic realm="Panel Edge", charset="UTF-8"' });
    return res.end('Se necesita usuario y clave');
  }
  const url = new URL(req.url, 'http://panel');
  const p = url.pathname.split('/').filter(Boolean);    // ["api","unidades","cit-001","video"]

  try {
    // Pagina
    if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return fs.createReadStream(path.join(__dirname, 'public', 'index.html')).pipe(res);
    }
    if (p[0] !== 'api') return responder(res, 404, { error: 'no existe' });
    if (p[1] === 'unidades' && p[2] && !RE_UNIDAD.test(p[2])) return responder(res, 400, { error: 'unidad invalida' });

    // GET /api/unidades  ->  estado de todas
    if (req.method === 'GET' && p.length === 2 && p[1] === 'unidades') {
      const r = await api('GET', '/v1/unidades');
      return responder(res, r.codigo, r.json);
    }
    // GET /api/unidades/cit-001/telemetria?limite=50
    if (req.method === 'GET' && p.length === 4 && p[1] === 'unidades' && p[3] === 'telemetria') {
      const limite = Math.min(Math.max(parseInt(url.searchParams.get('limite') || '50', 10) || 50, 1), 1000);
      const r = await api('GET', `/v1/unidades/${p[2]}/telemetria?limite=${limite}`);
      return responder(res, r.codigo, r.json);
    }
    // GET /api/comandos/<id>  ->  como va una orden
    if (req.method === 'GET' && p.length === 3 && p[1] === 'comandos' && RE_COMANDO.test(p[2])) {
      const r = await api('GET', `/v1/comandos/${p[2]}`);
      return responder(res, r.codigo, r.json);
    }
    if (req.method === 'POST' && p.length === 4 && p[1] === 'unidades') {
      const cuerpo = await leerCuerpo(req);
      const motivo = String(cuerpo.reason || '').trim().slice(0, 200) || 'desde el panel';
      // POST /api/unidades/cit-001/video  ->  liga del visor (tarda ~2 s)
      if (p[3] === 'video') {
        const dur = Math.min(Math.max(parseInt(cuerpo.duration_s, 10) || 60, 5), 60);
        const r = await api('POST', `/v1/unidades/${p[2]}/video`,
          { issued_by: USUARIO, reason: motivo, duration_s: dur });
        return responder(res, r.codigo, r.json);
      }
      // POST /api/unidades/cit-001/cerrar-video
      if (p[3] === 'cerrar-video') {
        const r = await api('POST', `/v1/unidades/${p[2]}/comandos?esperar=10`,
          { type: 'media.close', issued_by: USUARIO, reason: motivo });
        return responder(res, r.codigo, r.json);
      }
      // POST /api/unidades/cit-001/apagar  ->  module.shutdown
      // El navegador debe mandar confirmacion: el nombre de la unidad escrito.
      if (p[3] === 'apagar') {
        if (cuerpo.confirmar !== p[2]) {
          return responder(res, 400, { error: `para apagar, confirmar debe ser "${p[2]}"` });
        }
        const r = await api('POST', `/v1/unidades/${p[2]}/comandos?esperar=10`,
          { type: 'module.shutdown', issued_by: USUARIO, reason: motivo });
        return responder(res, r.codigo, r.json);
      }
    }
    return responder(res, 404, { error: 'no existe' });
  } catch (e) {
    console.error(e);
    return responder(res, 500, { error: 'error interno del panel' });
  }
});

servidor.listen(PUERTO, () => {
  console.log(`Panel en http://localhost:${PUERTO}  ->  ${API_URL}`);
});
