"""Run agnoclaw against a Docker-backed llm-sandbox session."""

from agnoclaw import AgentHarness
from agnoclaw.integrations import LLMSandboxBackend


def main() -> None:
    workspace_dir = "workspace"
    backend = LLMSandboxBackend(
        sync_paths=["workspace/inputs"],
    ).bind(workspace_dir)

    agent = AgentHarness(
        "anthropic:claude-sonnet-4-6",
        workspace_dir=workspace_dir,
        backend=backend,
    )
    agent.print_response(
        "Summarize the files under workspace/inputs and write a short report to "
        "workspace/outputs/report.md",
        stream=True,
    )
    backend.sync_from_runtime("workspace/outputs")


if __name__ == "__main__":
    main()
