# Imagen base: Python 3.11 slim
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app.
# Son SEIS archivos + la carpeta de configuración de Streamlit.
# Si falta cualquiera, el contenedor construye bien
# pero revienta al arrancar con ModuleNotFoundError.
#   app.py           · la aplicación
#   blindaje.py      · motor de respuestas (dos vías)
#   ui.py            · tema visual y componentes
#   motor_seo.py     · motor SEO anclado a la Ficha de Verdad
#   motor_agente.py  · asistente de crecimiento local
#   .streamlit/      · configuración de tema nativo (config.toml)
COPY app.py .
COPY blindaje.py .
COPY ui.py .
COPY motor_seo.py .
COPY motor_agente.py .
COPY .streamlit/ .streamlit/

ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_LOGGER_LEVEL=info
EXPOSE 8080
CMD streamlit run app.py --server.port=8080 --server.address=0.0.0.0