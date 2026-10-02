# Deploy

Control plane artifacts: container image, Helm, Kustomize, or
`install-local.sh`. Agent artifact: `labforge-agent` `.deb` / `.rpm` only.

Defaults can differ between Helm and Kustomize — check the values you apply.

## Agent package

```bash
make package
sudo dpkg -i dist/labforge-agent_*.deb   # or: sudo rpm -i dist/labforge-agent-*.rpm
# set AGENT_TOKEN (and AGENT_BIND if remote) in /etc/labforge/labforge-agent.env
sudo systemctl enable --now labforge-agent
```

Installs under `/usr/lib/labforge`, config at `/etc/labforge/labforge-agent.env`.

## Helm (control only)

```bash
kubectl create namespace labforge
kubectl -n labforge create secret generic labforge-ssh-keys \
  --from-file=authorized_keys=$HOME/.ssh/id_ed25519.pub
kubectl -n labforge create secret generic labforge-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"
helm install labforge charts/labforge \
  --namespace labforge \
  --set image.repository=ghcr.io/OWNER/labforge \
  --set image.tag=0.1.0 \
  --set agent.url=http://kvm-host:8443 \
  --set agent.tokenSecretName=labforge-agent-token \
  --set sshKeys.secretName=labforge-ssh-keys
```

Uses `HOST_MODE=remote`. Put the same token on the agent; set `AGENT_BIND` so
the cluster can reach it. Options: `charts/labforge/values.yaml`.

## Kustomize

```bash
kubectl create namespace labforge
kubectl -n labforge create secret generic labforge-ssh-keys \
  --from-file=authorized_keys=$HOME/.ssh/id_ed25519.pub
kubectl -n labforge create secret generic labforge-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl apply -k k8s/overlays/dev
```

Set `AGENT_URL` in `k8s/base/configmap.yaml` (or patch). Replace
`ghcr.io/OWNER/labforge` with your image.

## Container

```bash
docker build -t labforge:0.1.0 backend
docker run --rm -p 8000:8000 \
  -e MODE=control \
  -e HOST_MODE=remote \
  -e AGENT_URL=http://kvm-host:8443 \
  -e AGENT_TOKEN="$(cat /path/to/agent-token)" \
  -e SSH_PUBLIC_KEYS_FILE=/etc/labforge/ssh/authorized_keys \
  -v "$HOME/.ssh/labforge_authorized_keys:/etc/labforge/ssh/authorized_keys:ro" \
  labforge:0.1.0
```

## Single-host systemd

See [Install](install.md). `LOCAL_AGENT=true` runs control and agent in one
process.
