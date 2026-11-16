"""Quote-evidence driven multi-currency translation workbench.

Public Python interface (all callable directly from tests)::

    from journal_check.translate import translate, run_file
    from journal_check.translate.errors import (
        TranslateError, TranslationValidationError,
    )
    from journal_check.translate.rates import resolve_day
    from journal_check.translate.engine import round_half_even

``translate(data, source_sha256=None)`` works on a parsed strict JSON object;
``run_file(source)`` performs the strict 8 MB read itself. Only the Python
standard library is used. The workbench never touches the open-items database
or the close-chain store: it neither clears open items nor carries periods.
"""
from ..common import read, save
from .engine import translate
from .errors import TranslateError, TranslationValidationError

__all__ = ['translate', 'run_file', 'publish', 'TranslateError',
           'TranslationValidationError']


def run_file(source):
    """Strictly read a translation request file and translate it.

    Returns the full report, including the SHA256 of the exact source bytes.
    """
    data, digest = read(source)
    return translate(data, digest)


def publish(report, output):
    """Atomically publish a report through the shared safe publication path."""
    save(output, report)
