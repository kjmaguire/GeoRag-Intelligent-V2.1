"""Wrapper-raised exceptions. Agents may also raise these to signal outcome."""


class AgentError(Exception):
    """Base class for all wrapper-mediated agent errors."""


class AgentTimeoutError(AgentError):
    """Soft or hard timeout exceeded."""


class AgentCircuitOpenError(AgentError):
    """Circuit breaker is open for this (agent, workspace) pair."""


class AgentRefusalError(AgentError):
    """Agent deliberately refused — counts as outcome='refusal' (not 'failure').

    The wrapper does NOT increment the failure counter for refusals — they
    are intentional, well-formed outputs. Master plan §35.1 distinguishes
    refusal from failure precisely so circuit breakers don't trip on
    deliberate "I cannot answer this" responses.
    """
