FROM python:3.13-slim

COPY dist/*.whl /opt/agnoclaw/dist/
RUN python -m pip install --disable-pip-version-check --no-cache-dir /opt/agnoclaw/dist/*.whl

COPY scripts/public_api_journey_probe.py /opt/agnoclaw/public_api_journey_probe.py
COPY scripts/agno_stack_restart_probe.py /opt/agnoclaw/agno_stack_restart_probe.py

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-I"]
