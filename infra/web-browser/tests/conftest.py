"""The synthetic test sites live on 127.0.0.1: an explicit egress exception
for the tests only (E33 blocks loopback by default)."""

import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import egress  # noqa: E402

egress.ALLOW_NETWORKS[:] = [ipaddress.ip_network("127.0.0.1/32")]
