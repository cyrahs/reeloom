FROM node:24-bookworm-slim AS web-builder

WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.13-slim-trixie

# 7-Zip unpacks subtitle releases (.7z/.zip/.rar), pinned by checksum.
# ffmpeg provides ffprobe, which version replacement uses to sample video
# quality; without it the comparison degrades to file sizes only.
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        xz-utils \
    && case "${TARGETARCH}" in \
        amd64) \
            sevenzip_url="https://www.7-zip.org/a/7z2602-linux-x64.tar.xz"; \
            sevenzip_sha256="41aaba7b1235304ab5aa0624530c67ae829496cd29e875925271efdccc28c03e" ;; \
        arm64) \
            sevenzip_url="https://www.7-zip.org/a/7z2602-linux-arm64.tar.xz"; \
            sevenzip_sha256="70ea6cc737ae1495ea2d7eb20ef3120fe579bd3f1a83a9d2362b62ec5bde2bba" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --proto '=https' --proto-redir '=https' \
        --tlsv1.2 --output /tmp/7zz.tar.xz "${sevenzip_url}" \
    && echo "${sevenzip_sha256}  /tmp/7zz.tar.xz" | sha256sum --check --strict \
    && tar --extract --xz --file /tmp/7zz.tar.xz --directory /tmp 7zz \
    && install --mode=0755 /tmp/7zz /usr/bin/7zz \
    && rm -f /tmp/7zz /tmp/7zz.tar.xz \
    && apt-get purge -y --auto-remove curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

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
ENTRYPOINT ["reeloom"]
