"""Host metrics, lab budget and preflight tests."""
from app.host import (
    HostUsage,
    budget_for,
    capacity,
    check_resources,
    fits_count,
    get_host_usage,
    usage_of,
)
from app.virsh_client import VMInfo


def _usage(**overrides) -> HostUsage:
    base = dict(
        cpu_percent=10.0, mem_total_mb=16000, mem_available_mb=12000, mem_used_mb=4000,
        mem_percent=25.0, load_1m=0.1, load_5m=0.1, load_15m=0.1, cpu_cores=8,
        disk_total_gb=200, disk_available_gb=150, disk_used_gb=50, disk_percent=25.0,
    )
    base.update(overrides)
    return HostUsage(**base)


def _vm(name="labs-a", vcpus=2, memory_mb=2048, disk_gb=20) -> VMInfo:
    return VMInfo(name=name, state="running", vcpus=vcpus, memory_mb=memory_mb, disk_gb=disk_gb)


# ---------------------------------------------------------------- host usage

def test_real_host_usage_is_sane():
    usage = get_host_usage()
    assert usage.cpu_cores >= 1
    assert 0.0 <= usage.cpu_percent <= 100.0
    assert usage.mem_total_mb > 0
    assert 0 <= usage.mem_available_mb <= usage.mem_total_mb
    assert 0.0 <= usage.mem_percent <= 100.0
    assert usage.disk_total_gb >= 0
    assert 0.0 <= usage.disk_percent <= 100.0
    assert usage.load_1m >= 0


def test_disk_usage_falls_back_to_root(monkeypatch):
    from app import host as host_module

    real = host_module.shutil.disk_usage
    calls = []

    def fake(path):
        calls.append(path)
        if path != "/":
            raise OSError("missing")
        return real("/")

    monkeypatch.setattr(host_module.shutil, "disk_usage", fake)
    monkeypatch.setattr(host_module.settings, "vm_storage_path", "/definitely/missing")

    usage = host_module.get_host_usage()
    assert "/" in calls
    assert usage.disk_total_gb > 0


# ---------------------------------------------------------------- budget

def test_budget_is_percent_of_host_total():
    budget = budget_for(_usage(), percent=50)
    assert budget.vcpus == 4          # 50% of 8 cores
    assert budget.memory_mb == 8000   # 50% of 16000 MB
    assert budget.disk_gb == 100      # 50% of 200 GB
    assert budget.percent == 50


def test_budget_default_comes_from_settings():
    from app.config import settings
    budget = budget_for(_usage())
    assert budget.percent == settings.resource_budget_percent


def test_usage_of_sums_vms():
    used = usage_of([_vm("a"), _vm("b", vcpus=4, memory_mb=8192, disk_gb=40)])
    assert used.vms == 2
    assert used.vcpus == 6
    assert used.memory_mb == 10240
    assert used.disk_gb == 60


def test_usage_of_empty():
    used = usage_of([])
    assert (used.vms, used.vcpus, used.memory_mb, used.disk_gb) == (0, 0, 0, 0)


def test_fits_count_is_limited_by_scarcest_dimension():
    remaining = {"vcpus": 8, "memory_mb": 8192, "disk_gb": 100}
    # cpu: 8/2=4, mem: 8192/2048=4, disk: 100/20=5  -> 4
    assert fits_count(remaining, 2, 2048, 20) == 4
    # memory is now the limit
    assert fits_count({"vcpus": 100, "memory_mb": 4096, "disk_gb": 100}, 2, 2048, 20) == 2
    # disk is now the limit
    assert fits_count({"vcpus": 100, "memory_mb": 100000, "disk_gb": 25}, 2, 2048, 20) == 1


def test_capacity_reports_remaining_and_percent():
    cap = capacity(_usage(), [_vm(), _vm("labs-b")], percent=50)
    assert cap["budget"] == {"vcpus": 4, "memory_mb": 8000, "disk_gb": 100}
    assert cap["used"] == {"vms": 2, "vcpus": 4, "memory_mb": 4096, "disk_gb": 40}
    assert cap["remaining"] == {"vcpus": 0, "memory_mb": 3904, "disk_gb": 60}
    assert cap["percent_used"]["vcpus"] == 100.0
    assert cap["percent_used"]["memory"] == 51.2


# ---------------------------------------------------------------- preflight

def test_check_resources_ok_within_budget():
    result = check_resources(2048, 2, 20, usage=_usage(), vms=[])
    assert result["ok"] is True
    assert result["reasons"] == []
    # 50% budget: 4 vCPU / 8000 MB / 100 GB -> 2048 MB is the binding limit (3),
    # but 2 vCPU per VM caps it at 2.
    assert result["fits"] == 2


def test_check_resources_cpu_budget_exceeded():
    result = check_resources(1024, 8, 10, usage=_usage(), vms=[])
    assert result["ok"] is False
    assert any("cpu budget" in r.lower() for r in result["reasons"])


def test_check_resources_memory_budget_exceeded():
    # budget is 8000 MB; two VMs already consume 4096, so 8192 more won't fit
    result = check_resources(8192, 1, 10, usage=_usage(), vms=[_vm()])
    assert result["ok"] is False
    assert any("memory budget" in r.lower() for r in result["reasons"])


def test_check_resources_disk_budget_exceeded():
    result = check_resources(1024, 1, 150, usage=_usage(), vms=[])
    assert result["ok"] is False
    assert any("disk budget" in r.lower() for r in result["reasons"])


def test_check_resources_host_free_memory_safety():
    # Plenty of budget, but the host is nearly out of free memory right now.
    usage = _usage(mem_total_mb=64000, mem_available_mb=100)
    result = check_resources(2048, 1, 10, usage=usage, vms=[])
    assert result["ok"] is False
    assert any("host free memory" in r.lower() for r in result["reasons"])


def test_check_resources_host_free_disk_safety():
    usage = _usage(disk_total_gb=4000, disk_available_gb=1)
    result = check_resources(1024, 1, 20, usage=usage, vms=[])
    assert result["ok"] is False
    assert any("host free disk" in r.lower() for r in result["reasons"])


def test_check_resources_includes_capacity_snapshot():
    result = check_resources(2048, 2, 20, usage=_usage(), vms=[_vm()])
    assert result["capacity"]["used"]["vms"] == 1
    assert "budget_percent" in result["capacity"]
