"""
app.py - Servidor web de la herramienta SPHEREx

  GET  /                    pagina (mapa + "Explora el cielo")
  POST /api/jobs            pide un GIF   (devuelve un id)
  GET  /api/jobs/<id>       estado, registro de progreso y URL del GIF
  GET  /cache/<archivo>     GIFs generados (se guardan: el mismo pedido no se repite)

Local:   uvicorn app:app --reload        (abrir http://localhost:8000)
Publico: ver README.md (Hugging Face Spaces / Render, con el Dockerfile incluido)
"""
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import herramienta_colab as h
import spherex_video as sv

BASE = os.path.dirname(os.path.abspath(__file__))
def _carpeta_cache():
    """Carpeta de cache: CACHE_DIR, o ./cache, o /tmp si el disco es de solo lectura."""
    for c in (os.environ.get("CACHE_DIR"), os.path.join(BASE, "cache"), "/tmp/spherex_cache"):
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            return c
        except OSError:
            continue
    raise RuntimeError("No hay ninguna carpeta escribible para la cache")


CACHE = _carpeta_cache()

# Limites para proteger el servidor (y a IRSA) cuando la web es publica
MAX_SIZE, MAX_FRAMES = 20, 30
MAX_IMGS = int(os.environ.get("MAX_IMGS", 60))      # imagenes por pedido
WORKERS = int(os.environ.get("WORKERS", 2))         # pedidos a la vez (en Render gratis: 1)
MAX_PENDIENTES = 8


class Pedido(BaseModel):
    nombre: Optional[str] = Field(None, max_length=80)
    ra: Optional[str] = Field(None, max_length=40)
    dec: Optional[str] = Field(None, max_length=40)
    size: float = Field(10, ge=3, le=MAX_SIZE)
    banda: str = "SPHEREx-D2"
    frames: int = Field(20, ge=6, le=MAX_FRAMES)
    diff: bool = False
    horizons: Optional[str] = Field(None, max_length=30)
    tipo: str = "smallbody"


# ---- el texto que imprimen los scripts se guarda en el registro de cada trabajo ----
class _Registro:
    def __init__(self):
        self.texto = ""

    def write(self, s):
        self.texto = (self.texto + s)[-4000:]


class _Router(io.TextIOBase):
    def __init__(self, orig):
        self.orig, self.local = orig, threading.local()

    def write(self, s):
        reg = getattr(self.local, "reg", None)
        (reg or self.orig).write(s)
        return len(s)

    def flush(self):
        self.orig.flush()


_router = _Router(sys.stdout)
sys.stdout = _router

JOBS = {}
EJECUTOR = ThreadPoolExecutor(max_workers=WORKERS)
app = FastAPI(title="SPHEREx Sky Explorer")
# Permite que una pagina alojada en OTRO sitio (por ej. Vercel) use esta API.
# En produccion conviene limitarlo: ALLOW_ORIGINS="https://mi-sitio.vercel.app"
app.add_middleware(CORSMiddleware,
                   allow_origins=os.environ.get("ALLOW_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def _clave(p):
    d = p.model_dump() if hasattr(p, "model_dump") else p.dict()
    for k in ("nombre", "ra", "dec", "horizons"):
        d[k] = (d[k] or "").strip().lower()
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def _ruta_cache(clave, diff):
    return os.path.join(CACHE, h._slug(clave) + ("_dif" if diff else "") + ".gif")


def _trabajar(jid, p, clave):
    job = JOBS[jid]
    job["estado"] = "trabajando"
    _router.local.reg = job["reg"]
    try:
        ruta = h.generar(
            nombre=(p.nombre or "").strip() or None,
            ra=(p.ra or "").strip() or None, dec=(p.dec or "").strip() or None,
            size=p.size, banda=p.banda, frames=p.frames, diff=p.diff,
            horizons=(p.horizons or "").strip() or None, tipo=p.tipo,
            max_imgs=MAX_IMGS, carpeta=CACHE, etiqueta=clave)
        job["url"] = "/cache/" + os.path.basename(ruta)
        job["estado"] = "listo"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        job["estado"] = "error"
    finally:
        _router.local.reg = None


@app.post("/api/jobs")
def crear_trabajo(p: Pedido):
    if p.banda not in sv.BANDAS:
        raise HTTPException(422, "Detector invalido (usa D1 a D6).")
    if p.tipo not in ("smallbody", "majorbody", "designation"):
        raise HTTPException(422, "Tipo de ID invalido.")
    if p.horizons and not re.fullmatch(r"[A-Za-z0-9 /_.\-]{1,30}", p.horizons.strip()):
        raise HTTPException(422, "ID de Horizons invalido.")
    if not (p.horizons or (p.nombre and p.nombre.strip()) or (p.ra and p.dec)):
        raise HTTPException(422, "Indica un nombre, o RA y Dec.")
    clave = _clave(p)
    ruta = _ruta_cache(clave, p.diff)
    jid = uuid.uuid4().hex[:12]
    ahora = time.time()
    for k in [k for k, v in JOBS.items() if ahora - v["t0"] > 3600]:   # limpiar viejos
        JOBS.pop(k, None)
    if os.path.exists(ruta):                                            # ya generado antes
        JOBS[jid] = {"estado": "listo", "reg": _Registro(), "t0": ahora,
                     "url": "/cache/" + os.path.basename(ruta), "error": None}
        return {"id": jid, "estado": "listo"}
    if sum(v["estado"] in ("en_cola", "trabajando") for v in JOBS.values()) >= MAX_PENDIENTES:
        raise HTTPException(429, "Hay muchos pedidos en este momento. Proba en unos minutos.")
    JOBS[jid] = {"estado": "en_cola", "reg": _Registro(), "t0": ahora, "url": None, "error": None}
    EJECUTOR.submit(_trabajar, jid, p, clave)
    return {"id": jid, "estado": "en_cola"}


@app.get("/api/jobs/{jid}")
def ver_trabajo(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Pedido no encontrado (puede haber vencido).")
    return {"estado": job["estado"], "log": job["reg"].texto, "url": job["url"],
            "error": job["error"], "segundos": int(time.time() - job["t0"])}


@app.get("/api/salud")
def salud():
    return {"ok": True}


_DIAG = {"t": 0, "res": None}


@app.get("/api/diagnostico")
def diagnostico():
    """Prueba la conexion de ESTE servidor con IRSA (resultado en cache 60 s)."""
    if time.time() - _DIAG["t"] < 60 and _DIAG["res"]:
        return _DIAG["res"]
    import requests
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astroquery.ipac.irsa import Irsa
    res = {}
    t0 = time.time()
    try:
        r = requests.get("https://irsa.ipac.caltech.edu/TAP/capabilities", timeout=20)
        res["TAP_capabilities"] = {"status": r.status_code, "seg": round(time.time() - t0, 1)}
    except Exception as e:
        res["TAP_capabilities"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    c = SkyCoord(269.45 * u.deg, 4.69 * u.deg)
    for col in sv.COLECCIONES:
        t0 = time.time()
        try:
            t = Irsa.query_sia(pos=(c, 1 * u.arcsec), collection=col)
            res[col] = {"filas": len(t), "seg": round(time.time() - t0, 1)}
        except Exception as e:
            res[col] = {"error": f"{type(e).__name__}: {e}"[:160], "seg": round(time.time() - t0, 1)}
    _DIAG["t"], _DIAG["res"] = time.time(), res
    return res


@app.get("/")
def inicio():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
app.mount("/gifs_pre", StaticFiles(directory=os.path.join(BASE, "static", "gifs_pre")), name="gifs_pre")
app.mount("/cache", StaticFiles(directory=CACHE), name="cache")
