# Human-like page fetcher: Google Chrome in a window on a virtual display,
# driven by patchright (Playwright with anti-detection patches). Renders
# JavaScript, keeps per-person profiles, returns readable text + optional
# screenshot. Internal-only sidecar — no published ports.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_PROFILES_DIR=/profiles

WORKDIR /app

# Fonts a desktop has: a page measured in a font-less browser is a bot signal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
        fonts-noto-cjk xvfb \
    && apt-get clean

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    # W2: Google Chrome stable is the browser sites see; the bundled
    # Chromium stays as the BROWSER_CHANNEL=chromium fallback.
    && patchright install chrome \
    && patchright install chromium \
    && apt-get clean

COPY server.py egress.py profiles.py humanize.py challenges.py /app/

# The base image already provides the non-root "pwuser". Kept profiles live
# on a volume mounted at /profiles; the directory is created owned by pwuser
# so a fresh named volume inherits that owner.
# Xvfb runs as pwuser and cannot create its socket directory itself.
RUN mkdir -p /profiles /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && chown -R pwuser:pwuser /app /profiles
USER pwuser

EXPOSE 8093

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1"]
