# Build the settings UI separately; no Node.js is needed in the runtime image.
FROM node:22-alpine AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PC1_DATA_DIR=/data \
    PC1_PRINTER_OUTPUT=esp32 \
    PC1_PORT=8000

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 paper \
    && useradd --uid 1000 --gid paper --no-create-home --home-dir /data paper

COPY app/ ./app/
COPY icons/ ./icons/
COPY --from=frontend /build/web/dist/ ./web/dist/

ARG APP_VERSION=unknown
RUN printf '%s\n' "$APP_VERSION" > /app/.version \
    && mkdir -p /data \
    && chown paper:paper /data

USER paper:paper
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:"+os.environ["PC1_PORT"]+"/api/health",timeout=3)' || exit 1

# One worker: scheduling, selection and printer reservations are process-local.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port \"$PC1_PORT\" --workers 1"]
