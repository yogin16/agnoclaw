# The sibling release-journey.Dockerfile.dockerignore (a BuildKit per-Dockerfile
# ignore) keeps the workspace out of the build context. With BuildKit disabled
# (DOCKER_BUILDKIT=0) that file is silently ignored and the whole worktree —
# including local .venv-* trees — becomes context: the build still succeeds,
# just very slowly.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

COPY dist/*.whl /opt/agnoclaw/dist/
RUN python -m pip install --disable-pip-version-check --no-cache-dir /opt/agnoclaw/dist/*.whl

COPY scripts/public_api_journey_probe.py /opt/agnoclaw/public_api_journey_probe.py
COPY scripts/agno_stack_restart_probe.py /opt/agnoclaw/agno_stack_restart_probe.py

USER 65532:65532
ENV HOME=/tmp/home
WORKDIR /tmp
ENTRYPOINT ["python", "-I"]
