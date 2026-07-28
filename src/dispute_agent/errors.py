from __future__ import annotations


class DisputeAgentError(Exception):
    code = "DISPUTE_AGENT_ERROR"


class NotFoundError(DisputeAgentError):
    code = "NOT_FOUND"


class ValidationError(DisputeAgentError):
    code = "VALIDATION_ERROR"


class AuthorizationError(DisputeAgentError):
    code = "NOT_AUTHORIZED"


class ConflictError(DisputeAgentError):
    code = "CONFLICT"


class BudgetExceededError(DisputeAgentError):
    code = "BUDGET_EXCEEDED"


class StateTransitionError(DisputeAgentError):
    code = "INVALID_STATE_TRANSITION"


class PolicySelectionError(DisputeAgentError):
    code = "POLICY_SELECTION_ERROR"


class ModelBackendError(DisputeAgentError):
    code = "MODEL_BACKEND_ERROR"


class QueueUnavailableError(DisputeAgentError):
    code = "QUEUE_UNAVAILABLE"
