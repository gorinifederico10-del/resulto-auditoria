# Imagen base oficial de Microsoft con Playwright + Chromium ya instalados.
# Esto evita el dolor de instalar a mano libnss3, libatk1.0, libxkb, etc.
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Variables Python sanas para containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencias primero (cache friendly)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Código
COPY . .

# Puerto: Railway inyecta $PORT en runtime. Default a 8001 para correr local.
ENV PORT=8001
EXPOSE 8001

# Arrancar uvicorn escuchando en $PORT (importante: 0.0.0.0, no 127.0.0.1).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
