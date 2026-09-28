"""Unit tests for guest usage metrics (QEMU guest agent responses are faked)."""
from app import vm_metrics
from app.virsh_client import VMInfo


class FakeAgentClient:
    """Stand-in VirshClient exposing only what the collector uses."""

    def __init__(self, vms=None, cpustats=None, fsinfo=None, load=None,
                 vcpus=None, mem_out=None, balloon=None):
        self.vms = list(vms or [])
        self.cpustats = cpustats
        self.fsinfo = fsinfo
        self.load = load
        self.vcpus = vcpus
        self.mem_out = mem_out
        self.balloon = balloon

    def list_vms(self):
        return list(self.vms)

    def get_vm_memory_stats(self, name):
        return self.balloon

    def agent_command(self, name, execute, arguments=None, timeout=15):
        return {
            "guest-get-cpustats": self.cpustats,
            "guest-get-fsinfo": self.fsinfo,
            "guest-get-load": self.load,
            "guest-get-vcpus": self.vcpus,
        }.get(execute)

    def _guest_exec_capture(self, name, script, timeout=6):
        if self.mem_out is None:
            return None
        return {"exitcode": 0, "out": self.mem_out, "err": ""}

    # Public alias the collector now uses (safe to expose over the agent RPC).
    def guest_exec_capture(self, name, script, timeout=6):
        return self._guest_exec_capture(name, script, timeout)


CPU_1 = [{"cpu": 0, "user": 10, "nice": 0, "system": 5, "idle": 85, "iowait": 0}]
CPU_2 = [{"cpu": 0, "user": 30, "nice": 0, "system": 10, "idle": 160, "iowait": 0}]
FS = [{"mountpoint": "/", "type": "ext4",
       "total-bytes": 32212254720, "used-bytes": 3221225472}]
# The QEMU guest agent scales load averages by 1000 (500 == load 0.5).
LOAD = {"load1": 500, "load5": 400, "load15": 300}


def _client(**kwargs):
    return FakeAgentClient(**kwargs)


# ---------------------------------------------------------------- parsing

def test_parse_cpustats_aggregates_all_vcpus():
    stats = [
        {"user": 10, "nice": 1, "system": 2, "idle": 5, "iowait": 2, "irq": 1, "softirq": 1},
        {"user": 20, "nice": 0, "system": 3, "idle": 7, "iowait": 0},
    ]
    busy, total = vm_metrics._parse_cpustats(stats)
    assert busy == 10 + 1 + 2 + 1 + 1 + 20 + 3   # user+nice+system+irq+softirq(+steal 0)
    assert total == busy + 5 + 2 + 7


def test_parse_meminfo():
    total, used = vm_metrics._parse_meminfo("2048000 1024000 1024000")
    assert total == 2000
    assert used == 1000


def test_parse_meminfo_falls_back_to_memfree_when_available_absent():
    # awk collapses the missing MemAvailable, so MemFree becomes the second field.
    total, used = vm_metrics._parse_meminfo("2048000 2048000")
    assert total == 2000
    assert used == 0


def test_parse_fsinfo_prefers_root_and_skips_pseudo():
    fsinfo = [
        {"mountpoint": "/dev/shm", "type": "tmpfs", "total-bytes": 999, "used-bytes": 1},
        {"mountpoint": "/", "type": "ext4", "total-bytes": 100, "used-bytes": 40},
    ]
    assert vm_metrics._parse_fsinfo(fsinfo) == (100, 40)
    # tmpfs would be larger but must never win.
    big_tmpfs = [{"mountpoint": "/x", "type": "tmpfs", "total-bytes": 10**12, "used-bytes": 0}]
    assert vm_metrics._parse_fsinfo(big_tmpfs) == (0, 0)


def test_parse_vcpus_counts_online():
    assert vm_metrics._parse_vcpus([{"online": True}, {"online": False}, {"online": True}]) == 2
    assert vm_metrics._parse_vcpus([]) == 0


def test_parse_balloon():
    assert vm_metrics._parse_balloon({"actual": 1572864, "available": 1492652}) == (1536, 78)
    assert vm_metrics._parse_balloon({"actual": 1572864, "unused": 1222588}) == (1536, 342)
    assert vm_metrics._parse_balloon({"actual": 0}) is None
    assert vm_metrics._parse_balloon(None) is None


# ---------------------------------------------------------------- collect

def test_collect_computes_cpu_mem_disk():
    client = _client(cpustats=CPU_2, fsinfo=FS, load=LOAD,
                     mem_out="2048000 1024000 1024000")
    prev = vm_metrics._CpuSample(busy=15, total=100, ts=0.0)

    usage, new_prev = vm_metrics.collect(client, "labs-a", prev, vcpus=2)

    assert usage.available is True
    assert usage.cpu_percent == 25.0          # (40-15)/(200-100)
    assert usage.mem_total_mb == 2000
    assert usage.mem_used_mb == 1000
    assert usage.mem_percent == 50.0
    assert usage.disk_total_gb == 30.0
    assert usage.disk_used_gb == 3.0
    assert usage.disk_percent == 10.0
    assert usage.load_1m == 0.5
    assert usage.vcpus == 2
    assert (new_prev.busy, new_prev.total) == (40, 200)


def test_collect_memory_falls_back_to_balloon():
    # RHEL/CentOS disable guest-exec, so /proc/meminfo is unavailable.
    client = _client(cpustats=CPU_1, fsinfo=FS, load=LOAD, mem_out=None,
                     balloon={"actual": 1572864, "available": 1492652})
    usage, _ = vm_metrics.collect(client, "labs-a", None, vcpus=2)
    assert usage.mem_total_mb == 1536
    assert usage.mem_used_mb == 78
    assert usage.mem_percent == 5.1


def test_collect_first_sample_has_no_cpu_but_is_available():
    client = _client(cpustats=CPU_1, fsinfo=FS, load=LOAD, mem_out="2048000 1024000 1024000")
    usage, prev = vm_metrics.collect(client, "labs-a", None, vcpus=1)
    assert usage.available is True
    assert usage.cpu_percent == 0.0
    assert prev is not None


def test_collect_unavailable_when_agent_missing():
    client = _client(cpustats=None)
    usage, prev = vm_metrics.collect(client, "labs-a", None)
    assert usage.available is False
    assert "agent" in usage.reason
    assert prev is None


# ---------------------------------------------------------------- sampler

def test_sampler_populates_then_clears_stopped_vms(monkeypatch):
    running = VMInfo(name="labs-a", state="running", vcpus=2)
    client = _client(vms=[running], cpustats=CPU_1, fsinfo=FS, load=LOAD,
                     mem_out="2048000 1024000 1024000")
    monkeypatch.setattr(vm_metrics, "get_host_client", lambda: client)

    sampler = vm_metrics._MetricsSampler()
    sampler.sample_all()
    assert sampler.get("labs-a") is not None
    assert sampler.get("labs-a").available is True

    # The VM stops: its cache entry must be dropped, not shown stale.
    client.vms = [VMInfo(name="labs-a", state="shut off", vcpus=2)]
    sampler.sample_all()
    assert sampler.get("labs-a") is None


def test_sampler_survives_libvirt_failure(monkeypatch):
    class Boom:
        def list_vms(self):
            raise RuntimeError("libvirt down")

    monkeypatch.setattr(vm_metrics, "get_host_client", lambda: Boom())
    sampler = vm_metrics._MetricsSampler()
    sampler.sample_all()  # must not raise
    assert sampler.snapshot() == {}
