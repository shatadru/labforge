"""Safe regression tests: no hypervisor mutations, fake image data in temp dirs."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.config import settings
from app.main import app
from app.virsh_client import VirshClient, VirshResult
from app import host as host_module
from app.host import HostUsage, check_resources


_SEED_TOOLS = ("cloud-localds", "genisoimage", "xorriso", "mkisofs")


def _file_from_cmd(cmd, name):
    for arg in cmd:
        if str(arg).endswith(name):
            return Path(arg)
    return None


class LabTests(unittest.TestCase):
    def test_namespace_and_direct_actions(self):
        client = VirshClient()
        self.assertEqual(client.provision_name('demo'), 'labs-demo')
        self.assertEqual(client.provision_name('labs-demo'), 'labs-demo')
        self.assertEqual(client.provision_name('demo', 'fedora-44'), 'labs-fedora-44-demo')
        self.assertEqual(client.provision_name('demo', 'ubuntu-24.04'), 'labs-ubuntu-24-04-demo')
        self.assertEqual(client.provision_name('demo', 'centos-stream10'), 'labs-centos-stream10-demo')
        # An already qualified name is returned unchanged.
        self.assertEqual(client.provision_name('labs-fedora-44-demo', 'rhel-10'),
                         'labs-fedora-44-demo')
        # User input is normalised rather than rejected.
        self.assertEqual(client.provision_name('My VM!', 'fedora-44'), 'labs-fedora-44-my-vm')
        self.assertEqual(client.provision_name('Web_Server', 'rhel-10'), 'labs-rhel-10-web-server')
        self.assertEqual(client.provision_name('labs-Foo', 'rhel-10'), 'labs-foo')
        for name in ('fedora', 'labs-', 'labs-../fedora', '--help'):
            self.assertFalse(client.is_lab_vm(name))
        with patch('app.virsh_client.subprocess.run') as run:
            for method, args in [('start_vm', ('fedora',)), ('stop_vm', ('fedora',)),
                                 ('delete_vm', ('fedora',)), ('get_console_command', ('fedora',)),
                                 ('get_display', ('fedora',)), ('reboot_vm', ('fedora',)),
                                 ('create_snapshot', ('fedora', 's')),
                                 ('revert_snapshot', ('fedora', 's')),
                                 ('delete_snapshot', ('fedora', 's'))]:
                with self.assertRaises(PermissionError):
                    getattr(client, method)(*args)
            run.assert_not_called()

    def test_api_denies_old_vms(self):
        """Old/foreign VMs are invisible and never mutated by the API."""
        import subprocess
        with TestClient(app) as web, patch('app.virsh_client.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            for method, path in [('delete', '/api/v1/vms/fedora'),
                                 ('post', '/api/v1/vms/fedora/reboot'),
                                 ('get', '/api/v1/vms/fedora/snapshots')]:
                self.assertEqual(getattr(web, method)(path).status_code, 404)

            executed = [call.args[0] for call in run.call_args_list if call.args]
            for cmd in executed:
                self.assertNotIn('fedora', cmd, cmd)
                self.assertTrue(
                    any(arg in cmd for arg in ('list',)),
                    f"only read-only discovery is allowed, got {cmd}",
                )

    def test_discovery_skips_old_vm_inspection(self):
        client = VirshClient()
        def fake(args):
            if args[0] == 'list':
                return VirshResult(True, 'fedora\nlabs-demo\nk8s-master1')
            self.assertEqual(args[1], 'labs-demo')
            return VirshResult(True, 'shut off' if args[0] == 'domstate' else '')
        with patch.object(client, '_run', side_effect=fake):
            self.assertEqual([v.name for v in client.list_vms()], ['labs-demo'])

    def test_template_provision_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / 'templates'
            templates.mkdir(parents=True)
            default = templates / 'default'
            default.mkdir(parents=True)
            # Copy the default templates from the actual location
            actual_default = Path(__file__).parent.parent / 'cloud_init_templates' / 'default'
            if actual_default.exists():
                for f in actual_default.iterdir():
                    if f.is_file():
                        (default / f.name).write_text(f.read_text())
            centos_template = templates / 'centos-stream10'
            centos_template.mkdir(parents=True)
            (centos_template / 'base.qcow2').write_bytes(b'fake image for mocked tools')
            (centos_template / 'template.json').write_text('{"memory_mb": 3072, "vcpus": 3, "disk_gb": 24, "os_variant": "centos-stream10", "ssh_user": "cloud-user"}')
            key = root / 'keys.pub'
            key.write_text('ssh-ed25519 AAAATEST test-key\n')
            storage = root / 'storage'
            storage.mkdir()
            client = VirshClient(storage_path=str(storage), templates_dir=str(templates))
            self.assertIsNone(client.get_template('../outside'))
            self.assertEqual(Path(client.get_template('centos-stream10').path), centos_template / 'base.qcow2')
            commands = []
            def raw(cmd, timeout=120):
                commands.append(cmd)
                if cmd[0] in _SEED_TOOLS:
                    data = _file_from_cmd(cmd, "user-data").read_text()
                    self.assertIn('ssh-ed25519 AAAATEST test-key', data)
                    self.assertIn('labs-centos-stream10-demo', data)
                    self.assertIn('qemu-guest-agent', data)
                return VirshResult(True)
            with patch.object(settings, 'ssh_public_keys_file', str(key)), \
                 patch.object(client, 'vm_exists', return_value=False), \
                 patch.object(client, 'list_vms', return_value=[]), \
                 patch.object(client, '_run_raw', side_effect=raw), \
                 patch.object(client, 'get_vm_ip', return_value='192.0.2.10'), \
                 patch('app.virsh_client.check_resources', return_value={"ok": True, "reasons": []}):
                result = client.create_vm('demo', 'centos-stream10')
                self.assertTrue(result.success, result.stderr)
                install = next(c for c in commands if c[0] == 'virt-install' and '--name' in c)
                self.assertEqual(install[install.index('--name')+1], 'labs-centos-stream10-demo')
                self.assertEqual(install[install.index('--memory')+1], '3072')
                self.assertEqual(install[install.index('--connect')+1], 'qemu:///system')
                expected_graphics = ('none' if settings.graphics_type == 'none'
                                     else 'vnc,listen=127.0.0.1')
                self.assertEqual(install[install.index('--graphics') + 1], expected_graphics)
                self.assertTrue(client.start_vm('labs-centos-stream10-demo').success)
                self.assertEqual(commands[-1],
                                 ['virsh', '-c', 'qemu:///system', 'start',
                                  'labs-centos-stream10-demo'])
                count = len(commands)
                self.assertFalse(client.create_vm('demo', 'centos-stream10').success)
                self.assertEqual(len(commands), count)

    def test_password_in_cloud_init(self):
        """Injected credentials are rendered in user-data."""
        client = VirshClient(templates_dir='cloud_init_templates')
        text = client._render_template(
            'user-data.j2', vm_name='labs-demo', vm_user='deploy',
            ssh_keys=['ssh-ed25519 AAAATEST test-key'], vm_password='S3cret!pass')
        self.assertIn('deploy', text)
        self.assertIn('S3cret!pass', text)
        self.assertIn('plain_text_passwd', text)
        self.assertIn('ssh_pwauth: true', text)
        self.assertNotIn('lock_passwd: true', text)

    def test_key_only_when_no_password(self):
        """No password means SSH-key-only auth (no baked-in credential)."""
        client = VirshClient(templates_dir='cloud_init_templates')
        text = client._render_template(
            'user-data.j2', vm_name='labs-demo', vm_user='deploy',
            ssh_keys=['ssh-ed25519 AAAATEST test-key'], vm_password=None)
        self.assertIn('ssh_pwauth: false', text)
        self.assertIn('lock_passwd: true', text)
        self.assertNotIn('plain_text_passwd', text)

    def test_no_hardcoded_password_default(self):
        """The app ships no default password."""
        from app.config import Settings
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("VM_PASSWORD",)}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(Settings(_env_file=None).vm_password)

    def test_tailscale_in_userdata_when_enabled(self):
        """Enabling Tailscale adds the first-boot join script (official installer)."""
        client = VirshClient(templates_dir='cloud_init_templates')
        text = client._render_template(
            'user-data.j2', vm_name='labs-demo', vm_user='cloud-user',
            ssh_keys=['ssh-ed25519 AAAATEST'], vm_password=None,
            tailscale_enabled=True)
        self.assertIn('tailscale', text)
        self.assertIn('https://tailscale.com/install.sh', text)
        self.assertIn('--auth-key=', text)
        self.assertIn('/usr/local/sbin/labforge-tailscale-up', text)
        self.assertIn('tailscaled', text)
        # The key itself must never appear in user-data.
        self.assertNotIn('tskey', text)

    def test_tailscale_absent_without_auth_key(self):
        """No Tailscale packages or commands when it is disabled."""
        client = VirshClient(templates_dir='cloud_init_templates')
        text = client._render_template(
            'user-data.j2', vm_name='labs-demo', vm_user='cloud-user',
            ssh_keys=['ssh-ed25519 AAAATEST'], vm_password=None,
            tailscale_enabled=False)
        self.assertNotIn('tailscale', text)
        self.assertNotIn('curl', text)
        self.assertIn('qemu-guest-agent', text)

    def test_render_template_is_strict(self):
        """Missing variables must raise, not silently render empty values."""
        from jinja2 import TemplateError
        client = VirshClient(templates_dir='cloud_init_templates')
        with self.assertRaises((TemplateError, TypeError)):
            client._render_template('user-data.j2')  # no vm_name/ssh_keys/...

    def test_template_override_then_default_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp)
            (templates / 'default').mkdir(parents=True)
            (templates / 'default' / 'user-data.j2').write_text('default:{{ vm_name }}')
            (templates / 'custom').mkdir()
            (templates / 'custom' / 'user-data.j2').write_text('override:{{ vm_name }}')
            client = VirshClient(templates_dir=str(templates))
            self.assertEqual(
                client._render_template('user-data.j2', search_dir=templates / 'custom', vm_name='x'),
                'override:x')
            self.assertEqual(
                client._render_template('user-data.j2', search_dir=templates / 'missing', vm_name='x'),
                'default:x')

    def test_tailscale_key_not_included_in_user_data(self):
        """user-data never contains key material; the key lives on the seed only."""
        client = VirshClient(templates_dir='cloud_init_templates')
        text = client._render_template(
            'user-data.j2', vm_name='labs-demo', vm_user='deploy',
            ssh_keys=['ssh-ed25519 AAAATEST'], vm_password=None,
            tailscale_enabled=True)
        self.assertNotIn('tskey', text)
        self.assertIn('--auth-key=', text)
        self.assertIn('/run/labforge-seed/authkey', text)

    def test_create_vm_cleans_up_on_mid_failure(self):
        """A failure after the disk copy must not strand the VM name."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / 'templates'
            default = templates / 'default'
            default.mkdir(parents=True)
            src = Path(__file__).parent.parent / 'cloud_init_templates' / 'default'
            for f in src.iterdir():
                (default / f.name).write_text(f.read_text())
            tpl = templates / 'testos'
            tpl.mkdir()
            (tpl / 'base.qcow2').write_bytes(b'img')
            (tpl / 'template.json').write_text('{"memory_mb":2048,"vcpus":2,"disk_gb":20,"ssh_user":"cloud-user"}')
            storage = root / 'storage'
            storage.mkdir()
            key = root / 'k.pub'
            key.write_text('ssh-ed25519 AAAA test\n')

            client = VirshClient(storage_path=str(storage), templates_dir=str(templates))

            def raw(cmd, timeout=120):
                if cmd[0] == 'qemu-img':          # fail at resize
                    return VirshResult(False, stderr='resize failed')
                return VirshResult(True)

            with patch.object(settings, 'ssh_public_keys_file', str(key)), \
                 patch.object(client, 'vm_exists', return_value=False), \
                 patch.object(client, 'list_vms', return_value=[]), \
                 patch.object(client, '_run_raw', side_effect=raw):
                result = client.create_vm('demo', 'testos')

            self.assertFalse(result.success)
            self.assertFalse((storage / 'labs-testos-demo.qcow2').exists())
            self.assertFalse((storage / 'seed-labs-testos-demo.iso').exists())

    def test_tailscale_ip_detection_from_guest_agent(self):
        client = VirshClient()
        output = (
            " Name       MAC address          Protocol     Address\n"
            "--------------------------------------------------------------------\n"
            " lo         00:00:00:00:00:00    ipv4         127.0.0.1/8\n"
            " eth0       52:54:00:aa:bb:cc    ipv4         192.168.122.45/24\n"
            " tailscale0                     ipv4         100.101.102.103/32\n"
        )
        with patch.object(client, '_run', return_value=VirshResult(True, output)):
            self.assertEqual(client.get_vm_ip('labs-demo'), '192.168.122.45')
            self.assertEqual(client.get_tailscale_ip('labs-demo'), '100.101.102.103')
            self.assertEqual(client.get_vm_addresses('labs-demo'),
                             ['192.168.122.45', '100.101.102.103'])

    def test_host_usage_and_resource_check(self):
        """Host metrics endpoint and resource preflight work."""
        mock_usage = HostUsage(
            cpu_percent=12.5, mem_total_mb=16000, mem_available_mb=8000,
            mem_used_mb=8000, mem_percent=50.0, load_1m=0.3, load_5m=0.2,
            load_15m=0.1, cpu_cores=4, disk_total_gb=100, disk_available_gb=50,
            disk_used_gb=50, disk_percent=50.0,
        )
        with patch.object(host_module, 'get_host_usage', return_value=mock_usage):
            usage = host_module.get_host_usage()
            self.assertEqual(usage.cpu_cores, 4)
            result = check_resources(2048, 2, 20)
            self.assertTrue(result["ok"], result)

    def test_rhel_and_fedora_templates_exist(self):
        """Discovery finds every template dir that holds an image.

        Cloud images are gitignored, so build the layout in a temp dir: a
        template is only usable when its directory contains a qcow2/qcow/img/raw
        file next to template.json.
        """
        expected = {
            'rhel-10': 'rhel-10.qcow2',
            'fedora-44': 'fedora-44.img',
            'centos-stream10': 'centos-stream10.qcow2',
            'ubuntu-24.04': 'ubuntu-24.04.qcow2',
            'debian-12': 'debian-12.qcow2',
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, image in expected.items():
                template_dir = Path(tmp) / name
                template_dir.mkdir()
                (template_dir / image).write_bytes(b"fake-image")
                (template_dir / "template.json").write_text('{"description": "test"}')

            client = VirshClient(templates_dir=tmp)
            for name, image in expected.items():
                tpl = client.get_template(name)
                self.assertIsNotNone(tpl, f"Template {name} missing")
                self.assertTrue(tpl.path.endswith(('.qcow2', '.img')))
            # A template name with a dot is allowed, but traversal is not.
            self.assertIsNone(client.get_template('../etc'))
            self.assertIsNone(client.get_template('..'))
            # A directory without an image is not a usable template.
            (Path(tmp) / 'empty').mkdir()
            self.assertIsNone(client.get_template('empty'))

    def test_vnc_port_from_xml(self):
        client = VirshClient()
        xml = """<domain><devices>
<graphics type='vnc' port='5901' autoport='yes' listen='127.0.0.1'>
  <listen type='address' address='127.0.0.1'/>
</graphics>
<graphics type='spice' port='5900'/>
</devices></domain>"""
        with patch.object(client, '_run', return_value=VirshResult(True, xml)):
            self.assertEqual(client.get_display('labs-demo'), ('127.0.0.1', 5901))
        spice_only = xml.replace("<graphics type='vnc' port='5901' autoport='yes' listen='127.0.0.1'>\n  <listen type='address' address='127.0.0.1'/>\n</graphics>\n", "")
        with patch.object(client, '_run', return_value=VirshResult(True, spice_only)):
            self.assertIsNone(client.get_display('labs-demo'))
        with patch.object(client, '_run', return_value=VirshResult(True, "<domain><devices><graphics type='vnc' port='-1'/></devices></domain>")):
            self.assertIsNone(client.get_display('labs-demo'))

    def test_headless_create_start_flow(self):
        with patch.object(settings, 'graphics_type', 'none'):
            self.test_template_provision_flow()

    def test_stopped_vm_still_exists(self):
        client = VirshClient()
        with patch.object(client, '_run', return_value=VirshResult(True, 'shut off')):
            self.assertTrue(client.vm_exists('labs-demo'))


if __name__ == '__main__':
    unittest.main()
