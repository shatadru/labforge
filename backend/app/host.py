"""Host metrics, the lab resource budget, and provisioning preflight checks.

No external dependencies - CPU/memory come from /proc, disk from ``shutil``.
The *budget* is the share of host capacity LabForge is allowed to hand out to
lab VMs (``RESOURCE_BUDGET_PERCENT``, default 50%). Templates decide how big an
individual VM is; the budget caps the total.
"""
import os
import shutil
import time
from dataclasses import dataclass

from app.config import settings


@dataclass
class HostUsage:
    cpu_percent: float
    mem_total_mb: int
    mem_available_mb: int
    mem_used_mb: int
    mem_percent: float
    load_1m: float
    load_5m: float
    load_15m: float
    cpu_cores: int
    disk_total_gb: int
    disk_available_gb: int
    disk_used_gb: int
    disk_percent: float


def _read_meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                try:
                    info[key] = int(parts[1])
                except ValueError:
                    pass
    return info


def get_host_usage() -> HostUsage:
    meminfo = _read_meminfo()
    mem_total_kb = meminfo.get("MemTotal", 0)
    mem_avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
    mem_total_mb = mem_total_kb // 1024
    mem_avail_mb = mem_avail_kb // 1024
    mem_used_mb = mem_total_mb - mem_avail_mb

    # CPU usage: sample /proc/stat over a short interval
    with open("/proc/stat") as f:
        fields1 = list(map(int, f.readline().split()[1:]))
    idle1, total1 = fields1[3], sum(fields1)

    time.sleep(0.2)

    with open("/proc/stat") as f:
        fields2 = list(map(int, f.readline().split()[1:]))
    idle2, total2 = fields2[3], sum(fields2)

    d_idle = idle2 - idle1
    d_total = total2 - total1
    cpu_percent = round((1.0 - d_idle / max(d_total, 1)) * 100, 1) if d_total else 0.0

    load_1m = load_5m = load_15m = 0.0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load_1m, load_5m, load_15m = float(parts[0]), float(parts[1]), float(parts[2])
    except (ValueError, IndexError):
        pass

    cpu_cores = os.cpu_count() or 1

    storage_path = settings.vm_storage_path
    try:
        disk_usage = shutil.disk_usage(storage_path)
    except OSError:
        disk_usage = shutil.disk_usage("/")
    disk_total_gb = disk_usage.total // (1024 ** 3)
    disk_avail_gb = disk_usage.free // (1024 ** 3)
    disk_used_gb = disk_usage.used // (1024 ** 3)
    disk_percent = round(disk_usage.used / max(disk_usage.total, 1) * 100, 1)

    return HostUsage(
        cpu_percent=cpu_percent,
        mem_total_mb=mem_total_mb,
        mem_available_mb=mem_avail_mb,
        mem_used_mb=mem_used_mb,
        mem_percent=round(mem_used_mb / max(mem_total_mb, 1) * 100, 1),
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
        cpu_cores=cpu_cores,
        disk_total_gb=disk_total_gb,
        disk_available_gb=disk_avail_gb,
        disk_used_gb=disk_used_gb,
        disk_percent=disk_percent,
    )


# ---------------------------------------------------------------- lab budget

@dataclass
class LabBudget:
    percent: int
    vcpus: int
    memory_mb: int
    disk_gb: int


@dataclass
class LabUsage:
    vms: int = 0
    vcpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0


def budget_for(usage: HostUsage, percent: int = None) -> LabBudget:
    """Total capacity LabForge may allocate, as a share of the host."""
    percent = settings.resource_budget_percent if percent is None else percent
    return LabBudget(
        percent=percent,
        vcpus=max(1, usage.cpu_cores * percent // 100),
        memory_mb=usage.mem_total_mb * percent // 100,
        disk_gb=usage.disk_total_gb * percent // 100,
    )


def usage_of(vms) -> LabUsage:
    """Resources already allocated to lab VMs (running or stopped)."""
    vms = list(vms or [])
    return LabUsage(
        vms=len(vms),
        vcpus=sum(int(getattr(v, "vcpus", 0) or 0) for v in vms),
        memory_mb=sum(int(getattr(v, "memory_mb", 0) or 0) for v in vms),
        disk_gb=sum(int(getattr(v, "disk_gb", 0) or 0) for v in vms),
    )


def fits_count(remaining: dict, vcpus: int, memory_mb: int, disk_gb: int) -> int:
    """How many VMs of this size fit in the remaining budget."""
    dims = []
    if vcpus:
        dims.append(remaining["vcpus"] // vcpus)
    if memory_mb:
        dims.append(remaining["memory_mb"] // memory_mb)
    if disk_gb:
        dims.append(remaining["disk_gb"] // disk_gb)
    return max(0, min(dims)) if dims else 0


def capacity(usage: HostUsage = None, vms=None, percent: int = None) -> dict:
    """Full capacity picture: host, budget, allocated, remaining."""
    usage = usage or get_host_usage()
    vms = list(vms or [])
    budget = budget_for(usage, percent)
    used = usage_of(vms)

    remaining = {
        "vcpus": max(0, budget.vcpus - used.vcpus),
        "memory_mb": max(0, budget.memory_mb - used.memory_mb),
        "disk_gb": max(0, budget.disk_gb - used.disk_gb),
    }

    def pct(part: int, whole: int) -> float:
        return round(part / whole * 100, 1) if whole else 0.0

    return {
        "budget_percent": budget.percent,
        "host": {
            "cpu_cores": usage.cpu_cores,
            "memory_mb": usage.mem_total_mb,
            "memory_available_mb": usage.mem_available_mb,
            "disk_gb": usage.disk_total_gb,
            "disk_available_gb": usage.disk_available_gb,
        },
        "budget": {"vcpus": budget.vcpus, "memory_mb": budget.memory_mb, "disk_gb": budget.disk_gb},
        "used": {"vms": used.vms, "vcpus": used.vcpus, "memory_mb": used.memory_mb, "disk_gb": used.disk_gb},
        "remaining": remaining,
        "percent_used": {
            "vcpus": pct(used.vcpus, budget.vcpus),
            "memory": pct(used.memory_mb, budget.memory_mb),
            "disk": pct(used.disk_gb, budget.disk_gb),
        },
    }


def check_resources(memory_mb: int, vcpus: int, disk_gb: int,
                    usage: HostUsage = None, vms=None, percent: int = None) -> dict:
    """Can we provision one more VM of this size within the lab budget?

    Returns ``{ok, reasons, fits, capacity}``. ``fits`` is how many VMs of this
    exact size the remaining budget still holds.
    """
    usage = usage or get_host_usage()
    cap = capacity(usage, vms, percent)
    rem = cap["remaining"]
    reasons: list[str] = []

    if vcpus > rem["vcpus"]:
        reasons.append(
            f"CPU budget: {rem['vcpus']} of {cap['budget']['vcpus']} vCPUs free, {vcpus} requested"
        )
    if memory_mb > rem["memory_mb"]:
        reasons.append(
            f"Memory budget: {rem['memory_mb']}MB of {cap['budget']['memory_mb']}MB free, {memory_mb}MB requested"
        )
    if disk_gb > rem["disk_gb"]:
        reasons.append(
            f"Disk budget: {rem['disk_gb']}GB of {cap['budget']['disk_gb']}GB free, {disk_gb}GB requested"
        )

    # Hard safety: never commit more than the host can actually give right now.
    mem_needed = memory_mb + settings.resource_memory_overhead_mb
    if mem_needed > usage.mem_available_mb:
        reasons.append(
            f"Host free memory low: {usage.mem_available_mb}MB free, {mem_needed}MB needed"
        )
    disk_needed = disk_gb + settings.resource_disk_overhead_gb
    if disk_needed > usage.disk_available_gb:
        reasons.append(
            f"Host free disk low: {usage.disk_available_gb}GB free, {disk_needed}GB needed"
        )

    return {
        "ok": not reasons,
        "reasons": reasons,
        "fits": fits_count(rem, vcpus, memory_mb, disk_gb),
        "capacity": cap,
    }
