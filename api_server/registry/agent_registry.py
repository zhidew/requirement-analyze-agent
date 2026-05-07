"""Compatibility wrapper for legacy AgentRegistry imports."""

from .expert_registry import AgentFullConfig, AgentManifest, AgentRegistry

__all__ = ["AgentManifest", "AgentFullConfig", "AgentRegistry"]
