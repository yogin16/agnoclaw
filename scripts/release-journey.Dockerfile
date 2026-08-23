# The sibling release-journey.Dockerfile.dockerignore (a BuildKit per-Dockerfile
# ignore) keeps the workspace out of the build context. With BuildKit disabled
# (DOCKER_BUILDKIT=0) that file is silently ignored and the whole worktree —
# including local .venv-* trees — becomes context: the build still succeeds,
# just very slowly.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

COPY dist/*.whl /opt/agnoclaw/dist/
RUN python -m pip install --disable-pip-version-check --no-cache-dir /opt/agnoclaw/dist/*.whl

COPY scripts/public_api_journey_probe.py /opt/agnoclaw/public_api_journey_probe.py
COPY scripts/agno_stack_restart_probe.py /opt/agnoclaw/agno_stack_restart_probe.py

USER 65532:65532
ENV HOME=/tmp/home
WORKDIR /tmp
ENTRYPOINT ["python", "-I"]
