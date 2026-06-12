FROM python:3.12-slim

# NVML bindings; libnvidia-ml.so is injected by the NVIDIA container runtime.
RUN pip install --no-cache-dir nvidia-ml-py

COPY server.py /app/server.py

EXPOSE 9966

# Runs as root on purpose: /dev/mem + PCIe BAR mmap require it.
CMD ["python", "/app/server.py"]
