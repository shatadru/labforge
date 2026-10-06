"""LabForge UI routes - HTMX + Jinja2 server-rendered pages.

Handlers are synchronous because they call blocking subprocesses; FastAPI runs
them in its threadpool so the event loop stays responsive.
"""
import hashlib
import os
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import APP_VERSION, settings
from app.host_client import get_host_client
from app.vm_metrics import usage_for, usage_map
from app.host import capacity, fits_count, get_host_usage

router = APIRouter(tags=["ui"])

# Absolute path so the app works regardless of the process working directory.
_base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_templates_dir = os.path.join(_base_dir, "app", "templates")
templates = Jinja2Templates(directory=_templates_dir)


def _asset_version() -> str:
    """Cache-bust static assets by content, not by app version.

    ``?v=<APP_VERSION>`` changes only on a release, so a browser keeps serving
    a stale app.css/app.js after an edit. Hashing the asset bytes makes the URL
    change whenever a file does, while unchanged files stay cached.
    """
    digest = hashlib.md5()
    static_dir = os.path.join(_base_dir, "app", "static")
    try:
        names = sorted(os.listdir(static_dir))
    except OSError:
        names = []
    for name in names:
        if not name.endswith((".css", ".js")):
            continue
        try:
            with open(os.path.join(static_dir, name), "rb") as fh:
                digest.update(name.encode())
                digest.update(fh.read())
        except OSError:
            continue
    return f"{APP_VERSION}-{digest.hexdigest()[:10]}"


templates.env.globals.update(
    vm_name_prefix=settings.vm_name_prefix,
    templates_dir=settings.templates_dir,
    ssh_user=settings.default_ssh_user,
    app_version=APP_VERSION,
    asset_version=_asset_version(),
)


def host_context() -> dict:
    """Template context for the live host activity card."""
    from dataclasses import asdict
    return {**asdict(get_host_usage()), "storage_path": settings.vm_storage_path}


def capacity_context() -> dict:
    """Lab budget picture: allocated vs budget, and how many more fit."""
    client = get_host_client()
    cap = capacity(get_host_usage(), client.list_vms())
    default_size = {
        "vcpus": settings.default_vcpus,
        "memory_mb": settings.default_memory_mb,
        "disk_gb": settings.default_disk_gb,
    }
    cap["default_size"] = default_size
    cap["fits_default"] = fits_count(
        cap["remaining"], default_size["vcpus"], default_size["memory_mb"], default_size["disk_gb"]
    )
    return cap


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    client = get_host_client()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "vms": client.list_vms(),
            "usage_by_name": usage_map(client),
            "active_page": "dashboard",
            "chat_enabled": settings.chat_enabled and bool(settings.chat_ntfy_url),
            "chat_topic": settings.chat_topic,
        },
    )


@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    client = get_host_client()
    templates_list = client.list_templates()
    cap = capacity(get_host_usage(), client.list_vms())
    fits_by_name = {
        t.name: fits_count(cap["remaining"], t.vcpus, t.memory_mb, t.disk_gb)
        for t in templates_list
    }
    return templates.TemplateResponse(
        request,
        "templates.html",
        {
            "templates": templates_list,
            "active_page": "templates",
            "capacity": cap,
            "fits_by_name": fits_by_name,
            "tailscale_available": client.tailscale_available(),
            "images": client.list_imported_images(),
            "import_dir": str(settings.resolved_import_dir),
            "defaults": {
                "memory_mb": settings.default_memory_mb,
                "vcpus": settings.default_vcpus,
                "disk_gb": settings.default_disk_gb,
            },
        },
    )


@router.get("/vms/{vm_name}", response_class=HTMLResponse)
def vm_detail(request: Request, vm_name: str):
    client = get_host_client()
    vm = client.get_vm(vm_name)
    if vm is None:
        raise HTTPException(status_code=404, detail="VM not found")
    return templates.TemplateResponse(
        request,
        "vm_detail.html",
        {"vm": vm, "usage": usage_for(client, vm_name), "active_page": "dashboard"},
    )


# HTMX partial endpoints (swap fragments without full page load)

@router.get("/partials/vm-card/{vm_name}", response_class=HTMLResponse)
def vm_card_partial(request: Request, vm_name: str):
    """Single VM card - used by HTMX polling for auto-refresh."""
    client = get_host_client()
    vm = client.get_vm(vm_name)
    if vm is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/vm_card.html",
        {"vm": vm, "usage": usage_for(client, vm_name)},
    )


@router.get("/partials/vms", response_class=HTMLResponse)
def vms_partial(request: Request):
    """All VM cards - used by HTMX polling for dashboard refresh."""
    client = get_host_client()
    return templates.TemplateResponse(
        request,
        "partials/vm_grid.html",
        {"vms": client.list_vms(), "usage_by_name": usage_map(client)},
    )


@router.get("/partials/vm-usage/{vm_name}", response_class=HTMLResponse)
def vm_usage_partial(request: Request, vm_name: str):
    """Guest usage card, polled on the VM detail page."""
    client = get_host_client()
    vm = client.get_vm(vm_name)
    if vm is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/vm_usage.html",
        {"vm": vm, "usage": usage_for(client, vm_name)},
    )


@router.get("/partials/host", response_class=HTMLResponse)
def host_partial(request: Request):
    """Live host activity card - polled by HTMX on the dashboard."""
    return templates.TemplateResponse(
        request,
        "partials/host.html",
        host_context(),
    )


@router.get("/partials/vm-status/{vm_name}", response_class=HTMLResponse)
def vm_status_partial(request: Request, vm_name: str):
    """Status badge plus actions, polled on the VM detail page.

    Including the actions means a stopped VM shows Start without a reload.
    """
    vm = get_host_client().get_vm(vm_name)
    if vm is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/vm_status.html",
        {"vm": vm},
    )


@router.get("/partials/vm-console/{vm_name}", response_class=HTMLResponse)
def vm_console_partial(request: Request, vm_name: str):
    """Console and VNC buttons, polled so they follow the VM state."""
    vm = get_host_client().get_vm(vm_name)
    if vm is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/vm_console.html",
        {"vm": vm},
    )


@router.get("/partials/capacity", response_class=HTMLResponse)
def capacity_partial(request: Request):
    """Lab budget card - allocated vs budget and how many more fit."""
    return templates.TemplateResponse(
        request,
        "partials/capacity.html",
        {"capacity": capacity_context()},
    )
