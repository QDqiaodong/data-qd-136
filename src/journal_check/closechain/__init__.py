"""Local period close-chain workbench.

An immutable, append-only chain of period close results. Each successful
``apply`` publishes a new *generation* containing every period's full source
snapshot and its computed carry-forward result; ``show`` reads any historical
period back and verifies the on-disk hashes.

Public Python interface (all callable directly from tests)::

    from journal_check.closechain import apply, show, open_store
    from journal_check.closechain.engine import compute_chain
    from journal_check.closechain.errors import (
        CloseChainError, ValidationError, ConflictError,
        NotFoundError, LimitError, CorruptionError,
    )

Only the Python standard library is used; there is no network service and no
access to the open-items database.
"""
from .errors import (
    CloseChainError,
    ConflictError,
    CorruptionError,
    LimitError,
    NotFoundError,
    ValidationError,
)
from .store import apply, open_store, show

__all__ = [
    'apply',
    'show',
    'open_store',
    'CloseChainError',
    'ValidationError',
    'ConflictError',
    'NotFoundError',
    'LimitError',
    'CorruptionError',
]
