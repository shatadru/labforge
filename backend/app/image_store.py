"""Streaming upload of qcow2 images into an import directory.

Shared by the control plane (HOST_MODE=local, writing on this host) and the
agent (writing on the KVM host). Raises HTTPException so both can surface the
same errors.
"""
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.virsh_client import IMAGE_SUFFIXES


async def store_upload(upload, directory: Path, max_bytes: int, inspect) -> dict:
    """Stream ``upload`` into ``directory`` and validate it as a qcow2 image.

    ``inspect`` is called with the temporary path and must return image metadata
    or None. Installation is atomic and exclusive, so two concurrent uploads of
    the same name cannot overwrite each other.
    """
    raw_name = Path(upload.filename or "").name
    if not raw_name or raw_name.startswith("."):
        raise HTTPException(status_code=422, detail="A file name is required")
    suffix = Path(raw_name).suffix.lower()
    if suffix and suffix not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=422,
                            detail="Upload a .qcow2, .qcow, .img or .raw image")
    name = raw_name if suffix else f"{raw_name}.qcow2"

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot create import directory: {exc}")

    dest = directory / name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"An image named '{name}' already exists")

    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".part")
    tmp = Path(tmp_name)
    written = 0
    info = None
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload is larger than the {max_bytes // (1024 ** 3)} GB limit",
                    )
                fh.write(chunk)

        info = await run_in_threadpool(inspect, tmp)
        if not info:
            raise HTTPException(status_code=422, detail="That file is not a valid qcow2 image")

        try:
            os.link(tmp, dest)
        except FileExistsError as exc:
            raise HTTPException(status_code=409,
                                detail=f"An image named '{name}' already exists") from exc
    finally:
        tmp.unlink(missing_ok=True)

    return {"name": dest.name, **info}
