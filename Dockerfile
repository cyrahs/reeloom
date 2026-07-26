FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 reeloom \
    && mkdir -p /var/lib/reeloom \
    && chown -R reeloom:reeloom /var/lib/reeloom
USER reeloom

EXPOSE 8080
ENTRYPOINT ["reeloom-server"]
