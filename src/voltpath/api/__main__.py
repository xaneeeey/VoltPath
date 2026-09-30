import os

import uvicorn

from voltpath.config import setup_logging
from voltpath.metrics import prepare_multiproc


def app():
    from voltpath.api.app import create_app

    return create_app()


if __name__ == "__main__":
    setup_logging("api")
    workers = int(os.environ.get("API_WORKERS", "4"))
    if workers > 1:
        prepare_multiproc("api")
    uvicorn.run("voltpath.api.__main__:app", factory=True, host="0.0.0.0", port=8000, workers=workers,
                log_level="warning", access_log=False, proxy_headers=True, forwarded_allow_ips="*")
