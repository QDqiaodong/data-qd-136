"""Exception convention for the inventory-cost workbench.

Every inventory-cost error is a :class:`ValueError` subclass so it stays in
the same family as the strict v1 JSON/parser errors and the other workbenches.
The CLI maps the whole family to exit code 2 and leaves any existing output
file untouched.
"""

__all__ = ['InventoryCostError', 'InventoryValidationError']


class InventoryCostError(ValueError):
    """Base class for every inventory-cost workbench failure."""


class InventoryValidationError(InventoryCostError):
    """Input shape, identifier, reference, quantity or cost is invalid.

    Any single illegal event (no stock, cross-pool issue, future reference,
    over-return, unknown field, boolean value, ...) rejects the whole request;
    a rejected request publishes nothing and the prior report stays
    byte-for-byte unchanged.
    """
