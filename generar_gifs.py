#!/usr/bin/env python3
"""
generar_gifs.py  -  GIFs REALES de SPHEREx para los 20 objetos del panel

Necesita estar en la misma carpeta que:
    spherex_video.py        (el script anterior)
    spherex_mapa_mc.html    (el panel)

Instalacion:
    pip install numpy astropy astroquery reproject pillow

Uso:
    python generar_gifs.py                    # los 20 objetos (tarda bastante)
    python generar_gifs.py --solo 8 9 10      # solo algunos (n° del panel)
    python generar_gifs.py --solo-html        # solo rearma el HTML con los GIF ya hechos
    python generar_gifs.py --band SPHEREx-D3  # otro detector

Resultado:
    gifs/01.gif ... gifs/20.gif
    spherex_mapa_REAL.html   (panel con los GIFs reales dentro; abrilo con doble clic)

Los GIFs se guardan de a uno: si se corta, volve a correr y sigue donde quedo.
"""
import argparse
import base64
import json
import os
import re
import sys

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.wcs import WCS

import spherex_video as sv

# n°, nombre, RA, Dec (None = se mueve), campo en arcmin, identificadores de Horizons
OBJETOS = [
    (1, "Andromeda M31", 10.685, 41.269, 20, None),
    (2, "Remolino M51", 202.470, 47.195, 10, None),
    (3, "Centro de la Via Lactea", 266.417, -29.008, 10, None),
    (4, "Nebulosa del Cangrejo M1", 83.633, 22.014, 10, None),
    (5, "Nebulosa del Anillo M57", 283.396, 33.029, 10, None),
    (6, "Mira", 34.837, -2.978, 10, None),
    (7, "Polo norte ecliptico", 270.0, 66.561, 15, None),
    (8, "Estrella de Barnard", 269.45, 4.69, 10, None),
    (9, "Luhman 16", 162.328, -53.319, 10, None),
    (10, "WISE 0855", 133.80, -7.25, 15, None),
    (11, "RW Aurigae", 76.96, 30.40, 10, None),
    (12, "R Draconis", 248.18, 66.76, 10, None),
    (13, "TRAPPIST-1", 346.62, -5.04, 10, None),
    (14, "Beta Pictoris", 86.82, -51.07, 10, None),
    (15, "Proxima Centauri", 217.43, -62.68, 10, None),
    (16, "Cygnus X", 307.5, 41.0, 20, None),
    (17, "Rho Ophiuchi", 246.8, -24.5, 20, None),
    (18, "Orion M42", 83.82, -5.39, 20, None),
    (19, "NGC 1760", 74.1, -66.4, 15, None),
    (20, "Gran Nube de Magallanes", 80.89, -69.76, 20, None),
]
# Movimiento propio (mas/ano en RA*cos(dec) y Dec). Las coordenadas de la lista son de
# la epoca J2000: las estrellas veloces se corrieron desde entonces y hay que "ponerlas
# al dia" para que queden en el centro del recorte.
PM = {8: (-802.8, 10362.5),    # Estrella de Barnard
      15: (-3781.0, 770.0)}    # Proxima Centauri
EPOCA_OBS = 2026.1             # mitad aproximada del periodo de observacion de SPHEREx


def coord_objeto(n, ra, dec):
    """Coordenada del objeto n en la epoca de observacion (corrige movimiento propio)."""
    if n in PM:
        dt = EPOCA_OBS - 2000.0
        pmra, pmdec = PM[n]
        dec = dec + pmdec / 3.6e6 * dt
        ra = ra + pmra / 3.6e6 * dt / np.cos(np.deg2rad(dec))
    return SkyCoord(ra * u.deg, dec * u.deg)


MAX_FRAMES = 24
INICIO = "2025-05-01"   # SPHEREx empieza a observar en mayo de 2025


def nombre_gif(n):
    return os.path.join("gifs", f"{n:02d}.gif")


# ----------------------------------------------------------------------
# Objetos que SE MUEVEN: posicion desde JPL Horizons (dia por dia)
# ----------------------------------------------------------------------
def efemerides(ids):
    from astroquery.jplhorizons import Horizons
    fin = Time.now().iso[:10]
    ultimo = None
    for ident, tipo in ids:
        try:
            obj = Horizons(id=ident, id_type=tipo, location="500",
                           epochs={"start": INICIO, "stop": fin, "step": "1d"})
            e = obj.ephemerides()
            jd = np.array(e["datetime_jd"], dtype=float)
            ra = np.array(e["RA"], dtype=float)
            dec = np.array(e["DEC"], dtype=float)
            print(f"  Horizons OK ({ident}): {len(jd)} dias")
            return jd, ra, dec
        except Exception as ex:
            ultimo = ex
    raise RuntimeError(f"Horizons no encontro el objeto: {ultimo}")


def posicion_en(jd_eph, ra_eph, dec_eph, mjd):
    jd = mjd + 2400000.5
    ra = np.interp(jd, jd_eph, np.unwrap(np.deg2rad(ra_eph)))
    dec = np.interp(jd, jd_eph, dec_eph)
    return float(np.rad2deg(ra) % 360.0), float(dec)


def buscar_movil(jd, ra, dec, banda, size_deg, paso=None, workers=6):
    """Imagenes que contienen al objeto en cada momento (SIA2 por ventanas de tiempo)."""
    import concurrent.futures
    mjd = jd - 2400000.5
    det = int(banda[-1])
    if paso is None:   # ventana mas corta cuanto mas rapido se mueve el objeto
        dra = np.diff(np.unwrap(np.deg2rad(ra))) * np.cos(np.deg2rad(dec[:-1]))
        v = float(np.median(np.hypot(dra, np.diff(np.deg2rad(dec))))) * 180 / np.pi
        paso = int(np.clip(0.5 / max(v, 1e-3), 1, 10))
        print(f"  Se mueve ~{v:.2f} grados/dia -> ventanas de {paso} dia(s)")
    centro = lambda m: posicion_en(jd, ra, dec, m)
    ventanas = list(range(0, len(mjd), paso))

    def una(k):
        k1 = min(k + paso, len(mjd) - 1)
        mid = (k + k1) // 2
        coord = SkyCoord(ra[mid] * u.deg, dec[mid] * u.deg)
        try:
            t = sv.consultar_sia(coord, 1 * u.arcsec,
                                 tiempo=(Time(mjd[k], format="mjd"), Time(mjd[k1], format="mjd")))
        except Exception:
            return []
        return sv.filas_sia(t, det, None, None, size_deg, centro=centro,
                            tmin=mjd[k], tmax=mjd[k1])

    vistos, filas = set(), []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, lote in enumerate(ex.map(una, ventanas), 1):
            for f in lote:
                if f["uri"] not in vistos:
                    vistos.add(f["uri"]); filas.append(f)
            if i % 25 == 0 or i == len(ventanas):
                print(f"    ventanas consultadas: {i}/{len(ventanas)} (imagenes: {len(filas)})")
    filas.sort(key=lambda f: f["time_bounds_lower"])
    print(f"  Imagenes que cubren al objeto: {len(filas)}")
    return filas


# ----------------------------------------------------------------------
# Convertir cuadros en GIF
# ----------------------------------------------------------------------
def guardar_gif(pila, mjds, size_arcmin, ruta):
    if len(pila) > MAX_FRAMES:
        idx = np.linspace(0, len(pila) - 1, MAX_FRAMES).astype(int)
        pila, mjds = pila[idx], mjds[idx]
    norm = sv.normalizar(pila)
    n = max(1, round(size_arcmin * 60 / sv.PIXEL_SCALE_ARCSEC))
    escala = max(1, round(200 / n))
    imgs = [sv.a_pil(norm[i], Time(mjds[i], format="mjd").iso[:10], escala=escala)
            for i in range(len(norm))]
    imgs = [im.quantize(colors=48) for im in imgs]   # menos colores = GIF mas liviano
    os.makedirs("gifs", exist_ok=True)
    imgs[0].save(ruta, save_all=True, append_images=imgs[1:], duration=450, loop=0, optimize=True)
    print(f"  GIF guardado: {ruta} ({len(imgs)} cuadros)")


def alinear_movil(cuadros, size_arcmin, jd, ra, dec):
    """Cada cuadro se centra en la posicion del objeto en ese momento."""
    from reproject import reproject_interp
    pila, mjds = [], []
    for c in cuadros:
        try:
            r, d = posicion_en(jd, ra, dec, c["mjd"])
            wcs_out, shape = sv.crear_wcs_comun(SkyCoord(r * u.deg, d * u.deg), size_arcmin)
            rep, _ = reproject_interp((c["data"], WCS(c["header"])), wcs_out, shape_out=shape)
            if np.isfinite(rep).sum() >= 0.5 * rep.size:
                pila.append(rep)
                mjds.append(c["mjd"])
        except Exception as ex:
            print(f"  (no se pudo alinear un cuadro: {type(ex).__name__})")
    return np.array(pila), np.array(mjds)


def procesar(obj, banda):
    n, nombre, ra, dec, size, ids = obj
    print(f"\n[{n}/20] {nombre}")
    size_deg = (size * u.arcmin).to(u.deg).value
    if ids is None:                                   # objeto fijo
        coord = coord_objeto(n, ra, dec)
        tabla = sv.buscar_imagenes(coord, size_deg, banda)
        if len(tabla) == 0:
            print("  Sin imagenes en esa posicion/banda."); return False
        cuadros = sv.descargar(tabla, 80)
        pila, mjds = sv.alinear(cuadros, coord, size)
        mjds = np.array(mjds)
    else:                                              # objeto movil
        jd, ra_e, dec_e = efemerides(ids)
        filas = buscar_movil(jd, ra_e, dec_e, banda, size_deg)
        if not filas:
            print("  Sin imagenes para este objeto."); return False
        cuadros = sv.descargar(filas, 80)
        pila, mjds = alinear_movil(cuadros, size, jd, ra_e, dec_e)
    if len(pila) < 2:
        print("  Quedaron menos de 2 cuadros utilizables."); return False
    guardar_gif(pila, mjds, size, nombre_gif(n))
    return True


# ----------------------------------------------------------------------
# Armar el HTML con los GIF adentro
# ----------------------------------------------------------------------
def construir_html(plantilla, salida):
    with open(plantilla, encoding="utf-8") as f:
        html = f.read()
    gifs = {}
    import glob
    for n in range(1, 21):
        # busca gifs/NN.gif o gifs/<carpeta>/NN_nombre.gif (sin los "_dif")
        cand = [r for r in sorted(glob.glob(os.path.join("gifs", "**", f"{n:02d}*.gif"), recursive=True))
                if "_dif" not in os.path.basename(r)]
        if cand:
            ruta = cand[0]
            with open(ruta, "rb") as f:
                gifs[n] = "data:image/gif;base64," + base64.b64encode(f.read()).decode()
    nuevo, k = re.subn(r"const REAL_GIFS=\{\};", lambda m: "const REAL_GIFS=" + json.dumps(gifs) + ";", html, count=1)
    if k != 1:
        sys.exit("No encontre 'const REAL_GIFS={};' en la plantilla HTML.")
    with open(salida, "w", encoding="utf-8") as f:
        f.write(nuevo)
    faltan = [n for n in range(1, 21) if n not in gifs]
    print(f"\nHTML listo: {salida}  ({len(gifs)} GIF reales)")
    if faltan:
        print("Sin GIF real (siguen con animacion ilustrativa):", faltan)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", type=int, nargs="*", help="n° de objetos a procesar")
    ap.add_argument("--band", default="SPHEREx-D2", choices=sv.BANDAS)
    ap.add_argument("--html", default="spherex_mapa_mc.html")
    ap.add_argument("--out", default="spherex_mapa_REAL.html")
    ap.add_argument("--solo-html", action="store_true")
    ap.add_argument("--rehacer", action="store_true", help="regenerar aunque exista el GIF")
    a, _ = ap.parse_known_args(argv)

    if not a.solo_html:
        for obj in OBJETOS:
            if a.solo and obj[0] not in a.solo:
                continue
            if os.path.exists(nombre_gif(obj[0])) and not a.rehacer:
                print(f"[{obj[0]}/20] {obj[1]}: ya existe, se salta"); continue
            try:
                procesar(obj, a.band)
            except Exception as ex:
                print(f"  ERROR en {obj[1]}: {type(ex).__name__}: {ex}")
    construir_html(a.html, a.out)


if __name__ == "__main__":
    main()
