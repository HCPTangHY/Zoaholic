"""Plugin uploads remain executable for admins, but cannot escape their install root."""
import io
import json
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from routes import plugins


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, body in entries:
            output.writestr(name, body)
    return stream.getvalue()


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    calls = []
    result = SimpleNamespace(error=None)

    def load(path):
        calls.append(Path(path))
        assert Path(path).is_file()
        return result

    monkeypatch.setattr(plugins, "PLUGINS_DIR", root)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace(load_plugin=load))
    monkeypatch.setattr(plugins, "_get_plugin_info_dict", lambda info: {"name": "test"})
    return root, calls, result


async def upload(filename, body=b"PLUGIN_INFO = {}\n"):
    file = UploadFile(filename=filename, file=io.BytesIO(body))
    try:
        return await plugins.upload_plugin(file, "admin")
    finally:
        await file.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../outside.py", "/outside.py", "a/b.py", "a\\b.py", "C:\\bad.py", "C:bad.py", "..\\bad.zip", "../bad.zip", ".py", ".zip", "a\x00.py", "a\n.py", "", " .py", "a.py "])
async def test_upload_filename_rejected_before_writing_or_loading(install_env, filename):
    root, calls, _ = install_env
    with pytest.raises(HTTPException) as exc:
        await upload(filename)
    assert exc.value.status_code == 400
    assert not calls
    assert list(root.iterdir()) == []
    assert not (root.parent / "outside.py").exists()


@pytest.mark.asyncio
async def test_normal_python_upload_and_overwrite_remain_supported(install_env):
    root, calls, _ = install_env
    target = root / "plugin.py"
    target.write_bytes(b"previous")
    response = await upload("plugin.py", b"replacement")
    assert json.loads(response.body)["success"] is True
    assert target.read_bytes() == b"replacement"
    assert calls == [target]
    assert list(root.iterdir()) == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize("entries,filename,expected", [
    ([("package/__init__.py", b"init"), ("package/data.txt", b"data")], "bundle.zip", "package/__init__.py"),
    ([("main.py", b"code"), ("data.txt", b"data")], "bundle.zip", "bundle/main.py"),
    ([("plugin.py", b"code")], "bundle.zip", "bundle/plugin.py"),
    ([("pkg/foo..bar.py", b"code")], "bundle.zip", "pkg/foo..bar.py"),
])
async def test_normal_zip_uploads_keep_layout_and_call_loader(install_env, entries, filename, expected):
    root, calls, _ = install_env
    response = await upload(filename, archive(entries))
    assert json.loads(response.body)["success"] is True
    assert calls == [root / expected]
    assert all(not item.name.startswith("_upload_") for item in root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("member", ["../escape.py", "/escape.py", "pkg/../../escape.py", "pkg\\escape.py", "C:escape.py", "pkg//escape.py", "pkg/./escape.py", "pkg/../escape.py"])
async def test_zip_paths_rejected_before_install(install_env, member):
    root, calls, _ = install_env
    with pytest.raises(HTTPException) as exc:
        await upload("bundle.zip", archive([(member, b"invalid")]))
    assert exc.value.status_code == 400
    assert list(root.iterdir()) == []
    assert not calls
    assert not (root.parent / "escape.py").exists()


@pytest.mark.asyncio
async def test_zip_symlink_and_special_file_entries_rejected(install_env):
    root, calls, _ = install_env
    for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
        entry = zipfile.ZipInfo("pkg/link.py")
        entry.create_system = 3
        entry.external_attr = (mode | 0o777) << 16
        with pytest.raises(HTTPException) as exc:
            await upload("bundle.zip", archive([(entry, b"../../outside")]))
        assert exc.value.status_code == 400
        assert list(root.iterdir()) == []
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [False, True])
async def test_existing_target_symlink_is_not_followed(install_env, directory):
    root, calls, _ = install_env
    outside = root.parent / "outside"
    if directory:
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (root / "package").symlink_to(outside, target_is_directory=True)
        filename, body = "bundle.zip", archive([("package/main.py", b"code")])
    else:
        outside.write_text("keep")
        (root / "plugin.py").symlink_to(outside)
        filename, body = "plugin.py", b"code"
    with pytest.raises(HTTPException) as exc:
        await upload(filename, body)
    assert exc.value.status_code == 400
    assert not calls
    assert (outside / "keep.txt" if directory else outside).read_text() == "keep"


@pytest.mark.asyncio
async def test_symlink_in_install_root_ancestors_rejected(install_env, monkeypatch):
    root, calls, _ = install_env
    outside = root.parent / "outside"
    outside.mkdir()
    link = root.parent / "linked"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", link / "plugins")
    with pytest.raises(HTTPException) as exc:
        await upload("plugin.py")
    assert exc.value.status_code == 400
    assert not (outside / "plugins").exists()
    assert not calls


@pytest.mark.asyncio
async def test_failed_load_restores_original_file(install_env):
    root, calls, result = install_env
    target = root / "plugin.py"
    target.write_bytes(b"previous contents")
    result.error = "syntax failure"
    with pytest.raises(HTTPException) as exc:
        await upload("plugin.py", b"bad replacement")
    assert exc.value.status_code == 400
    assert target.read_bytes() == b"previous contents"
    assert calls == [target]
    assert list(root.iterdir()) == [target]


@pytest.mark.asyncio
async def test_failed_load_removes_only_new_file(install_env):
    root, _, result = install_env
    result.error = "failure"
    with pytest.raises(HTTPException):
        await upload("plugin.py")
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_zip_load_restores_directory_without_following_old_links(install_env):
    root, _, result = install_env
    target = root / "package"
    target.mkdir()
    (target / "old.py").write_text("original")
    external = root.parent / "external"
    external.write_text("untouched")
    (target / "old-link").symlink_to(external)
    result.error = "failed"
    with pytest.raises(HTTPException):
        await upload("bundle.zip", archive([("package/main.py", b"replacement")]))
    assert (target / "old.py").read_text() == "original"
    assert (target / "old-link").is_symlink()
    assert external.read_text() == "untouched"
    assert not (target / "main.py").exists()


@pytest.mark.asyncio
async def test_loader_exception_restores_old_file(install_env, monkeypatch):
    root, _, _ = install_env
    target = root / "plugin.py"
    target.write_text("old")

    def fail(path):
        raise RuntimeError("loader failed")

    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace(load_plugin=fail))
    with pytest.raises(HTTPException) as exc:
        await upload("plugin.py")
    assert exc.value.status_code == 500
    assert target.read_text() == "old"


@pytest.mark.asyncio
async def test_loader_declines_module_without_losing_old_file(install_env, monkeypatch):
    root, _, _ = install_env
    target = root / "plugin.py"
    target.write_text("old")
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace(load_plugin=lambda path: None))
    with pytest.raises(HTTPException) as exc:
        await upload("plugin.py")
    assert exc.value.status_code == 400
    assert target.read_text() == "old"


@pytest.mark.asyncio
async def test_upload_and_archive_limits_before_loading(install_env, monkeypatch):
    root, calls, _ = install_env
    monkeypatch.setattr(plugins, "MAX_PLUGIN_UPLOAD_BYTES", 8)
    with pytest.raises(HTTPException) as exc:
        await upload("plugin.py", b"x" * 9)
    assert exc.value.status_code == 413
    monkeypatch.setattr(plugins, "MAX_PLUGIN_UPLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(plugins, "MAX_PLUGIN_EXPANDED_BYTES", 8)
    with pytest.raises(HTTPException) as exc:
        await upload("bundle.zip", archive([("main.py", b"x" * 10000)]))
    assert exc.value.status_code == 413
    monkeypatch.setattr(plugins, "MAX_PLUGIN_EXPANDED_BYTES", 1024)
    monkeypatch.setattr(plugins, "MAX_PLUGIN_ZIP_ENTRIES", 1)
    with pytest.raises(HTTPException) as exc:
        await upload("bundle.zip", archive([("main.py", b"code"), ("data", b"data")]))
    assert exc.value.status_code == 413
    assert not calls
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not zip", archive([]), archive([("readme.txt", b"no code")]), archive([("pkg/main.py", b"code"), ("pkg/main.py/child", b"conflict")])])
async def test_invalid_zip_never_replaces_existing_plugin(install_env, body):
    root, calls, _ = install_env
    original = root / "bundle"
    original.mkdir()
    (original / "keep.py").write_text("keep")
    with pytest.raises(HTTPException) as exc:
        await upload("bundle.zip", body)
    assert exc.value.status_code == 400
    assert (original / "keep.py").read_text() == "keep"
    assert calls == []


def test_package_loader_accepts_init_and_uses_package_identity(tmp_path, monkeypatch):
    from core.plugins import loader as module

    package = tmp_path / "package"
    package.mkdir()
    entry = package / "__init__.py"
    entry.write_text("not executed")
    calls = []

    class FakeLoader:
        def exec_module(self, loaded):
            calls.append(loaded.__name__)

    def fake_spec(name, path):
        assert Path(path) == entry
        return SimpleNamespace(name=name, loader=FakeLoader())

    monkeypatch.setattr(module.importlib.util, "spec_from_file_location", fake_spec)
    monkeypatch.setattr(module.importlib.util, "module_from_spec", lambda spec: SimpleNamespace(__name__=spec.name))
    loader = module.PluginLoader([str(tmp_path)])
    module_name = "zoaholic_plugins.package"
    previous = sys.modules.get(module_name)
    try:
        info = loader.load_from_file(str(entry))
        assert info is not None and info.error is None
        assert info.name == "package"
        assert loader.get_plugin("package") is info
        assert calls == [module_name]
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


@pytest.mark.asyncio
async def test_duplicate_archive_names_and_case_sensitive_internal_dots(install_env):
    root, calls, _ = install_env
    with pytest.warns(UserWarning, match="Duplicate name"):
        body = archive([("main.py", b"first"), ("main.py", b"second")])
    with pytest.raises(HTTPException) as exc:
        await upload("bundle.zip", body)
    assert exc.value.status_code == 400
    assert list(root.iterdir()) == []
    assert not calls
    await upload("plugin..version.py", b"valid")
    assert calls == [root / "plugin..version.py"]
