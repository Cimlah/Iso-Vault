# syntax=docker/dockerfile:1

# --- iPXE loaders -------------------------------------------------------------
# Built on the build platform, because the loaders run on clients, not this server.
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS ipxe
ARG IPXE_COMMIT=12798ec29aa8a64d8675c4378b99f5fe28447afb
RUN set -eu; \
    apt-get update; \
    case "$(dpkg --print-architecture)" in \
        amd64) cross=gcc-aarch64-linux-gnu ;; \
        arm64) cross=gcc-x86-64-linux-gnu ;; \
        *) echo "Unsupported build platform" >&2; exit 1 ;; \
    esac; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gcc libc6-dev make perl xz-utils liblzma-dev mtools "$cross"; \
    mkdir /ipxe; \
    curl -fsSL "https://github.com/ipxe/ipxe/archive/${IPXE_COMMIT}.tar.gz" \
        | tar -xz --strip-components=1 -C /ipxe
COPY ipxe/config/general.h /ipxe/src/config/local/general.h
COPY ipxe/embed.ipxe /ipxe/src/embed.ipxe
RUN set -eu; \
    cd /ipxe/src; \
    case "$(dpkg --print-architecture)" in \
        amd64) x86=""; arm=aarch64-linux-gnu- ;; \
        arm64) x86=x86_64-linux-gnu-; arm="" ;; \
    esac; \
    jobs="$(nproc)"; \
    make -j"$jobs" CROSS="$x86" EMBED=embed.ipxe bin/undionly.kpxe; \
    make -j"$jobs" CROSS="$x86" EMBED=embed.ipxe bin-x86_64-efi/snponly.efi bin-x86_64-efi/ipxe.efi; \
    make -j"$jobs" CROSS="$arm" EMBED=embed.ipxe bin-arm64-efi/snponly.efi bin-arm64-efi/ipxe.efi; \
    mkdir /loaders; \
    cp bin/undionly.kpxe /loaders/undionly.kpxe; \
    cp bin-x86_64-efi/snponly.efi /loaders/snponly-x64.efi; \
    cp bin-x86_64-efi/ipxe.efi /loaders/ipxe-x64.efi; \
    cp bin-arm64-efi/snponly.efi /loaders/snponly-arm64.efi; \
    cp bin-arm64-efi/ipxe.efi /loaders/ipxe-arm64.efi

# --- wimboot ------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM alpine:3.24 AS wimboot
ADD --checksum=sha256:5f067ccdc4d084d5bf77b6c853bd0f8402dfc2b4cd1b103d358993ae97fae8e3 \
    https://github.com/ipxe/wimboot/releases/download/v2.9.0/wimboot /loaders/wimboot
ADD --checksum=sha256:b1440c6386981fb447b2f341294ec7a59546d496dff0f075b0f698c853f1c949 \
    https://github.com/ipxe/wimboot/releases/download/v2.9.0/wimboot.arm64 /loaders/wimboot.arm64

# --- Web UI -------------------------------------------------------------------
# The standalone output has no native dependencies, so one build serves both architectures.
FROM --platform=$BUILDPLATFORM node:26-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --- Runtime ------------------------------------------------------------------
FROM alpine:3.24
RUN apk add --no-cache \
        7zip \
        ca-certificates \
        dnsmasq \
        iproute2 \
        libarchive-tools \
        losetup \
        mount \
        nginx \
        nodejs \
        python3 \
        rpcbind \
        samba-common-tools \
        samba-server \
        umount \
        unfs3 \
    && addgroup -S isovault \
    && adduser -S -D -H -G isovault -s /sbin/nologin isovault \
    && addgroup nginx isovault

COPY --from=ipxe /loaders/ /opt/isovault/loaders/
COPY --from=wimboot /loaders/ /opt/isovault/loaders/
COPY --from=web /web/.next/standalone/ /opt/isovault/web/
COPY --from=web /web/.next/static/ /opt/isovault/web/.next/static/
COPY --from=web /web/public/ /opt/isovault/web/public/
COPY templates/ /opt/isovault/templates/
COPY isovault/ /opt/isovault/isovault/

ENV PYTHONPATH=/opt/isovault PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
ENTRYPOINT ["python3", "-m", "isovault"]
CMD ["run"]
