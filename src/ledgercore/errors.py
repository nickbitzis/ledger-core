"""Exceptions the ledger raises. The API maps each one to an HTTP status."""


class LedgerError(Exception):
    """Base class. `code` is a stable machine-readable name."""

    code = "ledger_error"


class ValidationError(LedgerError):
    """The request can never succeed as written (unbalanced, float amount, bad currency)."""

    code = "validation_error"


class InsufficientFunds(LedgerError):
    """A posting would push an account below zero or spend held funds."""

    code = "insufficient_funds"


class InvalidOperation(LedgerError):
    """The request is well formed but not allowed in the current state."""

    code = "invalid_operation"


class NotFound(LedgerError):
    code = "not_found"


class ConflictError(LedgerError):
    """Optimistic concurrency check failed. Safe to retry with the same Idempotency-Key."""

    code = "conflict"

    def __init__(self, message: str, retry_after_ms: int = 50) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class IdempotencyMismatch(LedgerError):
    """The Idempotency-Key was used before with a different request body."""

    code = "idempotency_mismatch"
