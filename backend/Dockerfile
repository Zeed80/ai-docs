# ── Stage 1: Build libredwg (dwg2dxf binary) ─────────────────────────────────
FROM python:3.11-slim AS libredwg-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential autoconf automake libtool pkg-config \
    libpcre2-dev curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# libredwg 0.13.4 — open-source DWG→DXF converter (~90% entity coverage)
RUN curl -fsSL \
    "https://github.com/LibreDWG/libredwg/releases/download/0.13.4/libredwg-0.13.4.tar.xz" \
    -o /tmp/libredwg.tar.xz \
    && tar xJf /tmp/libredwg.tar.xz -C /tmp \
    && cd /tmp/libredwg-0.13.4 \
    && ./configure \
        --prefix=/opt/libredwg \
        --disable-bindings \
        --disable-docs \
        --disable-python \
        --disable-perl \
        --disable-static \
        --with-dxf-precision=rfc \
    && make -j"$(nproc)" \
    && make install-strip \
    && rm -rf /tmp/libredwg*

# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Runtime deps: libxml2 for lxml, libglib for cairosvg, libffi for cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libxml2 libglib2.0-0 libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy libredwg runtime: binary + shared library
COPY --from=libredwg-builder /opt/libredwg/bin/dwg2dxf /usr/local/bin/dwg2dxf
COPY --from=libredwg-builder /opt/libredwg/lib/libredwg.so* /usr/local/lib/
RUN ldconfig

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
