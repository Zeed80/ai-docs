# Agent script supervisor (E27). The one component with the Docker socket:
# minimal code, no shell endpoint, runs only images pinned in SCRIPT_RUNTIMES.
FROM python:3.11-slim
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.34.* docker==7.1.*
WORKDIR /app
COPY app.py /app/app.py
EXPOSE 8078
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8078", "--no-server-header"]
