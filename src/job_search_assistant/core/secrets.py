"""Small credential port. Values must never enter DTOs, logs or audit events."""

from typing import Protocol


class SecretStore(Protocol):
    def get(self, identifier: str) -> str | None: ...

    def set(self, identifier: str, value: str) -> None: ...

    def delete(self, identifier: str) -> None: ...


class MemorySecretStore:
    """Ephemeral implementation for isolated tests."""

    def __init__(self):
        self._values = {}

    def get(self, identifier):
        return self._values.get(identifier)

    def set(self, identifier, value):
        self._values[identifier] = value

    def delete(self, identifier):
        self._values.pop(identifier, None)
