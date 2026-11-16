"""Exception convention for the open-items workbench.

Every workbench error is a :class:`ValueError` subclass so it stays in the
same family as the strict v1 JSON/parser errors. The CLI maps all of them to
exit code 2 and leaves any prior output untouched.
"""

__all__ = [
    'OpenItemError',
    'ValidationError',
    'ConflictError',
    'NotFoundError',
    'ExhaustedError',
    'LimitError',
]


class OpenItemError(ValueError):
    """Base class for every open-items workbench failure."""


class ValidationError(OpenItemError):
    """Input shape, identifier, reference or rule is invalid.

    A rejected ingest never creates a partial batch: no account, journal
    entry or open item is persisted.
    """


class ConflictError(OpenItemError):
    """A persisted uniqueness, idempotency or plan-state check failed.

    Examples: the same ``batch_id`` is reused with different bytes, an open
    item id already exists, or a plan is committed against a component that
    changed after planning. Nothing is written when this is raised.
    """


class NotFoundError(OpenItemError):
    """A referenced batch, open item or plan id does not exist."""


class ExhaustedError(OpenItemError):
    """A seed open item has no remaining quantity and cannot be planned."""


class LimitError(OpenItemError):
    """A hard graph or request size bound was exceeded.

    The operation fails explicitly: results are never truncated and capacities
    are never expanded into unit-sized nodes.
    """
