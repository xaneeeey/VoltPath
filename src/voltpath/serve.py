"""Multi-process ASGI launcher with one SO_REUSEPORT socket per worker.

Uvicorn's `workers=N` shares a single accept socket; on Linux long-lived keep-alive connections
then pile onto whichever worker won the first accepts (we measured 1.2 of 4 cores busy under
load). With SO_REUSEPORT every worker binds its own socket and the kernel hashes new
connections across them - even load without an extra proxy hop.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import socket
import sys

import uvicorn


def _worker(app: str, port: int, log_level: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("0.0.0.0", port))  # noqa: S104 - container listens on all interfaces
    sock.listen(2048)
    config = uvicorn.Config(app, factory=True, log_level=log_level, access_log=False, proxy_headers=True,
                            forwarded_allow_ips="*", timeout_keep_alive=15)
    uvicorn.Server(config).run(sockets=[sock])


def serve(app: str, port: int, workers: int, log_level: str = "warning") -> None:
    if workers <= 1 or sys.platform == "darwin":  # macOS SO_REUSEPORT does not load-balance
        uvicorn.run(app, factory=True, host="0.0.0.0", port=port, workers=workers, log_level=log_level,  # noqa: S104
                    access_log=False, proxy_headers=True, forwarded_allow_ips="*")
        return
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(app, port, log_level), daemon=False) for _ in range(workers)]
    for p in procs:
        p.start()

    def stop(*_):
        for p in procs:
            if p.is_alive():
                os.kill(p.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for p in procs:
        p.join()
