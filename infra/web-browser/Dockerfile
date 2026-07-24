# Human-like page fetcher: headless Chromium + stealth patches (patchright).
# Renders JavaScript, clears basic bot walls, returns readable text + optional
# screenshot. Internal-only sidecar — no published ports.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    # patchright ships its own patched Chromium download
    && patchright install chromium

COPY server.py /app/server.py

# The base image already provides the non-root "pwuser".
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8093

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1"]
