# The sibling release-journey.Dockerfile.dockerignore (a BuildKit per-Dockerfile
# ignore) keeps the workspace out of the build context. With BuildKit disabled
# (DOCKER_BUILDKIT=0) that file is silently ignored and the whole worktree —
# including local .venv-* trees — becomes context: the build still succeeds,
# just very slowly.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

COPY dist/*.whl /opt/agnoclaw/dist/
RUN python -m pip install --disable-pip-version-check --no-cache-dir /opt/agnoclaw/dist/*.whl

COPY scripts/public_api_journey_probe.py /opt/agnoclaw/public_api_journey_probe.py
COPY scripts/agno_stack_restart_probe.py /opt/agnoclaw/agno_stack_restart_probe.py

USER 65532:65532
ENV HOME=/tmp/home
WORKDIR /tmp
ENTRYPOINT ["python", "-I"]
