# LabForge control plane - multi-stage build.
# Builder: install Python deps into a prefix.
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime: slim image with the libvirt/qemu client tooling.
FROM python:3.12-slim

# Build-time version, baked into the image so the app reports it at runtime.
ARG APP_VERSION=0.1.0

# virsh, virt-install (Debian package: virtinst), qemu-img, cloud-localds,
# and genisoimage for building the cloud-init seed ISO.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libvirt-clients \
    virtinst \
    qemu-utils \
    cloud-image-utils \
    genisoimage \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin labforge

# Copy Python deps from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# App code
COPY app/ app/
COPY cloud_init_templates/ cloud_init_templates/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION}

RUN chown -R labforge:labforge /app
USER labforge

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
