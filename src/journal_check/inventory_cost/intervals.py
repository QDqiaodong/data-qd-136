"""Half-open unit-interval algebra for traced physical goods movement.

Every receipt batch owns unit indices ``[0, quantity)``. Physical ownership is
always expressed as a list of disjoint, non-adjacent (merged on contact),
ascending half-open intervals ``[start, end)`` of *original receipt unit
indices*, so a unit keeps its source identity forever: an issued unit carries
its ``receipt`` id and its original index, and a returned unit is restored to
the same batch at the same index.

Nothing here ever iterates per unit: all operations work on interval endpoints,
so quantities of ``10**12`` are handled in time proportional to the number of
interval fragments, never to the quantity.
"""


def take_prefix(intervals, quantity):
    """Remove the lowest-indexed ``quantity`` units from ``intervals``.

    Issues always take the oldest available units (FIFO by original
    ``receipt.seq`` and, within one batch, by unit index). Returns
    ``(taken, remaining)`` where both are canonical ascending, merged interval
    lists. Raises ``ValueError`` when fewer than ``quantity`` units are
    available; in that case ``intervals`` is left untouched.
    """
    total = sum(end - start for start, end in intervals)
    if quantity > total:
        raise ValueError('insufficient available units')
    taken = []
    remaining = []
    needed = quantity
    for start, end in intervals:
        if needed == 0:
            remaining.append((start, end))
            continue
        length = end - start
        if length <= needed:
            taken.append((start, end))
            needed -= length
        else:
            cut = start + needed
            taken.append((start, cut))
            remaining.append((cut, end))
            needed = 0
    return taken, remaining


def restore(intervals, segments):
    """Return interval fragments to an availability list and merge contacts.

    ``segments`` may come back in reverse-issue order; the result is always a
    canonical ascending list with touching fragments (``[a, b)`` + ``[b, c)``)
    merged, so overlaps and duplicate ownership can never appear.
    """
    combined = list(intervals)
    combined.extend(segments)
    combined.sort()
    merged = []
    for start, end in combined:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
        # start <= merged end: a fully contained duplicate fragment disappears.
    return [(start, end) for start, end in merged]


def take_tail(intervals, quantity):
    """Remove the *highest-indexed* ``quantity`` units from ``intervals``.

    Returns undo the most recent issue allocation first: an issue takes units
    in ascending index order, so its not-yet-returned units are undone in
    reverse order. Returns ``(removed, remaining)`` as canonical interval
    lists; raises ``ValueError`` when not enough units are held.
    """
    total = sum(end - start for start, end in intervals)
    if quantity > total:
        raise ValueError('cannot return more units than the issue still holds')
    removed = []
    remaining = []
    needed = quantity
    for start, end in reversed(intervals):
        if needed == 0:
            remaining.append((start, end))
            continue
        length = end - start
        if length < needed:
            removed.append((start, end))
            needed -= length
        else:
            # length >= needed: the boundary fragment is split and the tail
            # removal is fully satisfied by it (the equal-length case still
            # leaves an empty remainder that is simply dropped).
            cut = end - needed
            removed.append((cut, end))
            if cut > start:
                remaining.append((start, cut))
            needed = 0
    removed.reverse()
    remaining.reverse()
    return removed, remaining


def take_tail_labeled(segments, quantity):
    """Tail-remove units while keeping each fragment's source label.

    ``segments`` are ``(label, start, end)`` tuples in allocation order; the
    label is the originating receipt id. This is required because one issue
    can span several receipts whose original unit indices coincide, so bare
    ``(start, end)`` tuples would be ambiguous. Returns ``(removed, kept)``
    lists of labeled tuples, each in ascending allocation order; raises
    ``ValueError`` when not enough units are held.
    """
    total = sum(end - start for _label, start, end in segments)
    if quantity > total:
        raise ValueError('cannot return more units than the issue still holds')
    removed = []
    kept = []
    needed = quantity
    for label, start, end in reversed(segments):
        if needed == 0:
            kept.append((label, start, end))
            continue
        length = end - start
        if length < needed:
            removed.append((label, start, end))
            needed -= length
        else:
            cut = end - needed
            removed.append((label, cut, end))
            if cut > start:
                kept.append((label, start, cut))
            needed = 0
    removed.reverse()
    kept.reverse()
    return removed, kept


def total_length(intervals):
    return sum(end - start for start, end in intervals)


def assert_partition(intervals, bound, label):
    """Invariant check: intervals partition a subset of ``[0, bound)``.

    Intervals must be ascending, half-open, non-overlapping and inside the
    batch bound. Raises ``AssertionError`` on any break.
    """
    previous_end = 0
    seen = False
    for start, end in intervals:
        if not (isinstance(start, int) and isinstance(end, int)):
            raise AssertionError(label + ': interval endpoints must be integers')
        if not 0 <= start < end <= bound:
            raise AssertionError(label + ': interval outside batch bound')
        if seen and start < previous_end:
            raise AssertionError(label + ': overlapping ownership intervals')
        previous_end = end
        seen = True


def assert_disjoint(left, right, bound, label):
    """Two ownership sets over one batch must be disjoint and inside bounds."""
    assert_partition(left, bound, label)
    assert_partition(right, bound, label)
    i = j = 0
    while i < len(left) and j < len(right):
        l_start, l_end = left[i]
        r_start, r_end = right[j]
        if l_end <= r_start:
            i += 1
        elif r_end <= l_start:
            j += 1
        else:
            raise AssertionError(label + ': owners overlap on the same unit')
    return True


__all__ = ['take_prefix', 'restore', 'take_tail', 'take_tail_labeled',
           'total_length', 'assert_partition', 'assert_disjoint']
