"""Fixed entrypoint of the script runtime image (E27).

Reads a tar of the run's files from stdin into /tmp/work, runs main.py there
as a child with a deadline, then prints exactly one result frame to stdout:
the child's bounded stdout/stderr and a base64 tar of /tmp/out. The image's
root is read-only; /tmp is the only writable place and vanishes with the
container, so the result travels in the log, not in a file.
"""

import base64
import io
import json
import os
import signal
import subprocess
import sys
import tarfile

FRAME = "===AIW-SCRIPT-RESULT==="
LOG_LIMIT = 64 * 1024
OUT_FILES = 20
OUT_BYTES = 50 * 1024 * 1024


def _bounded(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > LOG_LIMIT
    return data[:LOG_LIMIT].decode("utf-8", "replace"), truncated


def main() -> None:
    os.makedirs("/tmp/work", exist_ok=True)
    os.makedirs("/tmp/out", exist_ok=True)
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        archive.extractall("/tmp/work", filter="data")
    timeout = float(os.environ.get("AIW_TIMEOUT", "60"))
    status, code = "succeeded", 0
    try:
        child = subprocess.run(
            [sys.executable, "-I", "main.py"],
            cwd="/tmp/work",
            capture_output=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/work"},
        )
        out, err, code = child.stdout, child.stderr, child.returncode
        if code != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        out, err, code, status = exc.stdout or b"", exc.stderr or b"", -9, "timeout"
    # Nothing but this process may write after this point: a detached
    # grandchild could otherwise append a forged result frame between ours
    # and the container's end (the supervisor reads the last frame). As PID 1
    # of the run's namespace, kill(-1) reaches every other process in it.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    buffer = io.BytesIO()
    files, total = 0, 0
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted(os.listdir("/tmp/out")):
            path = os.path.join("/tmp/out", name)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            size = os.path.getsize(path)
            if files >= OUT_FILES or total + size > OUT_BYTES:
                status = "failed" if status == "succeeded" else status
                err += b"\n[aiw] output limit exceeded"
                break
            archive.add(path, arcname=name)
            files, total = files + 1, total + size
    stdout, stdout_cut = _bounded(out)
    stderr, stderr_cut = _bounded(err)
    frame = {
        "status": status,
        "exit_code": code,
        "stdout": stdout,
        "stdout_truncated": stdout_cut,
        "stderr": stderr,
        "stderr_truncated": stderr_cut,
        "out_tar_b64": base64.b64encode(buffer.getvalue()).decode(),
    }
    sys.stdout.write(FRAME + json.dumps(frame) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
