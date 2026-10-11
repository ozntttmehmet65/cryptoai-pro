# CryptoAI Pro V7.2 - readable source; existing data and models retained.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data AUTO_TRAIN=false COIN_AUTO_TRAIN=true COIN_RETRAIN_HOURS=6 COIN_RETRY_SECONDS=3600 PRO_TRAIN_CANDLES=18000 ENABLE_REST_SCANNER=false MAX_WATCHED=700 KLINE_SOCKET_SIZE=72 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
WORKDIR /app
COPY source/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /app/data
COPY source/ /app/
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
