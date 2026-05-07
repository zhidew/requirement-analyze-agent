"""
Registry Error Classes

Custom exceptions for the registry module with detailed error information.
"""


class RegistryError(Exception):
    """Base exception for all registry-related errors."""
    
    def __init__(self, message: str, details: dict = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> dict:
        """Convert exception to dictionary for API responses."""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
        }


class ConfigLoadError(RegistryError):
    """Raised when configuration file loading fails."""
    
    def __init__(self, path: str, reason: str, details: dict = None):
        self.path = path
        self.reason = reason
        message = f"Failed to load config from '{path}': {reason}"
        super().__init__(message, details)


class SkillParseError(RegistryError):
    """Raised when SKILL.md parsing fails."""
    
    def __init__(self, path: str, reason: str, details: dict = None):
        self.path = path
        self.reason = reason
        message = f"Failed to parse SKILL.md '{path}': {reason}"
        super().__init__(message, details)


class ToolNotAllowedError(RegistryError):
    """Raised when a tool call is not permitted for the current agent."""
    
    def __init__(self, tool: str, capability: str, allowed: list, details: dict = None):
        self.tool = tool
        self.capability = capability
        self.allowed = allowed
        message = f"Tool '{tool}' is not allowed for agent '{capability}'. Allowed tools: {allowed}"
        super().__init__(message, details)


class AgentNotFoundError(RegistryError):
    """Raised when requested agent is not found in registry."""
    
    def __init__(self, capability: str, details: dict = None):
        self.capability = capability
        message = f"Agent '{capability}' not found in registry"
        super().__init__(message, details)


class ValidationError(RegistryError):
    """Raised when configuration validation fails."""
    
    def __init__(self, field: str, value: any, reason: str, details: dict = None):
        self.field = field
        self.value = value
        self.reason = reason
        message = f"Validation failed for field '{field}': {reason}"
        super().__init__(message, details)
