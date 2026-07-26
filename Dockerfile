FROM node:24-bookworm-slim AS web-builder

WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web-builder /app/src/reeloom/server/static ./src/reeloom/server/static
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 reeloom \
    && mkdir -p /var/lib/reeloom \
    && chown -R reeloom:reeloom /var/lib/reeloom
USER reeloom

EXPOSE 8080
ENTRYPOINT ["reeloom-server"]
