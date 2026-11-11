"""Fail-closed initialization state shared by HTTP, dialect and WebSocket auth."""
import asyncio
import os
import secrets
from time import monotonic
from types import SimpleNamespace

from fastapi import HTTPException

from core.jwt_utils import LEGACY_JWT_SECRET, set_jwt_secret


async def get_admin_user():
    from db import AdminUser, DB_TYPE, DISABLE_DATABASE, async_session_scope

    if DISABLE_DATABASE:
        return None
    async with async_session_scope() as session:
        if DB_TYPE == "d1":
            row = await session.query_one(
                "SELECT id, username, password_hash, jwt_secret FROM admin_user WHERE id = ?", [1]
            )
            return SimpleNamespace(**row) if row else None
        return await session.get(AdminUser, 1)


async def _ensure_persisted_secret(admin_user):
    """Conditional update makes all workers read the same winning persisted secret."""
    from db import AdminUser, DB_TYPE, async_session_scope
    from sqlalchemy import func, or_, select, update

    secret = str(getattr(admin_user, "jwt_secret", None) or "").strip()
    if secret and secret != LEGACY_JWT_SECRET:
        return secret
    candidate = secrets.token_urlsafe(48)
    async with async_session_scope() as session:
        if DB_TYPE == "d1":
            await session.execute(
                "UPDATE admin_user SET jwt_secret = ? WHERE id = ? AND "
                "(jwt_secret IS NULL OR trim(jwt_secret) = '' OR jwt_secret = ?)",
                [candidate, 1, LEGACY_JWT_SECRET],
            )
            row = await session.query_one("SELECT jwt_secret FROM admin_user WHERE id = ?", [1])
            return str((row or {}).get("jwt_secret") or "").strip()
        await session.execute(update(AdminUser).where(
            AdminUser.id == 1,
            or_(AdminUser.jwt_secret.is_(None), func.trim(AdminUser.jwt_secret) == "",
                AdminUser.jwt_secret == LEGACY_JWT_SECRET),
        ).values(jwt_secret=candidate))
        await session.commit()
        return str((await session.execute(select(AdminUser.jwt_secret).where(AdminUser.id == 1))).scalar_one_or_none() or "").strip()


def require_initialized(app) -> None:
    if getattr(app.state, "admin_initialized", False) is not True or not getattr(app.state, "api_list", None):
        raise HTTPException(status_code=503, detail="Instance is not initialized. Complete administrator setup before using the API.")


async def refresh_initialization(app, *, force=False, repair_secret=False) -> bool:
    """Ready requests use memory only; unready workers retry at most once per second."""
    from db import DISABLE_DATABASE

    if not force and getattr(app.state, "admin_initialized", False) is True and getattr(app.state, "api_list", None):
        return True
    # Missing startup state is never treated as permission to use a global DB.
    if not force and not getattr(app.state, "initialization_checked", False):
        return False
    now = monotonic()
    if not force and now < getattr(app.state, "initialization_retry_at", 0):
        return False
    lock = getattr(app.state, "initialization_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.initialization_lock = lock
    async with lock:
        if not force and getattr(app.state, "admin_initialized", False) is True and getattr(app.state, "api_list", None):
            return True
        if not force and monotonic() < getattr(app.state, "initialization_retry_at", 0):
            return False
        app.state.initialization_checked = True
        app.state.initialization_retry_at = monotonic() + 1
        if DISABLE_DATABASE:
            app.state.admin_initialized = False
            app.state.needs_setup = True
            return False
        admin_user = await get_admin_user()
        if not admin_user or not admin_user.username or not admin_user.password_hash:
            app.state.admin_initialized = False
            app.state.needs_setup = True
            return False
        secret = (os.getenv("JWT_SECRET") or "").strip()
        if not secret:
            secret = (await _ensure_persisted_secret(admin_user) if repair_secret
                      else str(getattr(admin_user, "jwt_secret", None) or "").strip())
        if not secret or secret == LEGACY_JWT_SECRET:
            app.state.admin_initialized = False
            app.state.needs_setup = True
            return False
        set_jwt_secret(secret)
        if not getattr(app.state, "api_list", None):
            if not force:
                # A different worker may have completed bootstrap since this one started.
                from utils import load_config
                app.state.config, app.state.api_keys_db, app.state.api_list = await load_config(app)
            if not getattr(app.state, "api_list", None):
                app.state.admin_initialized = False
                app.state.needs_setup = True
                return False
        app.state.admin_initialized = True
        app.state.needs_setup = False
        return True


async def ensure_initialized(app) -> None:
    try:
        await refresh_initialization(app)
    except Exception:
        # A failed DB refresh must never become an authentication bypass.
        app.state.admin_initialized = False
    require_initialized(app)
