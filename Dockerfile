# Imagen base: Python 3.11 slim
FROM python:3.11-slim

# Directorio de trabajo en el contenedor
WORKDIR /app

# Copiar requirements.txt
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app.
# OJO: antes aquí solo estaba "COPY app.py .". Ahora hay que copiar también
# blindaje.py, porque app.py lo importa en el arranque. Si falta, el
# contenedor construye bien pero revienta al primer inicio con
# ModuleNotFoundError: No module named 'blindaje'.
COPY app.py .
COPY blindaje.py .

# Variables de entorno para Streamlit en headless mode
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_LOGGER_LEVEL=info

# Exponer el puerto
EXPOSE 8080

# Comando de inicio (forma simple)
CMD streamlit run app.py --server.port=8080 --server.address=0.0.0.0
