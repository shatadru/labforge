# Development

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
pytest --cov=app --cov-fail-under=80
tox          # optional local isolated env
```

Jinja templates use strict undefined in tests.

## Versioning

Single source: top-level `VERSION`, resolved by `scripts/version.sh`:

| Context | Version |
|---------|---------|
| Tag `vX.Y.Z` | `X.Y.Z` |
| CI on a branch | `X.Y.Z-ci.<short-sha>` |
| Local, no exact tag | `X.Y.Z-dev` |

```bash
make bump-minor   # also bump-patch, bump-major
git commit -am "release 0.2.0"
git tag v0.2.0 && git push --follow-tags
```

## CI

`.github/workflows/ci.yml` on PR and push to `main`:

- pytest (80% coverage) + compile
- agent smoke (token auth)
- package build + install on Debian 12 / Fedora 40
- Helm lint/package + kind install
- systemd-analyze (any output fails; canary broken unit)
- image build + Trivy on PRs
