"""Database maintenance readiness gate shared between main and ASGI middleware."""

import asyncio

db_ready = asyncio.Event()
db_ready.set()
