"""
Example: SkillHub Demo — search, inspect, install skills from ClawHub

Demonstrates the ClawHub client API and skill installation workflow.
Works both as a standalone demo and as embedded library integration.

Run: uv run python examples/skillhub_demo.py
Requires: Network access to clawhub.ai (no API key needed for reads)
"""

from pathlib import Path
from tempfile import mkdtemp

from agnoclaw.skills.hub import ClawHubClient
from agnoclaw.skills.registry import SkillRegistry


def main():
    print("=" * 60)
    print("SkillHub Demo — ClawHub Integration")
    print("=" * 60)

    # ── Direct API usage ────────────────────────────────────────────────

    client = ClawHubClient()

    # Search for skills
    print("\n1. Searching ClawHub for 'code review'...")
    results = client.search("code review")
    if results:
        for skill in results[:5]:
            print(f"  - {skill.name}: {skill.description[:60]}")
    else:
        print("  (No results — ClawHub may be unreachable. Using demo mode.)")
        _demo_mode(client)
        return

    # Inspect a skill
    if results:
        name = results[0].name
        print(f"\n2. Inspecting skill: {name}")
        detail = client.inspect(name)
        if detail:
            print(f"  Name: {detail.name}")
            print(f"  Author: {detail.author}")
            print(f"  Version: {detail.version}")
            print(f"  Categories: {', '.join(detail.categories)}")
            print(f"  Dependencies: {', '.join(detail.dependencies) or 'none'}")

    # Download to temp directory
    dest = Path(mkdtemp()) / "skills"
    dest.mkdir(parents=True, exist_ok=True)

    if results:
        name = results[0].name
        print(f"\n3. Downloading skill '{name}' to {dest}...")
        skill_dir = client.download(name, dest)
        if skill_dir:
            print(f"  Installed to: {skill_dir}")
            print(f"  SKILL.md exists: {(skill_dir / 'SKILL.md').exists()}")

    # Browse categories
    print("\n4. ClawHub categories:")
    categories = client.categories()
    for cat in categories[:10]:
        print(f"  - {cat}")

    client.close()

    # ── Registry integration ────────────────────────────────────────────

    print("\n5. Registry integration: install_from_hub()")
    registry = SkillRegistry(workspace_skills_dir=dest)
    if results:
        name = results[0].name
        installed = registry.install_from_hub(name)
        if installed:
            # Verify the skill loads
            content = registry.load_skill(name)
            print(f"  Skill '{name}' loads: {bool(content)}")
            print(f"  Content preview: {content[:100]}..." if content else "  (empty)")

    print("\nDone.")


def _demo_mode(client):
    """Offline demo showing the API pattern without network."""
    print("\n  [Demo mode: showing API patterns without network]\n")

    print("  ClawHubClient API:")
    print("    client.search('code review')  → list[HubSkillInfo]")
    print("    client.inspect('skill-name')  → HubSkillDetail")
    print("    client.download('skill-name', dest_dir)  → Path")
    print("    client.categories()  → list[str]")

    print("\n  SkillRegistry integration:")
    print("    registry.install_from_hub('skill-name')  → Path")
    print("    registry.load_skill('skill-name')  → str (rendered content)")

    print("\n  CLI commands:")
    print("    agnoclaw hub search 'code review'")
    print("    agnoclaw hub inspect coding-agent")
    print("    agnoclaw hub install coding-agent")
    print("    agnoclaw hub categories")

    client.close()


if __name__ == "__main__":
    main()
