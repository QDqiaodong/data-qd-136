"""Exception convention for the close-chain workbench.

Every close-chain error is a :class:`ValueError` subclass so it stays in the
same family as the strict v1 JSON/parser errors and the open-items workbench.
The CLI maps all of them to exit code 2 and leaves the published head
untouched.
"""

__all__ = [
    'CloseChainError',
    'ValidationError',
    'ConflictError',
    'NotFoundError',
    'LimitError',
    'CorruptionError',
]


class CloseChainError(ValueError):
    """Base class for every close-chain failure."""


class ValidationError(CloseChainError):
    """Input shape, identifier, reference, rule or control figure is invalid.

    A rejected apply publishes nothing: the current head, every historical
    generation and every source/result file stay byte-for-byte unchanged.
    """


class ConflictError(CloseChainError):
    """The expected head no longer matches the current head.

    Two independent processes publishing against the same ``expected_head``
    can have at most one winner; the loser is rejected explicitly and never
    silently rebased onto the new head.
    """


class NotFoundError(CloseChainError):
    """A referenced store, generation, period or chain id does not exist."""


class LimitError(CloseChainError):
    """A hard size bound was exceeded (at most 60 periods in a chain)."""


class CorruptionError(CloseChainError):
    """A stored file is missing, unreadable, malformed or fails its hash.

    Reads that fail verification surface this error instead of returning data;
    the head pointer itself is never modified by a read.
    """
