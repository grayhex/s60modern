"""Child processes started by the Bridge, so that closing the Bridge can leave none behind.

Windows (settings, connections) and the PowerShell helpers of the host scan are registered here; ``terminate_all`` ends
whatever is still running. Gammu workers are tracked by ``gammu_native.terminate_workers`` and the installer launched by an
update is deliberately *not* registered: it has to outlive the Bridge.
"""

from __future__ import annotations

import subprocess
import threading

_lock = threading.Lock()
_running: set[subprocess.Popen] = set()


def register(process: subprocess.Popen) -> subprocess.Popen:
    with _lock:
        _running.add(process)
    return process


def spawn(command: list[str], **kwargs) -> subprocess.Popen:
    """Start a long-lived helper (a window) and remember it."""
    return register(subprocess.Popen(command, **kwargs))


def run(command: list[str], *, timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a short-lived helper whose process can be killed from another thread."""
    kwargs.pop("check", None)
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = register(subprocess.Popen(command, **kwargs))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        with _lock:
            _running.discard(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def alive() -> list[subprocess.Popen]:
    with _lock:
        return [p for p in _running if p.poll() is None]


def terminate_all(grace: float = 3.0) -> int:
    """End every registered process that is still running; returns how many were running."""
    with _lock:
        processes = list(_running)
        _running.clear()
    running = [p for p in processes if p.poll() is None]
    for process in running:
        process.terminate()
    for process in running:
        try:
            process.wait(grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(grace)
    return len(running)
