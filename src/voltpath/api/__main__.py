import os

from voltpath.config import setup_logging
from voltpath.metrics import prepare_multiproc
from voltpath.serve import serve


def app():
    from voltpath.api.app import create_app

    return create_app()


if __name__ == "__main__":
    setup_logging("api")
    workers = int(os.environ.get("API_WORKERS", "4"))
    if workers > 1:
        prepare_multiproc("api")
    serve("voltpath.api.__main__:app", 8000, workers)
