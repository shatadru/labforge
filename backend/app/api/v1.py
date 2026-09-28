"""LabForge API v1 - REST endpoints.

Handlers are deliberately synchronous: they call blocking subprocesses
(virsh/qemu-img), so FastAPI runs them in its threadpool and the event loop
stays free for other requests and WebSockets.
"""
import logging
import re
from typing import Optional

import hmac
from fastapi import APIRouter, Depends, File, HTTPException, Path as PathParam, Query, UploadFile, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.virsh_client import VMInfo, TemplateInfo
from app.host_client import HostError, get_host_client, host_mode
from app.vm_metrics import get_cached_usage
from app.image_store import store_upload
from app.host import (
    HostUsage,
    capacity,
    check_resources,
    fits_count,
    get_host_usage,
)
from dataclasses import asdict

logger = logging.getLogger("labforge.api")

_SNAPSHOT_NAME = r"^[a-zA-Z0-9_-]+$"


# ---------- Auth dependency ----------

def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a, b)


def _require_api_key(request: Request) -> str:
    """Validate the API bearer token on control-plane endpoints.

    Reads the X-API-Key header (or Authorization: Bearer) and compares it
    to the configured AGENT_TOKEN using hmac.compare_digest for timing safety.
    Returns the token on success, raises 401 otherwise.
    """
    token = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not settings.agent_token or not hmac.compare_digest(token, settings.agent_token):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return token


# ---------- Routes ----------

router = APIRouter(prefix="", tags=["vms"], dependencies=[Depends(_require_api_key)])

# ---------- Models ----------

class ProvisionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=63,
                      description="VM name; case and separators are normalised")
    template: Optional[str] = Field(default=None, description="Template name")
    image: Optional[str] = Field(default=None, description="Uploaded image file name")
    memory_mb: Optional[int] = Field(default=None, ge=512, le=65536)
    vcpus: Optional[int] = Field(default=None, ge=1, le=32)
    disk_gb: Optional[int] = Field(default=None, ge=1, le=1024)
    enable_tailscale: bool = Field(default=True, description="Join the VM to the Tailscale tailnet")
    vm_user: Optional[str] = Field(default=None, min_length=1, max_length=32,
                                   description="Guest login user; lowercased")
    vm_password: Optional[str] = Field(default=None, min_length=1, max_length=128,
                                       description="Guest password; omit for SSH-key-only auth")
    os_variant: Optional[str] = Field(default=None, max_length=64,
                                      description="Override the osinfo variant, for example debian12")


class SnapshotRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64,
                      description="Snapshot name; separators are normalised")


class VMResponse(BaseModel):
    name: str
    state: str
    ip: Optional[str] = None
    tailscale_ip: Optional[str] = None
    user: Optional[str] = None
    tailscale_enabled: bool = False
    vcpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0
    snapshots: list[str] = []
    status_class: str = ""

    @classmethod
    def from_vm(cls, vm: VMInfo) -> "VMResponse":
        return cls(
            name=vm.name,
            state=vm.state,
            ip=vm.ip,
            tailscale_ip=vm.tailscale_ip,
            user=vm.user,
            tailscale_enabled=vm.tailscale_enabled,
            vcpus=vm.vcpus,
            memory_mb=vm.memory_mb,
            disk_gb=vm.disk_gb,
            snapshots=vm.snapshots or [],
            status_class=vm.status_class,
        )


class ActionResponse(BaseModel):
    success: bool
    message: str


def _require_vm(client, vm_name: str):
    """Return the lab VM or raise 404. Keeps old VMs invisible."""
    vm = client.get_vm(vm_name)
    if vm is None:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


def _action(result, message: str) -> ActionResponse:
    """Turn a VirshResult into an ActionResponse, or raise on failure."""
    if not result.success:
        raise HTTPException(status_code=500, detail=result.stderr or "Host operation failed")
    return ActionResponse(success=True, message=message)


# ---------- VM Endpoints ----------

@router.get("/vms", response_model=list[VMResponse])
def list_vms():
    return [VMResponse.from_vm(vm) for vm in get_host_client().list_vms()]


@router.get("/vms/{vm_name}", response_model=VMResponse)
def get_vm(vm_name: str):
    client = get_host_client()
    return VMResponse.from_vm(_require_vm(client, vm_name))


@router.get("/vms/{vm_name}/usage", response_model=dict)
def vm_usage(vm_name: str):
    """Guest resource usage via the QEMU guest agent.

    Served from the background sampler's cache so the request never blocks on
    a slow or unavailable agent. ``available: false`` means it is not collected
    yet (for example a stopped VM, or a guest without a running agent).
    """
    _require_vm(get_host_client(), vm_name)
    usage = get_cached_usage(vm_name)
    if usage is None:
        return {"name": vm_name, "available": False, "reason": "collecting guest metrics"}
    return asdict(usage)


@router.post("/vms", response_model=ActionResponse)
def provision_vm(req: ProvisionRequest):
    client = get_host_client()
    if bool(req.template) == bool(req.image):
        raise HTTPException(status_code=422,
                            detail="Provide exactly one of 'template' or 'image'")
    try:
        req.name = client.provision_name(req.name, req.template or req.image)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if req.vm_user:
        req.vm_user = req.vm_user.strip().lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", req.vm_user):
            raise HTTPException(
                status_code=422,
                detail="Login user must start with a letter or underscore and use only "
                       "lowercase letters, digits, underscore or hyphen",
            )
    if client.vm_exists(req.name):
        raise HTTPException(status_code=409, detail="VM already exists")

    result = client.create_vm(
        name=req.name,
        template_name=req.template,
        image=req.image,
        memory_mb=req.memory_mb,
        vcpus=req.vcpus,
        disk_gb=req.disk_gb,
        enable_tailscale=req.enable_tailscale,
        vm_user=req.vm_user,
        vm_password=req.vm_password,
        os_variant=req.os_variant,
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.stderr)
    return ActionResponse(success=True, message=result.stdout or "VM provisioned", name=req.name)


@router.post("/vms/{vm_name}/start", response_model=ActionResponse)
def start_vm(vm_name: str):
    client = get_host_client()
    vm = _require_vm(client, vm_name)
    if vm.state.lower() == "running":
        return ActionResponse(success=True, message="Already running")
    return _action(client.start_vm(vm_name), "VM starting")


@router.post("/vms/{vm_name}/stop", response_model=ActionResponse)
def stop_vm(vm_name: str):
    client = get_host_client()
    vm = _require_vm(client, vm_name)
    if vm.state.lower() == "shut off":
        return ActionResponse(success=True, message="Already stopped")
    return _action(client.stop_vm(vm_name), "VM shutting down")


@router.post("/vms/{vm_name}/reboot", response_model=ActionResponse)
def reboot_vm(vm_name: str):
    client = get_host_client()
    vm = _require_vm(client, vm_name)
    if vm.state.lower() == "shut off":
        return ActionResponse(success=True, message="Already stopped")
    return _action(client.reboot_vm(vm_name), "VM rebooting")


@router.post("/vms/{vm_name}/reset", response_model=ActionResponse)
def reset_vm(vm_name: str):
    client = get_host_client()
    vm = _require_vm(client, vm_name)
    if vm.state.lower() == "shut off":
        return ActionResponse(success=True, message="Already stopped")
    return _action(client.reset_vm(vm_name), "VM resetting")


@router.delete("/vms/{vm_name}", response_model=ActionResponse)
def delete_vm(vm_name: str):
    client = get_host_client()
    vm = _require_vm(client, vm_name)
    return _action(client.delete_vm(vm_name), "VM deleting")


@router.get("/host/usage", response_model=HostUsage)
def host_usage():
    """Host resources (CPU, memory, disk, load) for the LabForge control plane."""
    return get_host_usage()


@router.get("/host/check", response_model=dict)
def host_check():
    """Detailed host readiness checks."""
    return check_resources()


@router.get("/host/capacity", response_model=dict)
def host_capacity():
    """Host resource capacity (hard limits vs. reserved)."""
    return capacity()


@router.get("/templates", response_model=list[TemplateInfo])
def list_templates():
    return get_host_client().list_templates()


# ---------- Image Endpoints ----------

@router.get("/images", response_model=list[dict])
def list_images():
    return get_host_client().list_images()


@router.post("/images", response_model=dict)
def upload_image(file: UploadFile = File(...), name: str = Query(None, alias="name")):
    """Upload a disk image for use with the "image" provisioning mode."""
    return store_upload(file.filename, file.file, name)


@router.delete("/images/{image_name}", response_model=ActionResponse)
def delete_image(image_name: str):
    return _action(get_host_client().delete_image(image_name), "Image deleting")


# ---------- Snapshot Endpoints ----------

@router.get("/vms/{vm_name}/snapshots", response_model=list[str])
def list_snapshots(vm_name: str):
    return get_host_client().list_snapshots(vm_name)


@router.post("/vms/{vm_name}/snapshots", response_model=ActionResponse)
def create_snapshot(vm_name: str, req: SnapshotRequest):
    return _action(get_host_client().create_snapshot(vm_name, req.name), "Snapshot creating")


@router.delete("/vms/{vm_name}/snapshots/{snapshot_name}", response_model=ActionResponse)
def delete_snapshot(vm_name: str, snapshot_name: str):
    return _action(get_host_client().delete_snapshot(vm_name, snapshot_name), "Snapshot deleting")


@router.post("/vms/{vm_name}/snapshots/{snapshot_name}/revert", response_model=ActionResponse)
def revert_snapshot(vm_name: str, snapshot_name: str):
    return _action(get_host_client().revert_snapshot(vm_name, snapshot_name), "Snapshot reverting")