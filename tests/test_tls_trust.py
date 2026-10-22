"""Offline guards for the certifi trust-store floor and HTTPS file fetching."""
import asyncio
from pathlib import Path
import re
import tomllib
from unittest.mock import patch

from core import file_utils


ROOT = Path(__file__).resolve().parents[1]
MIN_CERTIFI_VERSION = (2026, 10, 19)


def test_certifi_dependency_floor_and_lock_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    project_certifi = [d for d in project["project"]["dependencies"] if d.startswith("certifi")]
    requirements_certifi = [d.strip() for d in requirements if d.strip().startswith("certifi")]
    assert len(project_certifi) == 1
    assert requirements_certifi == project_certifi
    match = re.fullmatch(r"certifi>=(\d+)\.(\d+)\.(\d+)", project_certifi[0])
    assert match is not None, "certifi must have an explicit security minimum"
    minimum = tuple(map(int, match.groups()))
    assert minimum >= MIN_CERTIFI_VERSION

    packages = {package["name"]: package for package in lock["package"]}
    certifi = packages["certifi"]
    assert tuple(map(int, certifi["version"].split("."))) >= minimum
    assert certifi["source"] == {"registry": "https://pypi.org/simple"}
    for artifact in [certifi["sdist"], *certifi["wheels"]]:
        assert artifact["url"].startswith("https://files.pythonhosted.org/packages/")
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["hash"])

    root_package = packages[project["project"]["name"].lower()]
    assert root_package["version"] == project["project"]["version"]
    locked_requirement = [
        d for d in root_package["metadata"]["requires-dist"] if d["name"] == "certifi"
    ]
    assert locked_requirement == [{"name": "certifi", "specifier": project_certifi[0][len("certifi"):]}]


def test_shared_fetch_client_verifies_tls_and_is_closed(monkeypatch):
    # No network or application startup: only construct the HTTPX client.
    monkeypatch.setattr(file_utils, "_shared_fetch_client", None)
    monkeypatch.setattr(file_utils, "_shared_fetch_client_lock", asyncio.Lock())
    transport_factory = file_utils.httpx.AsyncHTTPTransport

    async def check_client():
        with patch.object(file_utils.httpx, "AsyncHTTPTransport", wraps=transport_factory) as factory:
            try:
                client = await file_utils._get_shared_fetch_client()
                assert factory.call_args.kwargs["verify"] is True
                assert await file_utils._get_shared_fetch_client() is client
                factory.assert_called_once()
            finally:
                await file_utils.close_shared_fetch_client()
            assert client.is_closed
            assert file_utils._shared_fetch_client is None

    asyncio.run(check_client())
