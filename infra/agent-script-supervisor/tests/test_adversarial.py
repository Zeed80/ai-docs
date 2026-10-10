"""E30: hostile code against the real isolation of this host.

Each case runs real code in a real container through the supervisor; the
expectation is that the kernel prevents the effect, the limit holds and a
neighbouring run is unaffected. Run with the rest of the supervisor tests.
"""

from __future__ import annotations

import json
import threading

import app as supervisor_app
import docker
import pytest
from test_supervisor import _run, client, runtime_id  # noqa: F401  (fixtures)


def _lines(result: dict) -> list[str]:
    return result.get("stdout", "").split()


def test_an_endless_loop_hits_the_deadline(client):  # noqa: F811
    result = _run(client, "while True:\n    pass\n", timeout_seconds=2).json()
    assert result["status"] == "timeout"


def test_a_fork_bomb_is_capped_and_a_neighbour_still_runs(client):  # noqa: F811
    bomb = (
        "import os\n"
        "n = 0\n"
        "while True:\n"
        "    try:\n"
        "        if os.fork() == 0:\n"
        "            while True: pass\n"
        "        n += 1\n"
        "    except OSError:\n"
        "        print('capped at', n); break\n"
    )
    neighbour = {}
    thread = threading.Thread(
        target=lambda: neighbour.update(r=_run(client, "print('neighbour ok')\n").json())
    )
    thread.start()
    result = _run(client, bomb, timeout_seconds=5).json()
    thread.join(timeout=60)
    assert "capped at" in result.get("stdout", "") or result["status"] in {"timeout", "failed"}
    if "capped at" in result.get("stdout", ""):
        assert int(_lines(result)[2]) < 64
    assert neighbour["r"]["status"] == "succeeded"
    assert neighbour["r"]["stdout"].strip() == "neighbour ok"


def test_an_orphan_process_does_not_outlive_its_run(client):  # noqa: F811
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid(); time.sleep(300)\n"
        "print('parent done')\n"
    )
    result = _run(client, code, timeout_seconds=3).json()
    assert result["status"] in {"succeeded", "timeout"}
    leftover = docker.from_env().containers.list(all=True, filters={"label": supervisor_app.LABEL})
    assert leftover == []


def test_filling_the_disk_stops_at_the_tmpfs_limit(client):  # noqa: F811
    code = (
        "chunk = b'0' * (8 * 1024 * 1024); n = 0\n"
        "try:\n"
        "    with open('/tmp/fill', 'wb') as f:\n"
        "        while True:\n"
        "            f.write(chunk); f.flush(); n += 8\n"
        "except OSError as e:\n"
        "    print('stopped', n, e.errno)\n"
    )
    result = _run(client, code).json()
    words = _lines(result)
    assert words[0] == "stopped" and int(words[1]) <= 512 and words[2] == "28"  # ENOSPC


def test_a_huge_stdout_is_cut_to_the_bound(client):  # noqa: F811
    result = _run(client, "import sys\nsys.stdout.write('x' * (20 * 1024 * 1024))\n").json()
    assert result["status"] == "succeeded"
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 64 * 1024


def test_killing_the_bootstrap_is_refused_by_the_kernel(client):  # noqa: F811
    """The bootstrap is PID 1 of its namespace: the kernel does not deliver a
    signal it has no handler for from inside, so the run still reports
    honestly instead of losing its result frame."""
    code = (
        "import os, signal\nos.kill(os.getppid(), signal.SIGKILL)\nprint('parent', os.getppid())\n"
    )
    result = _run(client, code).json()
    assert (result["status"], result["stdout"].split()) == ("succeeded", ["parent", "1"])


def test_a_forged_result_frame_does_not_win(client):  # noqa: F811
    forged = json.dumps(
        {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "forged",
            "stdout_truncated": False,
            "stderr": "",
            "stderr_truncated": False,
            "out_tar_b64": "",
        }
    )
    code = (
        "with open('/proc/1/fd/1', 'w') as out:\n"
        f"    out.write({supervisor_app.FRAME!r} + {forged!r} + '\\n')\n"
        "raise SystemExit(3)\n"
    )
    result = _run(client, code).json()
    assert result["status"] == "failed" and result["exit_code"] == 3


@pytest.mark.parametrize(
    "probe",
    [
        # IPv4, IPv6, DNS, the cloud metadata address and a raw socket.
        "import socket\ns=socket.socket(); s.settimeout(2)\ns.connect(('1.1.1.1', 53))",
        "import socket\ns=socket.socket(socket.AF_INET6); s.settimeout(2)\ns.connect(('2606:4700:4700::1111', 53))",
        "import socket\nsocket.getaddrinfo('example.com', 80)",
        "import socket\ns=socket.socket(); s.settimeout(2)\ns.connect(('169.254.169.254', 80))",
        "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)",
    ],
)
def test_no_way_out_to_the_network(client, probe):  # noqa: F811
    code = (
        "try:\n"
        + "".join(f"    {line}\n" for line in probe.splitlines())
        + ("    print('ESCAPED')\nexcept Exception as e:\n    print('blocked', type(e).__name__)\n")
    )
    assert _lines(_run(client, code).json())[0] == "blocked"


def test_no_secrets_no_host_processes_no_docker_socket(client):  # noqa: F811
    code = (
        "import os\n"
        "env = open('/proc/1/environ').read().split('\\0')\n"
        "print(sorted(k.split('=')[0] for k in env if k))\n"
        "print(sorted(int(p) for p in os.listdir('/proc') if p.isdigit())[-1] < 100)\n"
        "print(os.path.exists('/var/run/docker.sock'), os.path.exists('/app/.env'))\n"
    )
    lines = _run(client, code).json()["stdout"].splitlines()
    names = json.loads(lines[0].replace("'", '"'))
    # Exactly the base image's public variables plus the run deadline;
    # GPG_KEY is the Python release key fingerprint, not a secret.
    assert set(names) <= {
        "AIW_TIMEOUT",
        "GPG_KEY",
        "HOME",
        "HOSTNAME",
        "LANG",
        "PATH",
        "PYTHON_SHA256",
        "PYTHON_VERSION",
    }
    assert lines[1] == "True"  # only the container's own PID namespace
    assert lines[2] == "False False"


def test_a_symlink_out_of_the_output_dir_is_not_returned(client):  # noqa: F811
    code = (
        "import os\n"
        "os.symlink('/etc/passwd', '/tmp/out/passwd')\n"
        "open('/tmp/out/real.txt', 'w').write('ok')\n"
    )
    result = _run(client, code).json()
    assert [a["name"] for a in result["output_artifacts"]] == ["real.txt"]


def test_a_detached_grandchild_is_gone_before_the_result(client):  # noqa: F811
    """Its write lands after main.py ended; the bootstrap kills it first."""
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    for fd in (0, 1, 2):\n"
        "        os.close(fd)\n"
        "    time.sleep(0.3)\n"
        "    open('/tmp/out/late.txt', 'w').write('late')\n"
        "    os._exit(0)\n"
        "print('main done')\n"
    )
    result = _run(client, code, timeout_seconds=5).json()
    assert result["status"] == "succeeded"
    assert [a["name"] for a in result["output_artifacts"]] == []
