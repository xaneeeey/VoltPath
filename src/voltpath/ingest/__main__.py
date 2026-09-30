import os

from voltpath.config import setup_logging
from voltpath.metrics import prepare_multiproc
from voltpath.serve import serve


def app():
    from voltpath.ingest.app import create_app

    return create_app()


if __name__ == "__main__":
    setup_logging("ingest")
    prepare_multiproc("ingest")
    serve("voltpath.ingest.__main__:app", 8001, int(os.environ.get("INGEST_WORKERS", "4")))
