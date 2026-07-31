# Imagen base: Python 3.11 slim
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app.
# OJO: son TRES archivos. Si falta cualquiera, el contenedor construye bien
# pero revienta al arrancar con ModuleNotFoundError.
#   app.py       · la aplicación
#   blindaje.py  · motor de respuestas (dos vías)
#   ui.py        · tema visual y componentes
COPY app.py .
COPY blindaje.py .
COPY ui.py .

ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_LOGGER_LEVEL=info

EXPOSE 8080

CMD streamlit run app.py --server.port=8080 --server.address=0.0.0.0
