"""Guest usage metrics, collected through the QEMU guest agent.

CPU and disk come from native agent commands (``guest-get-cpustats`` and
``guest-get-fsinfo``); memory used is the one value the agent does not expose,
so ``/proc/meminfo`` is read once through ``guest-exec``. A background sampler
keeps a small cache so UI polls never block on a slow or unavailable agent.

The numbers describe what the *guest* sees, not the host: this is deliberately
separate from :mod:`app.host`, which measures the hypervisor.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.host_client import get_host_client

logger = logging.getLogger("labforge.metrics")

# Filesystem types that are not real disks and must never be reported as one.
_SKIP_FS_TYPES = {
    "tmpfs", "devtmpfs", "devpts", "proc", "sysfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "ramfs", "autofs", "mqueue", "debugfs", "tracefs",
    "securityfs", "pstore", "bpf", "configfs", "fusectl", "efivarfs", "hugetlbfs",
}

# One awk pass so the whole reading is a single agent round-trip.
_MEMINFO_SCRIPT = (
    "awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} "
    "/^MemFree:/{f=$2} END{print t\" \"a\" \"f}' /proc/meminfo"
)


@dataclass
class GuestUsage:
    """What a single guest reports about its own resource usage."""
    name: str
    available: bool = False
    reason: str = ""
    cpu_percent: float = 0.0
    vcpus: int = 0
    mem_total_mb: int = 0
    mem_used_mb: int = 0
    mem_percent: float = 0.0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_percent: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0
    sampled_at: float = 0.0


@dataclass
class _CpuSample:
    """Cumulative CPU jiffies, kept so the next sample can derive a percent."""
    busy: int
    total: int
    ts: float


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_cpustats(stats) -> tuple[int, int]:
    """Aggregate (busy, total) jiffies across every vCPU."""
    busy = 0.0
    total = 0.0
    for cpu in stats or []:
        if not isinstance(cpu, dict):
            continue
        user = _num(cpu.get("user"))
        nice = _num(cpu.get("nice"))
        system = _num(cpu.get("system"))
        idle = _num(cpu.get("idle"))
        iowait = _num(cpu.get("iowait"))
        irq = _num(cpu.get("irq"))
        softirq = _num(cpu.get("softirq"))
        steal = _num(cpu.get("steal"))
        bus = user + nice + system + irq + softirq + steal
        busy += bus
        total += bus + idle + iowait
    return int(busy), int(total)


def _parse_meminfo(text: str) -> Optional[tuple[int, int]]:
    """Return (total_mb, used_mb) from the awk output, or None."""
    parts = text.split()
    if not parts:
        return None
    try:
        total_kb = int(parts[0])
    except ValueError:
        return None
    try:
        avail_kb = int(parts[1])
    except (IndexError, ValueError):
        avail_kb = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    total_mb = total_kb // 1024
    used_mb = max(0, total_kb - avail_kb) // 1024
    return total_mb, used_mb


def _parse_fsinfo(fsinfo) -> tuple[int, int]:
    """Pick the root filesystem, else the largest real one: (total, used)."""
    root = None
    largest = None
    for fs in fsinfo or []:
        if not isinstance(fs, dict):
            continue
        if (fs.get("type") or "").lower() in _SKIP_FS_TYPES:
            continue
        total = int(_num(fs.get("total-bytes")))
        used = int(_num(fs.get("used-bytes")))
        if total <= 0:
            continue
        if fs.get("mountpoint") == "/":
            root = (total, used)
        if largest is None or total > largest[0]:
            largest = (total, used)
    return root or largest or (0, 0)


def _parse_vcpus(vcpus) -> int:
    if not vcpus:
        return 0
    online = [v for v in vcpus if isinstance(v, dict) and v.get("online")]
    return len(online) or len(vcpus)


def _balloon_stats(client, name: str):
    getter = getattr(client, "get_vm_memory_stats", None)
    if getter is None:
        return None
    try:
        return getter(name)
    except Exception:  # noqa: BLE001 - a metrics fallback must never raise
        return None


def _parse_balloon(stats) -> Optional[tuple[int, int]]:
    """(total_mb, used_mb) from ``virsh dommemstat`` values, all in KB."""
    if not isinstance(stats, dict):
        return None
    total_kb = int(_num(stats.get("actual")))
    if total_kb <= 0:
        return None
    avail = stats.get("available")
    if avail is None:
        avail = stats.get("unused")
    avail_kb = _num(avail)
    return total_kb // 1024, max(0, int(total_kb - avail_kb)) // 1024


def collect(client, name: str, prev: Optional[_CpuSample],
            vcpus: int = 0) -> tuple[GuestUsage, Optional[_CpuSample]]:
    """Sample one guest. Never raises; returns an unavailable usage on failure."""
    usage = GuestUsage(name=name)

    stats = client.agent_command(name, "guest-get-cpustats")
    if stats is None:
        usage.reason = "guest agent not responding"
        return usage, prev

    busy, total = _parse_cpustats(stats)
    now = time.monotonic()
    if prev is not None:
        d_busy = busy - prev.busy
        d_total = total - prev.total
        if d_total > 0:
            usage.cpu_percent = round(max(0.0, min(100.0, d_busy / d_total * 100)), 1)
    new_prev = _CpuSample(busy=busy, total=total, ts=now)

    captured = client.guest_exec_capture(name, _MEMINFO_SCRIPT, timeout=6)
    if captured and captured.get("exitcode") == 0:
        mem = _parse_meminfo(captured.get("out", ""))
        if mem:
            usage.mem_total_mb, usage.mem_used_mb = mem
            usage.mem_percent = round(usage.mem_used_mb / max(usage.mem_total_mb, 1) * 100, 1)

    if not usage.mem_total_mb:
        # RHEL and CentOS disable the agent's exec/file commands by policy, so
        # guest-exec cannot read /proc/meminfo there. The virtio balloon also
        # reports memory gathered inside the guest and is always available.
        balloon = _parse_balloon(_balloon_stats(client, name))
        if balloon:
            usage.mem_total_mb, usage.mem_used_mb = balloon
            usage.mem_percent = round(usage.mem_used_mb / max(usage.mem_total_mb, 1) * 100, 1)

    total_b, used_b = _parse_fsinfo(client.agent_command(name, "guest-get-fsinfo"))
    if total_b:
        usage.disk_total_gb = round(total_b / 1024 ** 3, 1)
        usage.disk_used_gb = round(used_b / 1024 ** 3, 1)
        usage.disk_percent = round(used_b / total_b * 100, 1)

    load = client.agent_command(name, "guest-get-load")
    if isinstance(load, dict):
        usage.load_1m = round(_num(load.get("load1")) / 1000, 2)
        usage.load_5m = round(_num(load.get("load5")) / 1000, 2)
        usage.load_15m = round(_num(load.get("load15")) / 1000, 2)

    usage.vcpus = vcpus or _parse_vcpus(client.agent_command(name, "guest-get-vcpus"))
    usage.sampled_at = time.time()
    usage.available = True
    return usage, new_prev


class _MetricsSampler:
    """Background poller that keeps cached usage for every running VM."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, GuestUsage] = {}
        self._prev: dict[str, _CpuSample] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not settings.metrics_enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="labforge-metrics", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        self._stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=settings.metrics_interval_seconds + 2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_all()
            except Exception:  # noqa: BLE001 - the sampler must never die
                logger.warning("Guest metrics sampling failed", exc_info=True)
            self._stop.wait(settings.metrics_interval_seconds)

    def sample_all(self) -> None:
        client = get_host_client()
        try:
            vms = client.list_vms()
        except Exception:  # noqa: BLE001 - libvirt may be momentarily unavailable
            logger.debug("Guest metrics: cannot list VMs", exc_info=True)
            return
        for vm in vms:
            if vm.state != "running":
                with self._lock:
                    self._cache.pop(vm.name, None)
                    self._prev.pop(vm.name, None)
                continue
            usage, new_prev = collect(client, vm.name, self._prev.get(vm.name), vcpus=vm.vcpus)
            with self._lock:
                self._cache[vm.name] = usage
                if new_prev is not None:
                    self._prev[vm.name] = new_prev

    def get(self, name: str) -> Optional[GuestUsage]:
        with self._lock:
            return self._cache.get(name)

    def snapshot(self) -> dict[str, GuestUsage]:
        with self._lock:
            return dict(self._cache)


sampler = _MetricsSampler()


def get_cached_usage(name: str) -> Optional[GuestUsage]:
    """Cached guest usage for a VM, or None if it has not been sampled yet."""
    return sampler.get(name)


def cached_usage_by_name() -> dict[str, GuestUsage]:
    """Cached guest usage for every currently sampled VM."""
    return sampler.snapshot()


def usage_for(client, name: str) -> Optional[GuestUsage]:
    """Guest usage for a VM from whichever side sampled it.

    The host client owns the decision: a remote client (including the
    in-process agent used by ``LOCAL_AGENT``) serves the agent's sample over
    RPC, while a local client reads this process's sampler cache. Without this,
    a remote control plane reads an empty local cache and always reports
    "collecting guest metrics".
    """
    getter = getattr(client, "get_vm_usage", None)
    if getter is not None:
        try:
            return getter(name)
        except Exception:  # noqa: BLE001 - metrics must never break a page
            logger.debug("Remote guest usage fetch failed for %s", name, exc_info=True)
            return None
    return get_cached_usage(name)


def usage_map(client) -> dict[str, GuestUsage]:
    """Guest usage for every VM, from the remote client or the local cache."""
    getter = getattr(client, "all_vm_usage", None)
    if getter is not None:
        try:
            return getter() or {}
        except Exception:  # noqa: BLE001 - metrics must never break a page
            logger.debug("Remote guest usage map fetch failed", exc_info=True)
            return {}
    return cached_usage_by_name()


def start_sampler() -> None:
    sampler.start()


def stop_sampler() -> None:
    sampler.stop()
