"""Pydantic models for structured LLM outputs."""

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput

__all__ = ["ParsedInput", "AdjudicationResult"]
