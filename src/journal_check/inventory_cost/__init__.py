"""Inventory cost attribution and catch-up invoice workbench.

Converts an event snapshot of goods receipts, departmental issues, returns and
purchase total-cost revisions (reprice = 追补发票) into traceable inventory
valuations, department expenses and double-entry postings, with a per-currency
trial balance that reuses the existing ledger semantics.

Public Python interface (all callable directly from tests)::

    from journal_check.inventory_cost import (
        inventory_cost, run_file, publish,
    )
    from journal_check.inventory_cost.errors import (
        InventoryCostError, InventoryValidationError,
    )
    from journal_check.inventory_cost.engine import (
        prefix_cost, validate_request,
    )

``inventory_cost(data, source_sha256=None)`` works on a parsed strict JSON
object; ``run_file(source)`` performs the strict 8 MB read itself. Only the
Python standard library is used. The workbench never touches the open-items
database or the close-chain store and never reads or writes the v1 storage.

There is no open-item matching, no period carry-forward and no currency
translation here.
"""
from ..common import read, save
from .engine import inventory_cost, prefix_cost, validate_request
from .errors import InventoryCostError, InventoryValidationError

__all__ = ['inventory_cost', 'run_file', 'publish', 'validate_request',
           'prefix_cost', 'InventoryCostError', 'InventoryValidationError']


def run_file(source):
    """Strictly read an inventory-cost request file and build the report.

    Returns the full report, including the SHA256 of the exact source bytes.
    """
    data, digest = read(source)
    return inventory_cost(data, digest)


def publish(report, output):
    """Atomically publish a report through the shared safe publication path."""
    save(output, report)
