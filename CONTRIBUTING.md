# Contributing to agnoclaw

## Quick start

```bash
git clone https://github.com/agnoclaw/agnoclaw
cd agnoclaw
uv sync --dev
uv run pytest tests/ -q
```

## Development workflow

1. **Branch** from `main`: `git checkout -b feature/my-feature`
2. **Write tests first** — all new functionality needs tests
3. **Run tests locally**: `uv run pytest tests/ -q`
4. **Lint**: `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`
5. **Open a PR** — CI runs automatically

## Code style

- **Formatter**: ruff (configured in `pyproject.toml`)
- **Linter**: ruff (E, F, I, UP, B rules)
- **Type hints**: required on all public functions
- **Docstrings**: only on public classes and non-obvious functions
- **Line length**: 100 chars

Run everything in one shot:
```bash
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
```

## Tests

- All new code must have tests
- Tests live in `tests/` mirroring the `src/agnoclaw/` structure
- Use `tmp_path` (pytest built-in) or the fixtures in `tests/conftest.py`
- Tests that require API keys must be marked with `pytest.mark.integration`
  and skipped by default:

```python
@pytest.mark.integration
def test_real_api_call():
    ...
```

Run only unit tests (no API calls):
```bash
uv run pytest tests/ -q -m "not integration"
```

## Adding a built-in skill

Built-in skills live in `skills/<skill-name>/SKILL.md`. Copy an existing skill
as a template and follow the AgentSkills frontmatter format (see
`src/agnoclaw/skills/loader.py` for all supported fields).

## Adding a new tool

1. Write the tool in `src/agnoclaw/tools/`
2. Register it in `src/agnoclaw/tools/__init__.py` if it belongs in the default suite
3. Add tests in `tests/test_tools.py`

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(tools): add WebhookToolkit for outbound notifications
fix(heartbeat): handle overnight active hours correctly
docs(examples): add openclaw_style reference example
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`
