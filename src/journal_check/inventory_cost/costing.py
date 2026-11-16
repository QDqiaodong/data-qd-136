"""Exact integer batch-prefix costing.

A receipt batch with total cost ``C`` spread over ``Q`` units defines the cost
of any original unit interval ``[a, b)`` through the batch prefix function::

    cost([a, b)) = floor(C * b / Q) - floor(C * a / Q)

The cost of several intervals is the sum of their prefix differences. This is
exact integer arithmetic: no float, no average unit price followed by
re-rounding, no per-unit expansion. Complexity is proportional to the number of
interval fragments, never to ``Q`` or ``C``, so ``Q = 10**12`` and
``C = 10**18`` are handled directly.

Worked contract: ``Q = 3, C = 5`` gives unit costs
``floor(5/3)=1``, ``floor(10/3)-floor(5/3)=3-1=2`` and
``5-floor(10/3)=5-3=2``, i.e. ``1, 2, 2`` rather than three averaged equal
costs. After repricing to ``C = 7`` the same units cost ``2, 2, 3``.
"""


def prefix_cost(total_cost, quantity, start, end):
    """Exact cost of half-open interval ``[start, end)`` of one batch."""
    if not 0 <= start <= end <= quantity:
        raise ValueError('cost interval outside batch bound')
    return (total_cost * end) // quantity - (total_cost * start) // quantity


def segments_cost(total_cost, quantity, segments):
    """Exact cost of a list of disjoint half-open intervals of one batch."""
    return sum(prefix_cost(total_cost, quantity, start, end)
               for start, end in segments)


__all__ = ['prefix_cost', 'segments_cost']
