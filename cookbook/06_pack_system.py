"""
Cookbook 6: Pack System

Demonstrates:
- Creating an agnoclaw pack with agnoclaw-pack.toml manifest
- Inspecting a pack without executing code
- Loading a trusted pack (tools, hooks, policies)
- Installing packs from local paths or git+ URLs
- Registering packs with AgentHarness

Run: uv run python cookbook/06_pack_system.py
"""

import os
import tempfile

from agnoclaw import (
    AgentHarness,
    LoadedPack,
    PackManifest,
    inspect_pack,
    install_pack,
    load_pack,
    trust_pack,
)


def main():
    # ── Create a demo pack on disk ──────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        pack_dir = os.path.join(tmpdir, "my-pack")
        os.makedirs(pack_dir)

        # Create agnoclaw-pack.toml manifest
        manifest_path = os.path.join(pack_dir, "agnoclaw-pack.toml")
        with open(manifest_path, "w") as f:
            f.write("""\
name = "my-demo-pack"
version = "1.0.0"
description = "A demo pack with a simple custom tool"

[tool.my_greeter]
module = "my_pack_tools"
function = "greet_user"

[[skills]]
name = "greeting-skill"
path = "skills/greeting.SKILL.md"

[trust]
default = "local"
requires_code_execution = false
""")

        # Create a skills directory
        skills_dir = os.path.join(pack_dir, "skills")
        os.makedirs(skills_dir)
        with open(os.path.join(skills_dir, "greeting.SKILL.md"), "w") as f:
            f.write("""\
---
name: greeting-skill
description: A friendly greeting skill
---
When asked to greet someone, use a warm and friendly tone.
Always address them by name when provided.
""")

        # ── 1. Inspect pack (no code executed) ────────────────────────────
        manifest: PackManifest = inspect_pack(pack_dir)
        print(f"Pack: {manifest.name} v{manifest.version}")
        print(f"  Description: {manifest.description}")
        print(f"  Provides skills: {manifest.provides.skills}")
        print(f"  Root: {manifest.root}")
        print()

        # ── 2. Load pack without trust (no code executed) ─────────────────
        loaded: LoadedPack = load_pack(pack_dir, trusted=False)
        print(f"Loaded pack: tools={len(loaded.tools)}, "
              f"skills_dirs={loaded.skills_dirs}")
        print()

        # ── 3. Install pack to local store ────────────────────────────────
        installed_manifest = install_pack(pack_dir)
        print(f"Installed: {installed_manifest.name} @ {installed_manifest.root}")
        print()

        # ── 4. Trust a pack ───────────────────────────────────────────────
        trusted = trust_pack(manifest.name)
        print(f"Trusted: {trusted.name} (trust default={trusted.trust.default})")
        print()

        # ── 5. Load pack into harness ─────────────────────────────────────
        harness_from_pack = AgentHarness(
            model="ollama:llama3.2",
            name="pack-demo",
            packs=[pack_dir],
            trusted_packs=True,
            session_id="cookbook-packs",
        )
        print(f"Harness '{harness_from_pack.name}' created with pack: {manifest.name}")

        # Access pack tools from loaded pack
        if loaded.tools:
            print(f"  Pre-loaded tools: {[t.name for t in loaded.tools]}")


if __name__ == "__main__":
    main()
