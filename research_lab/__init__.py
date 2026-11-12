"""Offline research contracts; separate from MCP providers and trading execution."""
from .core import (ContractError, Observation, ResearchSignal, HypothesisAssessment,
                   evaluate_hypothesis, validate_replay, ResearchLedger)
