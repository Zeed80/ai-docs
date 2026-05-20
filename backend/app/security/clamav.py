"""ClamAV antivirus scan integration.

Connects to clamd via TCP (default: localhost:3310) or Unix socket.
Returns ScanResult with is_clean flag and optional threat name.
Falls back gracefully when ClamAV is not available.
"""

from __future__ import annotations

import os
import socket
import struct
import logging

logger = logging.getLogger(__name__)

CLAMD_HOST = os.getenv("CLAMD_HOST", "localhost")
CLAMD_PORT = int(os.getenv("CLAMD_PORT", "3310"))
CLAMD_TIMEOUT = float(os.getenv("CLAMD_TIMEOUT", "10"))


class ScanResult:
    __slots__ = ("is_clean", "threat", "skipped")

    def __init__(
        self,
        is_clean: bool,
        threat: str | None = None,
        skipped: bool = False,
    ):
        self.is_clean = is_clean
        self.threat = threat
        self.skipped = skipped

    def __repr__(self) -> str:
        if self.skipped:
            return "ScanResult(skipped)"
        return f"ScanResult(clean={self.is_clean}, threat={self.threat!r})"


def scan_bytes(data: bytes) -> ScanResult:
    """Scan *data* bytes using clamd INSTREAM protocol.

    Returns ScanResult(skipped=True) when clamd is unavailable.
    """
    try:
        with socket.create_connection((CLAMD_HOST, CLAMD_PORT), timeout=CLAMD_TIMEOUT) as sock:
            # INSTREAM command
            sock.sendall(b"zINSTREAM\0")
            # Send data in chunks (max 4KB each)
            chunk_size = 4096
            for i in range(0, len(data), chunk_size):
                chunk = data[i : i + chunk_size]
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)
            # Zero-length chunk signals end
            sock.sendall(struct.pack("!I", 0))

            response = b""
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                response += part
                if b"\0" in part or b"\n" in part:
                    break

        result_str = response.rstrip(b"\0\n").decode("utf-8", errors="replace")
        # "stream: OK" or "stream: Eicar-Test-Signature FOUND"
        if result_str.endswith("OK"):
            return ScanResult(is_clean=True)

        parts = result_str.split()
        threat = parts[-2] if len(parts) >= 2 else result_str
        logger.warning("clamav_threat_detected", threat=threat, bytes=len(data))
        return ScanResult(is_clean=False, threat=threat)

    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        logger.debug("clamav_unavailable", reason=str(exc))
        return ScanResult(is_clean=True, skipped=True)
    except Exception as exc:
        logger.warning("clamav_scan_error", error=str(exc))
        return ScanResult(is_clean=True, skipped=True)
