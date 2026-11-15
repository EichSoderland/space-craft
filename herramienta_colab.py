#!/usr/bin/env python3
"""
herramienta_colab.py  -  Panel interactivo para Google Colab

Escribis un nombre o unas coordenadas, apretas "Generar" y el script:
  1. busca las imagenes de SPHEREx que cubren ese punto,
  2. baja solo un recorte chico de cada una,
  3. las alinea, arma el GIF y lo MUESTRA ahi mismo (y lo guarda en gifs_manual/).

Tambien sirve para objetos que SE MUEVEN (planetas, asteroides, cometas): tildas
"se mueve" y pones su identificador de JPL Horizons.

Uso (en una celda de Colab):
    import herramienta_colab as h
    h.interfaz()

Sin botones (una sola llamada):
    h.generar(nombre="Barnard's Star")
    h.generar(ra="08h55m10.831s", dec="-07d14m42.53s", size=15)
    h.generar(nombre="Eros", horizons="433")

Necesita en la misma carpeta: spherex_video.py y generar_gifs.py
Instalacion: pip install numpy astropy astroquery reproject pillow ipywidgets
"""
import os
import re

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord

import spherex_video as sv
import generar_gifs as g


def _slug(t):
    return re.sub(r"[^a-z0-9]+", "_", str(t).lower()).strip("_") or "objeto"


def parse_coord(ra, dec):
    """Acepta grados (133.79, -7.24) o sexagesimal ('08h55m10.8s', '-07d14m42s')."""
    ra, dec = str(ra).strip(), str(dec).strip()
    try:
        return SkyCoord(float(ra) * u.deg, float(dec) * u.deg)
    except ValueError:
        return SkyCoord(ra, dec, unit=(u.hourangle, u.deg))


def generar(nombre=None, ra=None, dec=None, size=10, banda="SPHEREx-D2", frames=20,
            diff=False, horizons=None, tipo="smallbody", max_imgs=80,
            carpeta="gifs_manual", etiqueta=None):
    """Baja los recortes, arma el GIF y devuelve la ruta del archivo."""
    size_deg = (size * u.arcmin).to(u.deg).value
    if horizons:                                   # ---- objeto que se mueve
        print(f"Objeto en movimiento (Horizons: {horizons})")
        jd, ra_e, dec_e = g.efemerides([(horizons, tipo)])
        filas = g.buscar_movil(jd, ra_e, dec_e, banda, size_deg)
        if not filas:
            raise RuntimeError("No hay imagenes de SPHEREx para este objeto/banda.")
        cuadros = sv.descargar(filas, max_imgs)
        pila, mjds = g.alinear_movil(cuadros, size, jd, ra_e, dec_e)
        titulo = etiqueta or nombre or horizons
    else:                                          # ---- objeto fijo
        if nombre and not (ra and dec):
            print(f"Buscando coordenadas de '{nombre}' en SIMBAD...")
            coord = SkyCoord.from_name(nombre)
        elif ra and dec:
            coord = parse_coord(ra, dec)
        else:
            raise ValueError("Indica un nombre, o RA y Dec.")
        print(f"Coordenadas: RA={coord.ra.deg:.5f}  Dec={coord.dec.deg:.5f}")
        tabla = sv.buscar_imagenes(coord, size_deg, banda)
        if len(tabla) == 0:
            raise RuntimeError("No hay imagenes de SPHEREx en ese punto/banda. "
                               "Proba otra banda (D1 a D6).")
        cuadros = sv.descargar(tabla, max_imgs)
        pila, mjds = sv.alinear(cuadros, coord, size)
        titulo = etiqueta or nombre or f"ra{coord.ra.deg:.3f}_dec{coord.dec.deg:.3f}"
    pila, mjds = np.asarray(pila), np.asarray(mjds)
    if len(pila) < 2:
        raise RuntimeError("Quedaron menos de 2 cuadros utilizables. "
                           "Proba un campo mas grande.")
    if diff:
        pila = pila - np.nanmedian(pila, axis=0)
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, _slug(titulo) + ("_dif" if diff else "") + ".gif")
    g.MAX_FRAMES = frames
    g.guardar_gif(pila, mjds, size, ruta)
    print(f"Listo: {ruta}")
    return ruta


def interfaz(mostrar=True):
    """Panel con campos y botones (para Colab / Jupyter)."""
    import ipywidgets as w
    from IPython.display import Image, display
    try:                                           # necesario en algunas versiones de Colab
        from google.colab import output
        output.enable_custom_widget_manager()
    except Exception:
        pass

    estilo = {"description_width": "110px"}
    ancho = w.Layout(width="420px")
    opciones = ["(personalizado)"] + [f"{o[0]:02d}  {o[1]}" for o in g.OBJETOS]
    preset = w.Dropdown(options=opciones, description="Objeto del panel", style=estilo, layout=ancho)
    nombre = w.Text(description="Nombre", placeholder="ej: Barnard's Star", style=estilo, layout=ancho)
    ra = w.Text(description="RA", placeholder="grados o 08h55m10.8s", style=estilo, layout=ancho)
    dec = w.Text(description="Dec", placeholder="grados o -07d14m42s", style=estilo, layout=ancho)
    size = w.FloatSlider(value=10, min=3, max=30, step=1, description="Campo (arcmin)", style=estilo, layout=ancho)
    banda = w.Dropdown(options=sv.BANDAS, value="SPHEREx-D2", description="Detector", style=estilo, layout=ancho)
    frames = w.IntSlider(value=20, min=6, max=40, description="Cuadros", style=estilo, layout=ancho)
    diff = w.Checkbox(value=False, description="Modo diferencia (resalta lo que cambia)")
    mueve = w.Checkbox(value=False, description="El objeto se mueve (planeta, asteroide, cometa)")
    hid = w.Text(description="ID Horizons", placeholder="ej: 433", style=estilo, layout=ancho)
    htipo = w.Dropdown(options=["smallbody", "majorbody", "designation"], description="Tipo de ID",
                       style=estilo, layout=ancho)
    boton = w.Button(description="Generar GIF", button_style="success", icon="play")
    bajar = w.Button(description="Descargar ultimo GIF", icon="download")
    out = w.Output()
    ultimo = {"ruta": None}

    def al_elegir(cambio):
        i = preset.index
        if i == 0:
            return
        n, nom, ra_o, dec_o, sz, ids = g.OBJETOS[i - 1]
        nombre.value = nom
        size.value = min(30, max(3, sz))
        if ids is None:
            c = g.coord_objeto(n, ra_o, dec_o)
            ra.value, dec.value = f"{c.ra.deg:.5f}", f"{c.dec.deg:.5f}"
            mueve.value = False
        else:
            ra.value = dec.value = ""
            mueve.value = True
            hid.value, htipo.value = ids[0]
    preset.observe(al_elegir, names="value")

    def al_generar(_):
        out.clear_output()
        with out:
            try:
                ruta = generar(
                    nombre=nombre.value.strip() or None,
                    ra=ra.value.strip() or None, dec=dec.value.strip() or None,
                    size=size.value, banda=banda.value, frames=frames.value,
                    diff=diff.value,
                    horizons=(hid.value.strip() or None) if mueve.value else None,
                    tipo=htipo.value)
                ultimo["ruta"] = ruta
                display(Image(filename=ruta))
            except Exception as e:
                print(f"ERROR: {type(e).__name__}: {e}")
    boton.on_click(al_generar)

    def al_bajar(_):
        with out:
            try:
                from google.colab import files
                files.download(ultimo["ruta"])
            except Exception as e:
                print("No hay GIF para descargar todavia o no estas en Colab:", e)
    bajar.on_click(al_bajar)

    panel = w.VBox([preset, nombre, ra, dec, size, banda, frames, diff, mueve, hid, htipo,
                    w.HBox([boton, bajar]), out])
    if mostrar:
        display(panel)
    return panel


if __name__ == "__main__":
    print(__doc__)
