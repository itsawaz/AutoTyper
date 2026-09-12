"""Vercel serverless entrypoint.

Vercel's Python runtime imports `app` from files under `api/`. We expose the
FastAPI ASGI app defined in main.py. The parent directory is added to sys.path
so the flat module layout (main.py, db.py, ...) imports cleanly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app  # noqa: E402

# Vercel looks for a module-level ASGI callable named `app`.
