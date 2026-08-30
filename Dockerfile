# Imagen base: Python 3.11 slim
FROM python:3.11-slim

# Sin .pyc y con stdout sin buffer: los logs de Render aparecen al momento
# en vez de quedarse retenidos hasta que se llena el búfer, que es justo lo
# que no quieres cuando estás mirando por qué falla un despliegue.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Las dependencias van en su propia capa, antes del código: así un cambio en
# app.py no invalida la caché del pip install y el despliegue tarda segundos
# en vez de minutos.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Antes se copiaba archivo por archivo, con un comentario avisando de que si
# faltaba uno el contenedor construía bien y reventaba al arrancar con
# ModuleNotFoundError. Eso es frágil por diseño: cada archivo nuevo obligaba a
# acordarse de tocar el Dockerfile. Con COPY . . más .dockerignore, lo que se
# EXCLUYE está declarado en un sitio y añadir un módulo no requiere nada.
COPY . .

# Usuario sin privilegios. Un contenedor que corre como root convierte
# cualquier ejecución de código en control total del contenedor.
RUN useradd --create-home --uid 10001 reselia && chown -R reselia:reselia /app
USER reselia

ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_LOGGER_LEVEL=info \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8080

# Forma exec (lista), no forma shell. Con la forma shell el proceso real es
# /bin/sh y Streamlit no recibe el SIGTERM de los redespliegues de Render:
# se queda colgado hasta que lo matan a la fuerza, cortando las sesiones
# abiertas en vez de cerrarlas.
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]
