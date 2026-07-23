# syntax=docker/dockerfile:1
# Build stage: install deps into a venv.
# Pinned to Python 3.13 (the tested/locked dependency set ships 3.13 wheels). The
# previous Chainguard `latest-dev` base used a moving tag that drifted to a newer
# Python without wheels for our pins, so the venv build failed. Pinning the minor
# version keeps builds reproducible while still receiving patch/security updates.
FROM python:3.13-slim-trixie AS build
WORKDIR /app
COPY requirements.txt requirements-overrides-security.txt azure_mgmt_keep.txt ./
# Install the pinned set, then force-upgrade cryptography + PyJWT to patched
# releases: c7n 0.9.51 / c7n-azure 0.7.50 hard-pin the vulnerable
# cryptography==46.0.7 (GHSA-537c-gmf6-5ccf) and PyJWT 2.12.x (CVE-2026-48526),
# but both are API-compatible with c7n's usage (validated by the test suite, which
# applies the SAME override file), so a --no-deps override clears the CVEs without
# disturbing the resolved tree. The patched versions live in a single source of truth
# (requirements-overrides-security.txt), shared with the CI backend job. See issue #58.
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /venv/bin/pip install --no-cache-dir --no-deps --upgrade \
      -r requirements-overrides-security.txt

# Prune unused azure-mgmt provider SDKs (issue #129 / M13.6). c7n-azure==0.7.50
# hard-pulls ~56 azure-mgmt-* providers (~480 MB) but imports them LAZILY, so only
# the handful CloudWarden runs are ever loaded. Delete every azure/mgmt/<provider>
# dir NOT in azure_mgmt_keep.txt (~214 MB / 39 providers). The smoke guard then
# re-registers all azure.* resources and imports every kept provider + the
# c7n-azure core modules — FAILING THE BUILD if the prune removed something needed,
# so an over-prune can never ship. Drift is caught in CI by test_azure_footprint.py.
RUN MGMT="$(/venv/bin/python -c 'import azure.mgmt; print(list(azure.mgmt.__path__)[0])')" \
 && for d in "$MGMT"/*/ ; do \
      name="$(basename "$d")"; \
      grep -qxF "$name" azure_mgmt_keep.txt || rm -rf "$d"; \
    done \
 && /venv/bin/python -c "import importlib, pathlib; import c7n_azure.entry; from c7n.resources import load_resources; load_resources(('azure.*',)); keep=[l.strip() for l in pathlib.Path('azure_mgmt_keep.txt').read_text().splitlines() if l.strip() and not l.lstrip().startswith('#')]; [importlib.import_module('azure.mgmt.'+p) for p in keep]; import c7n_azure.session, c7n_azure.query, c7n_azure.tags, c7n_azure.utils; print('azure-mgmt prune smoke OK:', len(keep), 'providers kept')"

# Runtime stage: same pinned base, minimal, run as a nonroot user (uid 65532 to
# match the appdata volume perms wired in docker-compose.yml).
FROM python:3.13-slim-trixie
WORKDIR /app
ENV PATH="/venv/bin:$PATH" \
    HOME=/home/nonroot \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DATA_DIR=/data
COPY --from=build /venv /venv
COPY cloudwarden ./cloudwarden
# Nonroot user + a writable data dir for the appdata named volume.
RUN useradd -u 65532 -m -d /home/nonroot -s /usr/sbin/nologin nonroot \
 && mkdir -p /data \
 && chown -R 65532:65532 /data /app
USER 65532:65532
ENTRYPOINT ["python", "-m", "cloudwarden.cli"]
CMD ["api"]
