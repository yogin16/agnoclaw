"""
Pre-built multi-agent team configurations.

Provides ready-to-use Team presets built on Agno's Team class:
  - research_team()  — web research → analysis → report writing
  - code_team()      — architect → implementer → reviewer
  - data_team()      — data fetcher → analyst → visualizer/reporter

Each preset uses Agno's TeamMode.coordinate by default (leader decomposes,
delegates, and synthesizes). All members share a storage backend.

Usage:
    from agnoclaw.teams import research_team

    team = research_team(model_id="claude-sonnet-4-6")
    team.print_response("Research the state of fusion energy in 2026", stream=True)
"""

from __future__ import annotations

from typing import Optional

from agno.team import Team, TeamMode

from .agent import _resolve_model, _make_db
from .config import HarnessConfig, get_config
from .tools import FilesToolkit, WebToolkit, make_bash_tool, TodoToolkit


def research_team(
    model_id: Optional[str] = None,
    provider: Optional[str] = None,
    config: Optional[HarnessConfig] = None,
    session_id: Optional[str] = None,
    enable_learning: bool = False,
) -> Team:
    """
    A three-agent research team:
      - Researcher: web search + source gathering
      - Analyst: synthesis + critical evaluation
      - Writer: final report production

    TeamMode: coordinate (leader delegates, synthesizes final output)
    """
    from agno.agent import Agent

    cfg = config or get_config()
    model_id = model_id or cfg.default_model
    provider = provider or cfg.default_provider
    db = _make_db(cfg)

    model = _resolve_model(model_id, provider, cfg)
    web = WebToolkit()

    # Learning machine for research patterns (namespaced to this team)
    _learning = None
    if enable_learning:
        from .memory import build_learning_machine
        _learning = build_learning_machine(db=db, namespace="research-team")

    researcher = Agent(
        name="Researcher",
        role="Find factual information from multiple sources. Search broadly, read deeply. Always cite URLs.",
        model=model,
        tools=[web, TodoToolkit()],
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    analyst = Agent(
        name="Analyst",
        role=(
            "Critically evaluate research findings. Identify consensus vs. contested claims. "
            "Note gaps, contradictions, and confidence levels. Do not fabricate."
        ),
        model=model,
        tools=[TodoToolkit()],
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    writer = Agent(
        name="Writer",
        role=(
            "Produce clear, structured, well-cited reports from analysis. "
            "Use Markdown with headers, bullet points, and inline citations [Title](URL). "
            "Write for an expert audience — no fluff."
        ),
        model=model,
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    return Team(
        name="Research Team",
        model=model,
        mode=TeamMode.coordinate,
        members=[researcher, analyst, writer],
        instructions=(
            "1. Have the Researcher gather comprehensive information from multiple sources.\n"
            "2. Have the Analyst evaluate and synthesize the findings critically.\n"
            "3. Have the Writer produce a well-structured final report with citations.\n"
            "Do not skip steps. The final output must include sources."
        ),
        db=db,
        session_id=session_id,
        show_members_responses=True,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )


def code_team(
    model_id: Optional[str] = None,
    provider: Optional[str] = None,
    config: Optional[HarnessConfig] = None,
    session_id: Optional[str] = None,
    enable_learning: bool = False,
) -> Team:
    """
    A three-agent software development team:
      - Architect: design and planning
      - Implementer: code writing
      - Reviewer: code review and testing

    TeamMode: coordinate
    """
    from agno.agent import Agent

    cfg = config or get_config()
    model_id = model_id or cfg.default_model
    provider = provider or cfg.default_provider
    db = _make_db(cfg)

    model = _resolve_model(model_id, provider, cfg)
    files = FilesToolkit()
    bash = make_bash_tool(timeout=cfg.bash_timeout_seconds)

    # Learning for code patterns — namespaced separately from research
    _learning = None
    if enable_learning:
        from .memory import build_learning_machine
        _learning = build_learning_machine(db=db, namespace="code-team")

    architect = Agent(
        name="Architect",
        role=(
            "Design software solutions. Define interfaces, data models, module structure. "
            "Produce clear technical specs before any code is written. "
            "Consider maintainability, performance, and security."
        ),
        model=model,
        tools=[files, TodoToolkit()],
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    implementer = Agent(
        name="Implementer",
        role=(
            "Write clean, correct, idiomatic code following the architect's spec. "
            "Read existing code before modifying. Write tests alongside implementation. "
            "Follow existing project conventions."
        ),
        model=model,
        tools=[files, bash, TodoToolkit()],
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    reviewer = Agent(
        name="Reviewer",
        role=(
            "Review code for bugs, security issues, performance problems, and style. "
            "Run tests and verify they pass. "
            "Produce a structured review with P0/P1/P2/P3 priority issues. "
            "Approve or request changes with specific, actionable feedback."
        ),
        model=model,
        tools=[files, bash],
        db=db,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )

    return Team(
        name="Code Team",
        model=model,
        mode=TeamMode.coordinate,
        members=[architect, implementer, reviewer],
        instructions=(
            "1. Have the Architect design the solution and produce a spec.\n"
            "2. Have the Implementer write the code following the spec.\n"
            "3. Have the Reviewer review, run tests, and confirm correctness.\n"
            "The final output must include: working code + test results + review summary."
        ),
        db=db,
        session_id=session_id,
        show_members_responses=True,
        markdown=True,
        learning=_learning,
        add_learnings_to_context=enable_learning,
    )


def data_team(
    model_id: Optional[str] = None,
    provider: Optional[str] = None,
    config: Optional[HarnessConfig] = None,
    session_id: Optional[str] = None,
) -> Team:
    """
    A two-agent data analysis team:
      - Fetcher: data acquisition and preparation
      - Analyst: analysis, pattern finding, insight generation

    TeamMode: coordinate
    """
    from agno.agent import Agent

    cfg = config or get_config()
    model_id = model_id or cfg.default_model
    provider = provider or cfg.default_provider
    db = _make_db(cfg)

    model = _resolve_model(model_id, provider, cfg)

    fetcher = Agent(
        name="DataFetcher",
        role=(
            "Acquire, clean, and prepare data. "
            "Fetch from APIs, files, or databases. "
            "Describe the data structure clearly for the analyst."
        ),
        model=model,
        tools=[WebToolkit(), FilesToolkit(), make_bash_tool()],
        db=db,
        markdown=True,
    )

    analyst = Agent(
        name="DataAnalyst",
        role=(
            "Analyze prepared data to find patterns, trends, and insights. "
            "Be quantitative and precise. Distinguish correlation from causation. "
            "Present findings clearly with supporting numbers."
        ),
        model=model,
        tools=[FilesToolkit(), make_bash_tool()],
        db=db,
        markdown=True,
    )

    return Team(
        name="Data Team",
        model=model,
        mode=TeamMode.coordinate,
        members=[fetcher, analyst],
        instructions=(
            "1. Have DataFetcher acquire and describe the relevant data.\n"
            "2. Have DataAnalyst perform rigorous analysis and present findings.\n"
            "Be quantitative. Include specific numbers, not vague summaries."
        ),
        db=db,
        session_id=session_id,
        show_members_responses=True,
        markdown=True,
    )
