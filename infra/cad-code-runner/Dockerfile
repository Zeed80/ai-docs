# Isolated runner for VLM-generated CAD-reading code (ezdxf/numpy geometry
# scripts). Deliberately a SEPARATE image from skill-runner: skill-runner's
# near-empty dependency set (fastapi/uvicorn/httpx only) is itself a security
# property for promoted skills, and is not widened here just because this
# runner needs ezdxf/numpy for a narrower, higher-frequency, less-reviewed use
# case (one script per CAD digitization, never promoted/reviewed by a human).
FROM python:3.13-slim

RUN pip install --no-cache-dir \
    fastapi==0.115.* uvicorn==0.34.* \
    ezdxf==1.3.* numpy==2.*

WORKDIR /app
COPY runner.py /app/runner.py

# Non-root; the filesystem is mounted read-only by compose, /tmp is tmpfs.
# No volumes — code and results travel only in the HTTP request/response
# body, nothing is ever persisted to disk between requests.
USER 65534:65534

EXPOSE 8078
CMD ["uvicorn", "runner:app", "--host", "0.0.0.0", "--port", "8078", "--no-server-header"]
