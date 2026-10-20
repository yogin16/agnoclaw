# Clarion-shaped domain package

Read the [package authoring guide](../../docs/domain-packages.md), then inspect
[the package](clarion_domain/__init__.py) and [host demo](demo.py).

From the agnoclaw repository:

```bash
uv sync --extra dev
uv run python examples/clarion_domain_pack/demo.py
```

This example imports public constructs and performs no model calls. It demonstrates
definition composition, validation, checked output, schema aliases, offline fixtures,
pinned skill resources, trigger planning and watcher accounting. Business events are
simulated; actual persistence, authorization and review belong to the embedding host.

For distribution, build this directory with `uv build --project examples/clarion_domain_pack`.
`InstalledResources` reads the skill included in its wheel. The optional
`agnoclaw-pack.toml` supports the existing trusted pack loader from a source/installed
pack directory. The normal Python API also works without that loader.
