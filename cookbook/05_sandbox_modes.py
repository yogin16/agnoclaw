"""
Cookbook 5: Session Sandbox Modes

Demonstrates:
- SandboxMode enum: WORKSPACE_WRITE, READ_ONLY, FULL
- Setting sandbox mode via string alias or enum
- Mode aliases: "workspace-write"/"rw", "read-only"/"ro", "full"/"host"
- Impact on built-in files/bash tool surface
- Reading current sandbox mode at runtime

Run: uv run python cookbook/05_sandbox_modes.py
"""

from agnoclaw import AgentHarness, SandboxMode


def main():
    # ── Create harness in read_only mode ────────────────────────────────────
    # In read_only mode, the built-in files/bash tools are constrained to
    # read-only workspace access. Writes and modifications are blocked.
    harness_ro = AgentHarness(
        model="ollama:llama3.2",
        name="sandbox-ro-demo",
        sandbox_mode="read_only",  # string alias
        session_id="cookbook-sandbox-ro",
    )
    print(f"read_only harness sandbox mode: {harness_ro.sandbox_mode}")
    assert harness_ro.sandbox_mode == "read_only"

    # ── Workspace-write mode (default) ─────────────────────────────────────
    # Tools can read and write within the workspace directory.
    harness_rw = AgentHarness(
        model="ollama:llama3.2",
        name="sandbox-rw-demo",
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,  # enum
        session_id="cookbook-sandbox-rw",
    )
    print(f"workspace_write harness sandbox mode: {harness_rw.sandbox_mode}")

    # ── Full / unrestricted mode ───────────────────────────────────────────
    # No sandbox restrictions on the built-in tools.
    harness_full = AgentHarness(
        model="ollama:llama3.2",
        name="sandbox-full-demo",
        sandbox_mode="full",  # "host" and "none" are also valid aliases
        session_id="cookbook-sandbox-full",
    )
    print(f"full harness sandbox mode: {harness_full.sandbox_mode}")

    # ── Alias normalization ────────────────────────────────────────────────
    from agnoclaw import normalize_sandbox_mode

    assert normalize_sandbox_mode("workspace-write") == SandboxMode.WORKSPACE_WRITE
    assert normalize_sandbox_mode("rw") == SandboxMode.WORKSPACE_WRITE
    assert normalize_sandbox_mode("read-only") == SandboxMode.READ_ONLY
    assert normalize_sandbox_mode("ro") == SandboxMode.READ_ONLY
    assert normalize_sandbox_mode("host") == SandboxMode.FULL
    assert normalize_sandbox_mode("none") == SandboxMode.FULL
    print("All sandbox mode aliases normalized correctly.\n")

    # ── Effect on runtime ──────────────────────────────────────────────────
    # The sandbox mode propagates to:
    #   1. Built-in files tools (read/write restrictions)
    #   2. Bash tool working directory constraints
    #   3. Spawned subagents inherit the mode
    #   4. Prompt metadata includes the mode
    #   5. Admin/debug surfaces expose it

    print(f"RO harness:  {harness_ro.sandbox_mode}")
    print(f"RW harness:  {harness_rw.sandbox_mode}")
    print(f"Full harness: {harness_full.sandbox_mode}")


if __name__ == "__main__":
    main()
