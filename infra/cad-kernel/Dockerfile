FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/freecad-home \
    PYTHONPATH=/usr/lib/freecad-python3/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates freecad-python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/cad-kernel \
    && /opt/cad-kernel/bin/pip install --no-cache-dir \
        "fastapi==0.115.*" "uvicorn==0.34.*" "pydantic==2.*"

WORKDIR /app
COPY server.py /app/server.py

RUN python3 -c "import FreeCAD, Part; print(FreeCAD.Version())" \
    && groupadd --gid 10001 cadkernel \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cadkernel \
    && chown -R cadkernel:cadkernel /app

USER cadkernel
EXPOSE 8092

CMD ["/opt/cad-kernel/bin/python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8092", "--workers", "1"]
