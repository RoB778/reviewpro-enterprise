# Imagen base: Python 3.11 slim (ligero, optimizado)
FROM python:3.11-slim

# Directorio de trabajo en el contenedor
WORKDIR /app

# Copiar requirements.txt
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app
COPY app.py .

# Variables de entorno para Streamlit en headless mode
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_PORT=8080
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_LOGGER_LEVEL=info

# Exponer el puerto que Render usa
EXPOSE 8080

# Comando de inicio
CMD ["streamlit", "run", "app.py", "--server.port=${PORT:-8080}", "--server.address=0.0.0.0"]
