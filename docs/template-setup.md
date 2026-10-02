# CentOS Stream 10 template setup

This prepares an **untouched official GenericCloud image**, not a copy of any
existing VM disk. Downloading does not create, boot, resize, or modify a VM and
requires neither sudo nor access to libvirt.

## Download and verify

From the project root, as your normal user:

```bash
bash scripts/download-centos-stream10.sh
```

Requirements: Bash, curl with working CA certificates, GNU coreutils, awk, and
about 1 GiB of free disk space. The script defaults to
`backend/cloud_init_templates/centos-stream10/`; an optional first argument selects
a different template directory. Copy `template.json` into that directory too if
using a custom destination.

The script pins the dated image instead of the mutable `latest` alias. It fetches
its official SHA-256 file over HTTPS, requires an exact filename match and the
reviewed digest below, verifies the downloaded bytes, then atomically publishes
the qcow2 without replacing any existing file. Rerunning verifies an existing
image; a different existing image causes an error, not an overwrite. Temporary
image data stays in a hidden `.download.*` directory on the destination filesystem
(to avoid consuming a RAM-backed `/tmp`) and is removed on exit. No image is
visible to template discovery until verification succeeds.

- Official download page: <https://www.centos.org/download/>
- Official image directory: <https://cloud.centos.org/centos/10-stream/x86_64/images/>
- Image: <https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-20260922.0.x86_64.qcow2>
- Official checksum: <https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-20260922.0.x86_64.qcow2.SHA256SUM>
- Size: **1,044,643,840 bytes**.
- SHA-256: `cc788312a1f5d86f2557de38b4ca57ff9f9a6b7f812f7a7fc91a5009fb68f76b`.

This is checksum verification against the official HTTPS endpoint, not OpenPGP
signature verification. If upstream removes this dated build or the download
fails, rerun later or deliberately review and update the filename and digest in
the script. Never bypass verification or substitute a disk from an existing VM.
Images, partial downloads, and generated VM media are excluded by `.gitignore`.
`backend/.dockerignore` also excludes downloaded disk images from Docker's build
context. Mount the prepared template directory read-only at runtime; cloud images
are not bundled into the lightweight application container.

The template metadata requests 1536 MiB RAM, 2 vCPUs, a 30 GiB disk,
`os_variant: centos-stream10`, and `ssh_user: cloud-user`. Keep exactly one qcow2
in each template directory. CentOS Stream 10 requires an x86-64-v3-capable CPU;
the VM CPU configuration must expose the needed host features. Keep the host's
libosinfo database current enough to recognize `centos-stream10`.

## SSH public keys

Prepare a UTF-8 file containing **public** OpenSSH keys, one per line, and point
the backend process at its absolute path:

```bash
export SSH_PUBLIC_KEYS_FILE=/absolute/path/to/labforge-authorized-keys.pub
export VIRSH_URI=qemu:///system
export TEMPLATES_DIR=/absolute/path/to/labforge/backend/cloud_init_templates
export VM_STORAGE_PATH=/var/lib/libvirt/images/labforge
```

Use the `.pub` portion of keys already managed by you. Do not place private keys,
passwords, or a private-key path in the environment, template, container image,
or repository. The backend must be able to read the public-key file; in a
container, mount it read-only and use its **container path** in
`SSH_PUBLIC_KEYS_FILE`. Restart the backend after configuration changes. Key
changes apply to newly provisioned instances, not existing VMs.

The rendering contract is `vm_name` (string), `ssh_user` (string, from template
metadata), and `ssh_keys` (list of public-key strings loaded by the backend).
`user-data.j2` encodes these values through Jinja's `tojson`, so YAML punctuation
or quotes in key comments cannot inject cloud-init settings. The backend must
supply both SSH variables and reject missing/empty keys before provisioning;
this template cannot read environment variables or key files itself.

Login is `ssh cloud-user@<guest-ip>`. When no password is set at provision
time, password login is disabled, the account's
password is locked, and root SSH login is disabled. The account has passwordless
sudo for lab administration. No default password or console autologin is added.
A serial login prompt is therefore not a password-based recovery route; use
SSH keys and an administrator-approved recovery procedure if necessary.

## Host libvirt and storage permissions

LabForge controls the **host system libvirt** service at `qemu:///system`, not a
per-user `qemu:///session` service and not a separate libvirt daemon inside the
backend container. Host libvirt/KVM and its intended DHCP network must already be
configured by the host administrator. The Dockerfile provides client utilities,
including `virt-install` from Debian's `virtinst` package; it does not set up the
host service or network.

Have the administrator provision a dedicated directory such as
`/var/lib/libvirt/images/labforge`. Do not change ownership or modes recursively
on the shared `/var/lib/libvirt/images` tree or any existing VM disks.

A least-privilege setup uses a dedicated shared group for the backend service
account and the actual host QEMU account (`libvirt-qemu` on many Debian hosts,
`qemu` on many RPM-based hosts; confirm locally):

- Dedicated VM directory: backend service owner, shared group, mode **2770**
  (setgid). Grant both accounts directory traversal and the required read/write
  access through group membership or narrowly scoped ACLs.
- Newly generated VM disks: shared-group read/write, typically **0660**; seed
  media needs read access. Use an appropriate service umask (for example `0007`)
  or default ACLs so future files inherit the intended access.
- Template directories: readable/traversable by the backend, preferably mounted
  read-only; base images may be **0644** with no write access for QEMU. The backend
  copies the template into VM storage, so QEMU need not traverse your home to
  access the source. Keep source templates separate from writable VM disks.
- All parent directories must permit required traversal. Avoid sourcing active
  VM disks from a private home directory. Do not use **chmod 777**, globally open
  libvirt sockets, or run the whole application as root as a permission workaround.
- On SELinux/AppArmor hosts, have the administrator apply the correct libvirt
  storage labels/profile access to this dedicated path; do not disable mandatory
  access controls or relabel unrelated VM storage.

For a container controlling host libvirt, expose the authorized host libvirt
socket and mount VM storage at the **same absolute path** in the container and on
the host: paths passed to libvirt are interpreted on the host. Align numeric
UID/GID or ACL access for the container process and host QEMU. Access to the
system libvirt socket is powerful; limit it to the service identity. Setup
commands for users/groups, permissions, labels, or networks are administrator
operations, not part of the download script.

## First boot and consoles

The default network configuration uses DHCPv4 on Ethernet names matching `e*`
(including `eth0`, `ens3`, and `enp1s0`) and obtains DNS/routes from DHCP. It does
not pin an interface name, rename interfaces, choose public DNS servers, or force
a renderer. DHCPv6 is disabled. If your deployment uses nonstandard interface
names or static addressing, review this configuration for that deployment.

Cloud-init installs `qemu-guest-agent`, enables its service, enables SSH, and
enables `serial-getty@ttyS0`. It does **not** run a full `dnf update` or package
upgrade on boot. Guest-agent installation still needs reachable configured
repositories, and guest-agent IP reporting requires a libvirt channel named
`org.qemu.guest_agent.0` in the VM definition. The serial console also requires
the VM's serial device; the cloud image normally supplies serial kernel output.

GenericCloud is a server image: **there is no GUI desktop**. The graphical
Screen/VNC view may show boot text or a text login, not GNOME or a desktop session.
Use SSH for administration and the serial console for diagnostics. Template and
cloud-init changes do not retrofit already-created VMs.
