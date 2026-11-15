#!/usr/bin/env python3
"""
spherex_video.py  -  NASA Space Apps: "video" del cielo con datos de SPHEREx

Que hace (todo automatico):
  1. Convierte un nombre (o RA/Dec) en coordenadas.
  2. Le pregunta al archivo IRSA que imagenes de SPHEREx cubren ese punto.
  3. Descarga SOLO un recorte pequeno de cada imagen (por defecto 10 arcmin).
  4. Alinea todos los recortes (reproyeccion) para que las estrellas no "salten".
  5. Genera: un GIF animado + una pagina HTML con barra de tiempo (index.html).

Instalacion:
    pip install numpy astropy astroquery reproject pillow

Ejemplos:
    python spherex_video.py --name "Barnard's Star"
    python spherex_video.py --ra 133.7951 --dec -7.2452 --size 15 --band SPHEREx-D3
    python spherex_video.py --coords "08h55m10.831s -07d14m42.53s" --diff
    python spherex_video.py --selftest        (prueba sin internet, con datos falsos)
"""
import argparse
import base64
import concurrent.futures
import io
import json
import os
import re
import sys
import time

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
from astropy.utils.data import conf
from PIL import Image, ImageDraw

conf.remote_timeout = 120
PIXEL_SCALE_ARCSEC = 6.2   # tamano de pixel de SPHEREx (aprox.)
BANDAS = ["SPHEREx-D1", "SPHEREx-D2", "SPHEREx-D3",
          "SPHEREx-D4", "SPHEREx-D5", "SPHEREx-D6"]



def consultar_tap(query, timeout=420, intentos=2):
    """Consulta TAP a IRSA en modo ASINCRONO: se manda la consulta, se espera mostrando
    el estado cada 15 s y, si pasa 'timeout' segundos, se cancela y se reintenta.
    (El modo sincrono puede quedarse colgado varios minutos sin mostrar nada.)"""
    import pyvo
    ultimo = None
    for i in range(intentos):
        job = None
        try:
            svc = pyvo.dal.TAPService("https://irsa.ipac.caltech.edu/TAP")
            job = svc.submit_job(query)
            job.run()
            t0, ult = time.time(), -99
            while True:
                fase, dt = job.phase, time.time() - t0
                if dt - ult >= 15 or fase in ("COMPLETED", "ERROR", "ABORTED"):
                    print(f"  IRSA: {fase} ({dt:.0f} s)")
                    ult = dt
                if fase == "COMPLETED":
                    break
                if fase in ("ERROR", "ABORTED"):
                    raise RuntimeError(f"la consulta termino en estado {fase}")
                if dt > timeout:
                    raise TimeoutError(f"IRSA no respondio en {timeout} s")
                time.sleep(3)
            return job.fetch_result().to_table()
        except Exception as e:
            ultimo = e
            print(f"  (intento {i + 1}/{intentos} fallo: {type(e).__name__}: {e})")
            try:
                if job is not None:
                    job.abort()
            except Exception:
                pass
            time.sleep(5)
    print("  Ultimo recurso: consulta sincrona (puede tardar)...")
    from astroquery.ipac.irsa import Irsa
    try:
        return Irsa.query_tap(query).to_table()
    except Exception:
        raise ultimo


# ----------------------------------------------------------------------
# 1) CONSULTA AL ARCHIVO
# ----------------------------------------------------------------------
COLECCIONES = ["spherex_dr1", "spherex_qr3", "spherex_dr1_deep", "spherex_qr3_deep"]


def consultar_sia(coord, radio, tiempo=None, intentos=3):
    """Busca imagenes con SIA2 (servicio liviano de IRSA, no usa la cola de TAP)."""
    from astroquery.ipac.irsa import Irsa
    ultimo = None
    for i in range(intentos):
        try:
            kw = {"time": tiempo} if tiempo is not None else {}
            return Irsa.query_sia(pos=(coord, radio), collection=COLECCIONES, **kw)
        except Exception as e:
            ultimo = e
            time.sleep(3 * (i + 1))
    raise ultimo


def filas_sia(tabla, det, ra, dec, size_deg, centro=None, tmin=None, tmax=None):
    """Convierte la tabla de SIA en filas {uri, time_bounds_lower} para descargar recortes
    del detector 'det'. 'centro(mjd)' da (ra, dec) del recorte (objetos que se mueven)."""
    vistos, filas = set(), []
    for r in tabla:
        url = str(r["access_url"])
        mjd = float(r["t_min"])
        if tmin is not None and not (tmin - 0.01 <= mjd <= tmax + 0.01):
            continue
        m = re.search(r"_2D(\d)_", url)
        ok = (int(m.group(1)) == det) if m else str(r["energy_bandpassname"]).endswith(str(det))
        if not ok or url in vistos:
            continue
        vistos.add(url)
        c = centro(mjd) if centro else (ra, dec)
        filas.append({"uri": f"{url}?center={c[0]:.6f},{c[1]:.6f}&size={size_deg}",
                      "time_bounds_lower": mjd})
    filas.sort(key=lambda f: f["time_bounds_lower"])
    return filas


def buscar_imagenes_tap(coord, size_deg, banda):
    """Respaldo: misma busqueda por TAP (mas pesado; puede quedar en cola)."""
    query = f"""
    SELECT
        'https://irsa.ipac.caltech.edu/' || a.uri
            || '?center={coord.ra.deg},{coord.dec.deg}d&size={size_deg}' AS uri,
        p.time_bounds_lower
    FROM spherex.artifact a
    JOIN spherex.plane p ON a.planeid = p.planeid
    WHERE 1 = CONTAINS(POINT('ICRS', {coord.ra.deg}, {coord.dec.deg}), p.poly)
      AND p.energy_bandpassname = '{banda}'
    """
    res = consultar_tap(query)
    res.sort("time_bounds_lower")
    return list(res)


def buscar_imagenes(coord, size_deg, banda):
    """Imagenes de SPHEREx que cubren 'coord' (detector segun 'banda', ej SPHEREx-D2)."""
    det = int(banda[-1])
    print("  Consultando IRSA (SIA, deberia tardar unos segundos)...")
    t0 = time.time()
    try:
        tabla = consultar_sia(coord, 1 * u.arcsec)
        filas = filas_sia(tabla, det, coord.ra.deg, coord.dec.deg, size_deg)
    except Exception as e:
        print(f"  SIA fallo ({type(e).__name__}: {e}). Pruebo con TAP...")
        filas = buscar_imagenes_tap(coord, size_deg, banda)
    print(f"  Consulta lista en {time.time() - t0:.1f} s. Imagenes encontradas: {len(filas)}")
    return filas


# ----------------------------------------------------------------------
# 2) DESCARGA (en paralelo, con reintentos)
# ----------------------------------------------------------------------
def bajar_uno(fila, reintentos=3):
    for intento in range(reintentos):
        try:
            with fits.open(fila["uri"], cache=False) as h:
                # h[1]=IMAGE, h[2]=FLAGS, ... (ver tutorial de IRSA)
                return {
                    "data": h[1].data.astype("float64").copy(),
                    "header": h[1].header.copy(),
                    "mjd": float(fila["time_bounds_lower"]),
                    "url": str(fila["uri"]),
                }
        except Exception as e:  # error transitorio: esperamos y reintentamos
            time.sleep(1.5 * (intento + 1))
            ultimo = e
    print(f"  (se salta un cuadro: {type(ultimo).__name__})")
    return None


def descargar(tabla, max_images, workers=8):
    filas = list(tabla)[:max_images] if max_images else list(tabla)
    print(f"  Descargando {len(filas)} recortes...")
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(bajar_uno, f) for f in filas]
        for k, fu in enumerate(concurrent.futures.as_completed(futs), 1):
            out.append(fu.result())
            if k % 10 == 0 or k == len(futs):
                print(f"    {k}/{len(futs)} listos")
    cuadros = [c for c in out if c is not None]
    cuadros.sort(key=lambda c: c["mjd"])
    print(f"  Descargados OK: {len(cuadros)}")
    return cuadros


# ----------------------------------------------------------------------
# 3) ALINEAR (reproyectar) todos los cuadros a una misma grilla
# ----------------------------------------------------------------------
def crear_wcs_comun(coord, size_arcmin):
    n = int(round(size_arcmin * 60 / PIXEL_SCALE_ARCSEC))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [coord.ra.deg, coord.dec.deg]
    w.wcs.crpix = [n / 2 + 0.5, n / 2 + 0.5]
    s = PIXEL_SCALE_ARCSEC / 3600.0
    w.wcs.cdelt = [-s, s]            # norte arriba, este a la izquierda
    return w, (n, n)


def alinear(cuadros, coord, size_arcmin):
    from reproject import reproject_interp

    wcs_out, shape = crear_wcs_comun(coord, size_arcmin)
    pila, fechas = [], []
    for c in cuadros:
        try:
            wcs_in = WCS(c["header"])
            reproj, _ = reproject_interp((c["data"], wcs_in), wcs_out,
                                         shape_out=shape)
            if np.isfinite(reproj).sum() < 0.5 * reproj.size:
                continue            # cuadro casi vacio en el centro: lo ignoramos
            pila.append(reproj)
            fechas.append(c["mjd"])
        except Exception as e:
            print(f"  (no se pudo alinear un cuadro: {type(e).__name__})")
    return np.array(pila), np.array(fechas)


# ----------------------------------------------------------------------
# 4) IMAGENES -> PNG / GIF / HTML
# ----------------------------------------------------------------------
def normalizar(pila):
    """Resta el fondo de cada cuadro y aplica un estiramiento asinh comun."""
    pila = pila.copy()
    for i in range(len(pila)):
        med = np.nanmedian(pila[i])
        pila[i] -= med
    lo, hi = np.nanpercentile(pila, [1, 99.7])
    hi = max(hi, lo + 1e-9)
    esc = np.clip((pila - lo) / (hi - lo), 0, 1)
    esc = np.arcsinh(10 * esc) / np.arcsinh(10)
    return np.nan_to_num(esc, nan=0.0)


def a_pil(arr01, texto, escala=4):
    img = Image.fromarray((arr01[::-1] * 255).astype("uint8"))  # norte arriba
    img = img.resize((img.width * escala, img.height * escala), Image.BICUBIC)
    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((6, 6), texto, fill=(255, 255, 0))
    # marca pequena en el centro (para ubicar el objetivo)
    cx, cy = img.width // 2, img.height // 2
    for dx, dy in [(-25, 0), (25, 0), (0, -25), (0, 25)]:
        d.line([(cx + dx * 0.6, cy + dy * 0.6), (cx + dx, cy + dy)],
               fill=(0, 255, 255), width=1)
    return img


HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>SPHEREx - __TITULO__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{background:#0b0f1a;color:#e8ecf5;font-family:system-ui,sans-serif;margin:0;padding:16px;text-align:center}
 img{max-width:100%;width:560px;image-rendering:auto;border:1px solid #334}
 .row{margin:10px auto;max-width:560px;display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap}
 input[type=range]{flex:1;min-width:160px}
 button,select{background:#1d2a4a;color:#fff;border:1px solid #456;border-radius:6px;padding:6px 12px;font-size:15px}
 small{color:#9ab}
</style></head><body>
<h2>__TITULO__</h2>
<p><small>Campo: __SIZE__ arcmin &middot; Banda: __BANDA__ &middot; Norte arriba, Este a la izquierda.<br>
Los objetos que se mueven cambian de posicion entre cuadros.</small></p>
<img id="v" alt="cuadro">
<div class="row"><button id="p">&#9654; / &#10074;&#10074;</button>
<input id="s" type="range" min="0" value="0">
<span id="f"></span></div>
<div class="row">Velocidad <select id="sp"><option value="1200">lenta</option>
<option value="600" selected>normal</option><option value="250">rapida</option></select>
&nbsp; Modo <select id="m"><option value="0">Normal</option><option value="1">Restar mediana</option></select></div>
<script>
const D=__DATA__;let i=0,play=true,t=null;
const v=document.getElementById('v'),s=document.getElementById('s'),
f=document.getElementById('f'),sp=document.getElementById('sp'),m=document.getElementById('m');
function arr(){return m.value==="1"&&D.diff.length?D.diff:D.norm}
function show(k){i=k;v.src="data:image/png;base64,"+arr()[i];s.value=i;f.textContent=D.fechas[i]+" ("+(i+1)+"/"+D.norm.length+")"}
function loop(){clearInterval(t);if(play)t=setInterval(()=>show((i+1)%D.norm.length),+sp.value)}
s.max=D.norm.length-1;s.oninput=()=>{play=false;loop();show(+s.value)};
document.getElementById('p').onclick=()=>{play=!play;loop()};
sp.onchange=loop;m.onchange=()=>show(i);
show(0);loop();
</script></body></html>"""


def png_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def generar_salidas(pila, mjds, titulo, banda, size_arcmin, carpeta, con_diff):
    os.makedirs(carpeta, exist_ok=True)
    fechas = [Time(m, format="mjd").iso[:10] for m in mjds]

    norm = normalizar(pila)
    imgs = [a_pil(norm[i], f"{fechas[i]}  #{i + 1}") for i in range(len(norm))]

    # Modo "restar mediana": deja ver solo lo que cambia (objetos en movimiento)
    imgs_diff = []
    if con_diff or True:
        mediana = np.nanmedian(pila, axis=0)
        dif = pila - mediana
        for i in range(len(dif)):
            dif[i] -= np.nanmedian(dif[i])
        lim = np.nanpercentile(np.abs(dif), 99.5)
        lim = max(lim, 1e-9)
        d01 = np.nan_to_num(np.clip(dif / (2 * lim) + 0.5, 0, 1), nan=0.5)
        imgs_diff = [a_pil(d01[i], f"{fechas[i]}  (diferencia)")
                     for i in range(len(d01))]

    # GIF
    gif_path = os.path.join(carpeta, "video.gif")
    imgs[0].save(gif_path, save_all=True, append_images=imgs[1:],
                 duration=500, loop=0)
    if imgs_diff:
        imgs_diff[0].save(os.path.join(carpeta, "video_diferencia.gif"),
                          save_all=True, append_images=imgs_diff[1:],
                          duration=500, loop=0)

    # PNG sueltos
    png_dir = os.path.join(carpeta, "cuadros")
    os.makedirs(png_dir, exist_ok=True)
    for i, im in enumerate(imgs):
        im.save(os.path.join(png_dir, f"cuadro_{i:03d}.png"))

    # HTML con todo embebido (se abre con doble clic, sin servidor)
    data = {"norm": [png_b64(i) for i in imgs],
            "diff": [png_b64(i) for i in imgs_diff],
            "fechas": fechas}
    html = (HTML.replace("__TITULO__", titulo).replace("__BANDA__", banda)
                .replace("__SIZE__", f"{size_arcmin:g}")
                .replace("__DATA__", json.dumps(data)))
    html_path = os.path.join(carpeta, "index.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return gif_path, html_path


# ----------------------------------------------------------------------
# AUTOPRUEBA (sin internet): cuadros falsos con un objeto que se mueve
# ----------------------------------------------------------------------
def cuadros_falsos(coord, n=12, size_arcmin=10):
    rng = np.random.default_rng(1)
    wcs_true, shape = crear_wcs_comun(coord, size_arcmin)
    cuadros = []
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    for k in range(n):
        img = rng.normal(100, 2, shape)
        for (x0, y0) in [(20, 25), (70, 60), (40, 80)]:       # estrellas fijas
            img += 300 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / 4.0)
        x, y = 30 + 3.5 * k, 50 + 1.5 * k                        # objeto movil
        img += 250 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 4.0)
        cuadros.append({"data": img, "header": wcs_true.to_header(),
                        "mjd": 60800.0 + 14 * k, "url": "falso"})
    return cuadros


# ----------------------------------------------------------------------
# PROGRAMA PRINCIPAL
# ----------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Video del cielo con SPHEREx")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--name", help='Nombre del objeto, ej: "Barnard\'s Star"')
    g.add_argument("--coords", help='ej: "08h55m10.831s -07d14m42.53s"')
    ap.add_argument("--ra", type=float, help="RA en grados")
    ap.add_argument("--dec", type=float, help="Dec en grados")
    ap.add_argument("--size", type=float, default=10.0,
                    help="Campo de vision en arcmin (default 10)")
    ap.add_argument("--band", default="SPHEREx-D2", choices=BANDAS,
                    help="Detector D1..D6 (default SPHEREx-D2)")
    ap.add_argument("--max-images", type=int, default=60)
    ap.add_argument("--out", default=None, help="Carpeta de salida")
    ap.add_argument("--diff", action="store_true",
                    help="(reservado) siempre se genera el modo diferencia")
    ap.add_argument("--selftest", action="store_true")
    # parse_known_args: ignora argumentos extra que mete Jupyter/Colab (-f ...)
    a, _ = ap.parse_known_args(argv)

    # --- coordenadas ---
    if a.selftest:
        coord = SkyCoord(ra=210.8 * u.deg, dec=54.3 * u.deg)
        titulo = "AUTOPRUEBA (datos falsos)"
    elif a.name:
        print(f"Buscando coordenadas de '{a.name}'...")
        coord = SkyCoord.from_name(a.name)
        titulo = a.name
    elif a.coords:
        coord = SkyCoord(a.coords, frame="icrs")
        titulo = a.coords
    elif a.ra is not None and a.dec is not None:
        coord = SkyCoord(ra=a.ra * u.deg, dec=a.dec * u.deg)
        titulo = f"RA {a.ra:.4f}  Dec {a.dec:.4f}"
    else:
        # Sin argumentos (por ejemplo al pegar el codigo en una celda de Colab):
        # usamos la Estrella de Barnard como ejemplo en vez de dar error.
        print("No indicaste objeto: uso 'Barnard's Star' como ejemplo.")
        coord = SkyCoord.from_name("Barnard's Star")
        titulo = "Barnard's Star"

    print(f"Coordenadas: RA={coord.ra.deg:.5f}  Dec={coord.dec.deg:.5f}")
    size_deg = (a.size * u.arcmin).to(u.deg).value
    carpeta = a.out or ("salida_selftest" if a.selftest else
                        "salida_" + "".join(ch if ch.isalnum() else "_"
                                            for ch in titulo)[:40])

    # --- datos ---
    if a.selftest:
        cuadros = cuadros_falsos(coord, size_arcmin=a.size)
    else:
        print("1) Buscando imagenes en IRSA...")
        tabla = buscar_imagenes(coord, size_deg, a.band)
        if len(tabla) == 0:
            sys.exit("No hay imagenes de SPHEREx en esa posicion/banda. "
                     "Proba otra banda (--band SPHEREx-D3, etc.).")
        print("2) Descargando recortes...")
        cuadros = descargar(tabla, a.max_images)
        if not cuadros:
            sys.exit("No se pudo descargar ningun cuadro. Revisa tu internet.")

    print("3) Alineando cuadros...")
    pila, mjds = alinear(cuadros, coord, a.size)
    if len(pila) < 2:
        sys.exit("Quedaron menos de 2 cuadros utilizables. "
                 "Proba un campo mas grande (--size 15).")
    print(f"  Cuadros utilizables: {len(pila)}")

    print("4) Generando GIF y pagina web...")
    gif, html = generar_salidas(pila, mjds, titulo, a.band, a.size,
                                carpeta, a.diff)
    print("\nLISTO. Archivos en la carpeta:", os.path.abspath(carpeta))
    print("  - index.html           (abrilo con doble clic)")
    print("  - video.gif / video_diferencia.gif")
    print("  - cuadros/             (PNG sueltos)")


if __name__ == "__main__":
    main()
