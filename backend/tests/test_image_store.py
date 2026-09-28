"""Image upload helper tests: path safety, size cap, atomic install, cleanup."""
import asyncio

import pytest
from fastapi import HTTPException

from app.image_store import store_upload


class FakeUpload:
    def __init__(self, data: bytes, filename="x.qcow2"):
        self.filename = filename
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        chunk, self._data = self._data, b""
        return chunk


def _inspect_ok(_path):
    return {"format": "qcow2", "disk_gb": 10, "virtual_bytes": 1}


def _run(upload, directory, max_bytes=1024 ** 3, inspect=_inspect_ok):
    return asyncio.run(store_upload(upload, directory, max_bytes, inspect))


def test_rejects_empty_filename(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"x", filename=""), tmp_path)
    assert exc.value.status_code == 422


def test_rejects_dotfile(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"x", filename=".hidden.qcow2"), tmp_path)
    assert exc.value.status_code == 422


def test_reduces_traversal_to_basename(tmp_path):
    out = _run(FakeUpload(b"data", filename="../../evil.qcow2"), tmp_path)
    assert out["name"] == "evil.qcow2"
    assert (tmp_path / "evil.qcow2").exists()
    assert not (tmp_path.parent / "evil.qcow2").exists()


def test_adds_qcow2_when_no_suffix(tmp_path):
    out = _run(FakeUpload(b"data", filename="plain"), tmp_path)
    assert out["name"] == "plain.qcow2"


def test_rejects_unknown_suffix(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"data", filename="notes.txt"), tmp_path)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("suffix", [".qcow2", ".qcow", ".img", ".raw"])
def test_accepts_known_suffixes(tmp_path, suffix):
    out = _run(FakeUpload(b"data", filename=f"disk{suffix}"), tmp_path)
    assert out["name"] == f"disk{suffix}"


def test_size_boundary_equal_ok_and_over_is_413(tmp_path):
    _run(FakeUpload(b"x" * 8, filename="ok.qcow2"), tmp_path, max_bytes=8)
    assert (tmp_path / "ok.qcow2").exists()
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"x" * 9, filename="big.qcow2"), tmp_path, max_bytes=8)
    assert exc.value.status_code == 413
    assert not (tmp_path / "big.qcow2").exists()


def test_invalid_image_cleans_temp_and_raises(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"not-qcow2", filename="bad.qcow2"), tmp_path,
             inspect=lambda _p: None)
    assert exc.value.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_duplicate_returns_409_before_inspect(tmp_path):
    (tmp_path / "dup.qcow2").write_bytes(b"existing")
    called = []

    def inspect(path):
        called.append(path)
        return _inspect_ok(path)

    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"data", filename="dup.qcow2"), tmp_path, inspect=inspect)
    assert exc.value.status_code == 409
    assert called == []


def test_mkdir_failure_is_500(tmp_path, monkeypatch):
    from pathlib import Path

    def boom(self, *a, **k):
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(HTTPException) as exc:
        _run(FakeUpload(b"data"), tmp_path / "sub")
    assert exc.value.status_code == 500
