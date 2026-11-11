"""Execute the real static-route block without importing main or production state."""
import ast
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


@pytest.fixture
def static_app(tmp_path, monkeypatch):
    root = tmp_path / "gateway"
    static = root / "static"
    (static / "assets").mkdir(parents=True)
    (static / "icons").mkdir()
    (static / "index.html").write_text("<html>test application</html>")
    (static / "assets" / "app.js").write_text("/* public asset */")
    (static / "icons" / "logo.txt").write_text("public icon")
    (static / "robots.txt").write_text("User-agent: *")
    (root / "outside.txt").write_text("must not be disclosed")
    (static / "escape.txt").symlink_to(root / "outside.txt")
    (static / "assets" / "escape.txt").symlink_to(root / "outside.txt")
    (static / "icons" / "escape.txt").symlink_to(root / "outside.txt")
    (static / "loop").symlink_to(static / "loop")
    monkeypatch.chdir(root)

    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text())
    # Preserve actual registration order and definitions, without app startup.
    nodes = []
    active = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STATIC_ROOT" for t in node.targets):
            active = True
        if active:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                break  # __main__ entry point
            nodes.append(node)
    assert nodes
    app = FastAPI()
    namespace = {"app": app, "Path": Path, "StaticFiles": StaticFiles, "FileResponse": FileResponse,
                 "JSONResponse": JSONResponse, "HTTPException": HTTPException}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return app, static


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/..%2foutside.txt", "/%2e%2e/outside.txt", "/%2e%2e%2foutside.txt",
    "/assets/..%2f..%2foutside.txt", "/icons/..%2f..%2foutside.txt",
    "/settings/..%2f..%2foutside.txt", "/%5c..%5coutside.txt", "/%2foutside.txt",
    "/%00.txt", "/escape.txt", "/assets/escape.txt", "/icons/escape.txt", "/loop",
])
async def test_unauthenticated_traversal_and_symlinks_are_rejected(static_app, path):
    app, _ = static_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert "must not be disclosed" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path,content", [
    ("/", "<html>test application</html>"), ("/index.html", "<html>test application</html>"),
    ("/setup", "<html>test application</html>"),
    ("/settings", "<html>test application</html>"), ("/unknown-spa-route", "<html>test application</html>"),
    ("/assets/app.js", "/* public asset */"), ("/icons/logo.txt", "public icon"),
    ("/robots.txt", "User-agent: *"),
])
async def test_public_assets_and_spa_remain_available(static_app, path, content):
    app, _ = static_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.text == content
    if path in ("/", "/index.html", "/setup", "/settings", "/unknown-spa-route"):
        assert "no-store" in response.headers["cache-control"]
    if path == "/assets/app.js":
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_index_symlink_cannot_escape(static_app):
    app, static = static_app
    (static / "index.html").unlink()
    (static / "index.html").symlink_to(static.parent / "outside.txt")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        response = await client.get("/setup")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_asset_conditional_cache_and_missing_file(static_app):
    app, _ = static_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        first = await client.get("/assets/app.js")
        cached = await client.get("/assets/app.js", headers={"If-None-Match": first.headers["etag"]})
        missing = await client.get("/assets/missing.js")
    assert cached.status_code == 304
    assert cached.headers["cache-control"] == first.headers["cache-control"]
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")


def test_static_mounts_precede_catch_all(static_app):
    app, _ = static_app
    paths = [route.path for route in app.routes]
    assert paths.index("/assets") < paths.index("/{path:path}")
    assert paths.index("/icons") < paths.index("/{path:path}")
