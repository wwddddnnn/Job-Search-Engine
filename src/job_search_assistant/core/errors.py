"""Stable error types shared by all modules and delivery adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(eq=False)
class ApplicationError(Exception):
    """Base error with an adapter-safe machine code and diagnostic details."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def with_correlation_id(self, correlation_id: str | None) -> "ApplicationError":
        """Attach a correlation identifier if the error does not already have one."""
        if self.correlation_id is None:
            self.correlation_id = correlation_id
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation suitable for adapters and logs."""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        return payload


class ValidationError(ApplicationError):
    """Raised when caller input violates a declared contract."""

    def __init__(
        self,
        message: str = "Input validation failed.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("validation_error", message, details or {})


class NotFoundError(ApplicationError):
    """Raised when a requested resource is absent."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged_details = {"resource_type": resource_type, "resource_id": resource_id}
        if details:
            merged_details.update(details)
        super().__init__("not_found", f"{resource_type} '{resource_id}' was not found.", merged_details)


class ConflictError(ApplicationError):
    """Raised when a request conflicts with the current state or a prior request."""

    def __init__(
        self,
        message: str = "The request conflicts with the current state.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("conflict", message, details or {})


class AuthorizationError(ApplicationError):
    """Raised when the caller lacks permission for an operation."""

    def __init__(
        self,
        message: str = "The caller is not authorized to perform this operation.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("authorization_error", message, details or {})


class InvalidStateError(ApplicationError):
    """Raised when a command is invalid for the target entity's state."""

    def __init__(
        self,
        entity_type: str,
        entity_id: str,
        current_state: str,
        requested_action: str,
    ) -> None:
        super().__init__(
            "invalid_state",
            f"Cannot perform '{requested_action}' while {entity_type} '{entity_id}' is in state "
            f"'{current_state}'.",
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "current_state": current_state,
                "requested_action": requested_action,
            },
        )


class InfrastructureError(ApplicationError):
    """Raised when a required infrastructure dependency cannot complete an operation."""

    def __init__(
        self,
        message: str = "Infrastructure operation failed.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("infrastructure_error", message, details or {})
