# Templates

A template is a directory under `TEMPLATES_DIR` with a cloud image and optional
`template.json`:

```
$TEMPLATES_DIR/
  default/                 # shared cloud-init Jinja2
    user-data.j2
    meta-data.j2
    network-config.j2
  fedora-44/
    Fedora-Cloud-Base-….qcow2
    template.json
```

`template.json` (all fields optional):

```json
{
  "description": "Fedora 44 Generic Cloud",
  "memory_mb": 1536,
  "vcpus": 2,
  "disk_gb": 30,
  "os_variant": "fedora44",
  "ssh_user": "cloud-user"
}
```

Image extensions: `.qcow2`, `.img`, `.qcow`, `.raw`. A per-template
`user-data.j2` overrides `default/`.

```bash
./scripts/fetch-templates.sh    # Ubuntu + Debian, checksum verified
```

Distro-specific example: [CentOS Stream 10](template-setup.md).

## Provision from an uploaded qcow2

On Templates → **Provision from qcow2**: file picker, drag-and-drop, or pick a
file already in `IMPORT_DIR` (default `<VM_STORAGE_PATH>/imports`). Cap:
`MAX_UPLOAD_GB` (default 64). Upload is validated as qcow2; LabForge copies and
resizes a VM disk and never modifies the upload.

## VM naming

Pattern: `<prefix><template>-<name>` → e.g. `labs-fedora-44-web1`. Uploaded
images use the file stem (dots → hyphens). Names are lowercased, separators
collapsed, max 63 characters, must stay under `VM_NAME_PREFIX`.
