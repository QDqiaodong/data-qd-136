"""Exception convention for the quote-evidence translation workbench.

Every translation error is a :class:`ValueError` subclass so it stays in the
same family as the strict v1 JSON/parser errors and the other workbenches.
The CLI maps all of them to exit code 2 and leaves any existing output file
untouched.

A pending entry (a journal entry whose currency is missing or contradicted by
the quotes on its own date) is **not** an error: it is an ordinary business
result reported with exit code 0. Only malformed structure, illegal fields or
references, exact arithmetic failures and I/O failures raise here.
"""

__all__ = ['TranslateError', 'TranslationValidationError']


class TranslateError(ValueError):
    """Base class for every translation workbench failure."""


class TranslationValidationError(TranslateError):
    """Input shape, identifier, reference, date or quote is invalid.

    A rejected request publishes nothing; the prior report (if any) stays
    byte-for-byte unchanged.
    """
