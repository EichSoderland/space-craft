# Space-Craft · Mapa del cielo con SPHEREx

Web con dos partes:

1. **Mapa del cielo** (paleta Minecraft) con 20 objetos y su ficha, y una **guía colapsable** a la derecha con tutorial, glosario y ejemplos de coordenadas. Los que tienen GIF muestran
   imágenes reales de SPHEREx (`static/gifs_pre/`); el resto, una animación ilustrativa.
2. **Explorá el cielo**: se escribe un nombre o coordenadas (RA/Dec) y el servidor busca las
   imágenes en IRSA, baja un recorte chico de cada una, las alinea y devuelve un GIF.

## Probarlo en tu compu
```
pip install -r requirements.txt
uvicorn app:app --reload
```
Abrí http://localhost:8000

## Publicarlo gratis en Render (sin tarjeta)
1. Creá una cuenta en https://github.com y un repositorio nuevo (por ej. `spherex-web`).
   Subí TODO el contenido de esta carpeta (botón "Add file" → "Upload files"), con las
   carpetas `static/` y `static/gifs_pre/` adentro.
2. Entrá a https://render.com y registrate con tu cuenta de GitHub.
3. **New +** → **Web Service** → elegí tu repositorio.
4. Render detecta el `Dockerfile` (Language: **Docker**). En **Instance Type** elegí **Free**.
5. En **Environment Variables** agregá `WORKERS` = `1` y `MAX_IMGS` = `40`.
   (Si usaste el archivo `render.yaml` como "Blueprint", ya vienen cargadas.)
6. **Create Web Service**. Tarda unos 5 a 10 minutos. La URL queda `https://TU-NOMBRE.onrender.com`.
7. Probá `https://TU-NOMBRE.onrender.com/api/salud` (debe responder `{"ok":true}`) y después la página.

Limitaciones del plan gratis de Render: 512 MB de RAM y 0,1 CPU, se "duerme" tras 15 minutos sin
visitas (la primera visita tarda ~1 minuto en despertarlo) y el disco se borra al reiniciar, así
que la caché empieza de cero. Si el servidor se queda sin memoria (en Logs aparece "Out of memory"),
bajá `MAX_IMGS` a 25 o pasá al plan Starter (US$ 7 por mes).

## Usarlo desde otra página (incrustar) o con un enlace directo
La página lee parámetros en la URL y ejecuta sola:
```
https://TU-URL/?nombre=Barnard%27s%20Star
https://TU-URL/?ra=133.7951&dec=-7.2452&size=15&banda=D3
https://TU-URL/?horizons=433&diff=1
```
Parámetros: `nombre`, `ra`, `dec`, `size` (3 a 20 arcmin), `banda` (D1 a D6), `diff=1`,
`horizons` (ID de JPL Horizons, para objetos que se mueven).
Para incrustar: `<iframe src="https://TU-URL/?nombre=RW%20Aur" width="100%" height="900"></iframe>`

## Página en un sitio estático y servidor en Render (opcional)
Sirve para publicar la página en Vercel, Netlify, Cloudflare Pages o GitHub Pages (gratis y siempre
encendidos) mientras Render solo atiende los pedidos de GIF.
1. Publicá el servidor en Render (arriba). En Render, en Environment, podés definir `ALLOW_ORIGINS`
   con la URL de tu sitio (si no, acepta cualquiera).
2. Copiá la carpeta `static/` a una carpeta nueva. En su `index.html`, en la línea
   `<meta name="api-base" content="">`, poné la URL de Render sin barra final:
   `<meta name="api-base" content="https://TU-NOMBRE.onrender.com">`
3. Subí esa carpeta al sitio estático (framework "Other", sin comando de build).

## Por qué NO sirve Vercel para el servidor (error 500 FUNCTION_INVOCATION_FAILED)
Vercel ejecuta funciones cortas, sin hilos en segundo plano ni disco de escritura y con tope de
tiempo; esta herramienta necesita un servidor encendido varios minutos y librerías pesadas
(astropy, astroquery, reproject). Vercel solo sirve para la página estática.

## Hugging Face Spaces
Los Spaces con Docker o Gradio exigen un plan pago; solo los Spaces "Static" son gratis.
Un Space Static puede alojar la página (carpeta `static/`), no el servidor.

## Límites del servidor (para no saturar el servidor ni a IRSA)
Campo máximo 20 arcmin, 30 cuadros, `MAX_IMGS` imágenes por pedido, `WORKERS` pedidos a la vez
y 8 en cola. Los pedidos repetidos salen al instante (caché).

## Pendientes conocidos
- Los objetos 1 al 7 y el 9 son nuevos y todavía no tienen GIF real (se ven con animación ilustrativa).
  Generalos desde el explorador de la propia web (o con Colab) y guardalos como `static/gifs_pre/NN.gif`.
- Los GIFs 8 (Barnard) y 15 (Próxima) conviene rehacerlos con las coordenadas corregidas por
  movimiento propio.
- Para sumar un GIF al mapa: copialo como `static/gifs_pre/NN.gif` y agregá su línea en
  `REAL_GIFS` dentro de `static/index.html`.

## Archivos
`app.py` servidor (FastAPI) · `herramienta_colab.py`, `spherex_video.py`, `generar_gifs.py` motor ·
`static/index.html` página · `Dockerfile`, `requirements.txt`, `render.yaml` para publicar
