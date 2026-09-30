import os

import httpx


def before_all(context):
    context.base = os.environ.get("VOLTPATH_API", "http://localhost:8000")
    context.password = os.environ.get("SEED_PASSWORD", "voltpath-demo")
    context.http = httpx.Client(base_url=context.base, timeout=30)


def after_all(context):
    context.http.close()
