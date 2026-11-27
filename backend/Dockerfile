FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
# El índice de PyPI de «av» es grande: con conexiones lentas el tiempo de espera por defecto (15 s) falla.
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=6 PIP_NO_CACHE_DIR=1
RUN pip install fastapi uvicorn httpx
RUN pip install "av==19.0.2"
COPY main.py switcher.py outputs.py ./
COPY static ./static
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
