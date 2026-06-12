# Isolated runner for agent-generated skills.
# Deliberately minimal: no DB drivers, no object-storage clients, no secrets.
FROM python:3.13-slim

RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.34.* httpx==0.28.*

WORKDIR /app
COPY runner.py /app/runner.py

# Non-root; the filesystem is mounted read-only by compose, /tmp is tmpfs.
USER 65534:65534

EXPOSE 8077
CMD ["uvicorn", "runner:app", "--host", "0.0.0.0", "--port", "8077", "--no-server-header"]
