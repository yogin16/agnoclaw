"""Token-free smoke test for the exact wheel's Agno 3 CodeMode extra."""

from __future__ import annotations

from tempfile import TemporaryDirectory

from agno.run import RunContext

from agnoclaw.agno3 import resolve_code_mode
from agnoclaw.compat import AgnoFeature, inspect_agno_compatibility


def main() -> None:
    compatibility = inspect_agno_compatibility()
    compatibility.require(AgnoFeature.V3_CODE_MODE)
    with TemporaryDirectory(prefix="agnoclaw-code-smoke-") as workdir:
        code_mode, owned = resolve_code_mode(
            explicit=True,
            enabled=True,
            tools=[],
            db=None,
            profile="quick",
            scope_key="exact-wheel-smoke",
            cwd=workdir,
            allow_shell=False,
            snapshot=False,
            timeout_seconds=30,
            idle_ttl_seconds=30,
            max_kernels=1,
            compatibility=compatibility,
        )
        assert code_mode is not None and owned is not None
        context = RunContext(
            run_id="run-code-smoke",
            session_id="session-code-smoke",
            user_id="wheel-smoke",
        )
        try:
            result = code_mode.execute(context, "20 + 22")
            assert "42" in str(result.content)
            shell = code_mode.execute(context, "%%bash\nprintf forbidden")
            assert "disabled" in str(shell.content)
        finally:
            owned.close()


if __name__ == "__main__":
    main()
