import os

import uvicorn

from voltpath.config import setup_logging
from voltpath.metrics import prepare_multiproc


def app():
    from voltpath.ingest.app import create_app

    return create_app()


if __name__ == "__main__":
    setup_logging("ingest")
    prepare_multiproc("ingest")
    uvicorn.run("voltpath.ingest.__main__:app", factory=True, host="0.0.0.0", port=8001,
                workers=int(os.environ.get("INGEST_WORKERS", "4")), log_level="warning",
                access_log=False, http="httptools", loop="uvloop")
