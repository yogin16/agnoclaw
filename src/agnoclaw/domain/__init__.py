"""Opt-in domain harness SDK preview. Business policy and persistence stay with the host."""

from .actions import ActionProposal, ActionSpec
from .context import ContextBundle, ContextContribution, ContextContributor, assemble_context
from .decisions import (
    DecisionResult,
    DecisionRule,
    DecisionStatus,
    DecisionTable,
    InferencePolicy,
    InferenceRoute,
    RouteDecision,
)
from .evaluation import CaseResult, EvaluationCase, EvaluationSummary, evaluate_cases
from .evidence import EvidenceRef, QuoteMatch, TextSource, locate_quote
from .feedback import FeedbackEvent, FeedbackKind
from .interaction import ReviewDecision, ReviewRequest, ReviewVerdict
from .objects import ObjectRef, ObjectSnapshot
from .outputs import OutputSpec, WorkProduct, bind_output_capability, bind_output_schema
from .procedures import DomainPack, ProcedureSpec
from .verification import CheckResult, CheckStatus, VerificationReport, check_quote, run_check

__all__ = [
    "ActionProposal",
    "ActionSpec",
    "CaseResult",
    "ContextBundle",
    "ContextContribution",
    "ContextContributor",
    "DecisionResult",
    "DecisionRule",
    "DecisionStatus",
    "DecisionTable",
    "DomainPack",
    "EvaluationCase",
    "EvaluationSummary",
    "FeedbackEvent",
    "FeedbackKind",
    "InferencePolicy",
    "InferenceRoute",
    "ProcedureSpec",
    "ReviewDecision",
    "ReviewRequest",
    "ReviewVerdict",
    "RouteDecision",
    "assemble_context",
    "evaluate_cases",
    "CheckResult",
    "CheckStatus",
    "EvidenceRef",
    "ObjectRef",
    "ObjectSnapshot",
    "OutputSpec",
    "QuoteMatch",
    "TextSource",
    "VerificationReport",
    "WorkProduct",
    "bind_output_capability",
    "bind_output_schema",
    "check_quote",
    "locate_quote",
    "run_check",
]
