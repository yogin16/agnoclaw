"""Agno-native progressive skill disclosure with agnoclaw trust enforcement."""

from __future__ import annotations

import json
from collections.abc import Callable

from agno.tools.function import Function

from .registry import (
    ModelSkillActivation,
    ModelSkillActivationError,
    SkillRegistry,
)


class AgnoClawSkills:
    """Duck-typed Agno ``Skills`` adapter over :class:`SkillRegistry`.

    Agno owns when the access function is exposed to its model loop. Agnoclaw owns
    source trust, bounded disclosure, lifecycle hooks, policy, and the active skill's
    tool allowlist. Script execution is deliberately absent from this surface: a model
    must use an ordinary governed tool instead of an untracked subprocess shortcut.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        on_activation: Callable[[ModelSkillActivation], str],
        prepare_function: Callable[[Function], None],
        enabled: Callable[[], bool],
    ) -> None:
        self._registry = registry
        self._on_activation = on_activation
        self._prepare_function = prepare_function
        self._enabled = enabled

    def get_system_prompt_snippet(self) -> str:
        """Return the compact trust-filtered catalog expected by Agno's Skills API."""
        return self._registry.get_skill_descriptions() if self._enabled() else ""

    def get_tools(self) -> list[Function]:
        """Materialize one governed, run-owned progressive-disclosure function."""
        if not self._enabled():
            return []
        function = Function(
            name="get_skill_instructions",
            description=(
                "Load one trusted skill's full instructions. Call this as a standalone "
                "tool step before using any tools required by that skill. Community "
                "skills and skills requiring a model/context/schema change cannot be "
                "activated this way."
            ),
            entrypoint=self.get_skill_instructions,
        )
        self._prepare_function(function)
        return [function]

    def get_skill_instructions(self, skill_name: str, arguments: str = "") -> str:
        """Return a bounded instruction envelope for one eligible skill."""
        try:
            activation = self._registry.activate_for_model(skill_name, arguments)
            return self._on_activation(activation)
        except ModelSkillActivationError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )


__all__ = ["AgnoClawSkills"]
