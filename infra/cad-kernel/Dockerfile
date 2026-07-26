# FreeCAD 1.1.x — deliberately NOT the distribution's "stable" 1.0.
#
# Ubuntu 22.04 ships 0.19, which is what this image ran for a long time. Two
# things forced the move: 0.19 has no DrawComplexSection at all, so a stepped
# or offset section — the ordinary case on a real ЕСКД sheet — cannot be
# produced; and 0.19 stopped receiving fixes years ago.
#
# The obvious upgrade target, Debian stable's 1.0.0, was measured and REJECTED:
# headless it computes NO view geometry whatsoever (zero edges from a view that
# both 0.19 and 1.1.1 project fine), and neither KeepUpdated nor a forced
# recompute helps. That is invisible in the changelogs and only shows up by
# running it, so it is recorded here.
#
# 1.1 also brings the API this project was hand-writing: makeGeomHatch,
# makeDistanceDim/makeExtentDim, and findOuterWire/edgeWalker/findShapeOutline
# — the wire ordering the /drawing endpoint had to implement itself.
#
# sid is a rolling repository, so the packages are pinned; without that a
# rebuild silently drifts to whatever sid holds that day.
FROM debian:sid

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/freecad-home \
    PYTHONPATH=/usr/lib/freecad-python3/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        freecad-common=1.1.1+dfsg-3 \
        freecad-python3=1.1.1+dfsg-3+b2 \
        python3-venv \
    && apt-get clean

RUN python3 -m venv --system-site-packages /opt/cad-kernel \
    && /opt/cad-kernel/bin/pip install --no-cache-dir \
        "fastapi==0.115.*" "uvicorn==0.34.*" "pydantic==2.*"

WORKDIR /app
COPY server.py /app/server.py

RUN python3 -c "import FreeCAD, Part, TechDraw; print(FreeCAD.Version())" \
    && groupadd --gid 10001 cadkernel \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cadkernel \
    && chown -R cadkernel:cadkernel /app

USER cadkernel
EXPOSE 8092

CMD ["/opt/cad-kernel/bin/python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8092", "--workers", "1"]
