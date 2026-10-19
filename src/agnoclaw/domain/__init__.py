"""Opt-in domain harness SDK preview. Business policy and persistence stay with the host."""

from .actions import ActionProposal, ActionSpec
from .authoring import Check, CheckOutcome, Domain, OutputSchema, PreparedWork, Procedure
from .changes import ChangeConflict, ChangeResolution, ChangeSet
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
from .dispatch import Action, PreparedAction
from .evaluation import CaseResult, EvaluationCase, EvaluationSummary, evaluate_cases
from .evidence import EvidenceRef, QuoteMatch, TextSource, locate_quote
from .feedback import FeedbackEvent, FeedbackKind
from .identity import ContractRef, DeduplicationStatus, OperationIdentity
from .interaction import ReviewDecision, ReviewRequest, ReviewVerdict
from .links import ObjectLink
from .objects import ObjectId, ObjectRef, ObjectSnapshot
from .outputs import OutputSpec, WorkProduct, bind_output_capability, bind_output_schema
from .procedures import DomainPack, ProcedureSpec
from .verification import CheckResult, CheckStatus, VerificationReport, check_quote, run_check

__all__ = [
    "Action",
    "ActionProposal",
    "ActionSpec",
    "CaseResult",
    "ChangeConflict",
    "ChangeResolution",
    "ChangeSet",
    "Check",
    "CheckOutcome",
    "ContractRef",
    "ContextBundle",
    "ContextContribution",
    "ContextContributor",
    "DecisionResult",
    "DecisionRule",
    "DecisionStatus",
    "DecisionTable",
    "DomainPack",
    "Domain",
    "DeduplicationStatus",
    "EvaluationCase",
    "EvaluationSummary",
    "FeedbackEvent",
    "FeedbackKind",
    "InferencePolicy",
    "InferenceRoute",
    "ProcedureSpec",
    "Procedure",
    "PreparedAction",
    "PreparedWork",
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
    "ObjectId",
    "ObjectLink",
    "OperationIdentity",
    "ObjectSnapshot",
    "OutputSpec",
    "OutputSchema",
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
