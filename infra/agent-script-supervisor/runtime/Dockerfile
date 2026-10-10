# Script runtime (E27): a fixed bootstrap over a slim Python. The supervisor
# runs it by image ID from its allowlist, read-only, without network.
FROM python:3.11-slim
COPY bootstrap.py /opt/aiw/bootstrap.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "/opt/aiw/bootstrap.py"]
