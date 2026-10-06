# Deploy

| Piece | Artifact |
|-------|----------|
| Control plane | `ghcr.io/shatadru/labforge:1.0.0`, Helm, Kustomize, or `install-local.sh` |
| Agent | `labforge-agent` `v1.0.0` `.deb` / `.rpm` |

Helm and Kustomize defaults can differ. Check the values you apply.

```mermaid
flowchart TB
  subgraph Cluster["Kubernetes"]
    Helm["Helm / Kustomize<br/>MODE=control<br/>HOST_MODE=remote"]
  end

  subgraph KVM["KVM host"]
    Pkg["labforge-agent package<br/>MODE=agent"]
    LV[libvirt]
    Pkg --> LV
  end

  Helm -->|"AGENT_URL + AGENT_TOKEN"| Pkg
```

## 1. Install the agent on the hypervisor

```bash
VERSION=1.0.0
curl -fLO "https://github.com/shatadru/labforge/releases/download/v${VERSION}/labforge-agent_${VERSION}_amd64.deb"
sudo apt install "./labforge-agent_${VERSION}_amd64.deb"
# Fedora/RHEL/CentOS: download labforge-agent-${VERSION}-1.x86_64.rpm instead,
# then run: sudo dnf install ./labforge-agent-${VERSION}-1.x86_64.rpm
```

Release assets: <https://github.com/shatadru/labforge/releases/latest>.

Edit `/etc/labforge/labforge-agent.env`:

- Set `AGENT_TOKEN` (`openssl rand -hex 32`)
- For a remote control plane, set `AGENT_BIND` to a reachable address (not loopback)

```bash
sudo systemctl enable --now labforge-agent
```

## 2a. Helm (control only)

```bash
kubectl create namespace labforge
kubectl -n labforge create secret generic labforge-ssh-keys \
  --from-file=authorized_keys=$HOME/.ssh/id_ed25519.pub
kubectl -n labforge create secret generic labforge-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"
helm install labforge charts/labforge \
  --namespace labforge \
  --set image.repository=ghcr.io/shatadru/labforge \
  --set image.tag=1.0.0 \
  --set agent.url=http://kvm-host:8443 \
  --set agent.tokenSecretName=labforge-agent-token \
  --set sshKeys.secretName=labforge-ssh-keys
```

Use the **same** token on the agent. Full options: `charts/labforge/values.yaml`.

The chart is also published as an OCI artifact on GHCR (one version per
release, alongside the image and packages):

```bash
helm install labforge oci://ghcr.io/shatadru/charts/labforge \
  --version 1.0.0 --namespace labforge -f my-values.yaml
```

## 2b. Kustomize

```bash
kubectl create namespace labforge
kubectl -n labforge create secret generic labforge-ssh-keys \
  --from-file=authorized_keys=$HOME/.ssh/id_ed25519.pub
kubectl -n labforge create secret generic labforge-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl apply -k k8s/overlays/dev
```

Set `AGENT_URL` in `k8s/base/configmap.yaml` (or patch). Pin the image to a
released tag such as `ghcr.io/shatadru/labforge:1.0.0` rather than `latest`.

## Container (control only)

```bash
docker pull ghcr.io/shatadru/labforge:1.0.0
docker run --rm -p 8000:8000 \
  -e MODE=control \
  -e HOST_MODE=remote \
  -e AGENT_URL=http://kvm-host:8443 \
  -e AGENT_TOKEN="$(cat /path/to/agent-token)" \
  -e SSH_PUBLIC_KEYS_FILE=/etc/labforge/ssh/authorized_keys \
  -v "$HOME/.ssh/labforge_authorized_keys:/etc/labforge/ssh/authorized_keys:ro" \
  ghcr.io/shatadru/labforge:1.0.0
```

## Tailscale ingress

The Helm chart can expose the control plane through the Tailscale Operator.
Set `tailscale.ingress`/the chart's Tailscale ingress values for the release
you use, or add an Ingress with `ingressClassName: tailscale` pointing to the
LabForge Service. The operator provisions the MagicDNS hostname and HTTPS
certificate. The control plane still connects to each KVM host through the
agent URL and bearer token; Tailscale ingress protects the browser/API edge.

## Single-host systemd

Same machine for UI and libvirt: [Install](install.md) (`LOCAL_AGENT=true`).
