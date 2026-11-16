"""Per-day quote-graph resolution with exact rational evidence.

For one business day the active quotes form an undirected multigraph: every
quote ``base -> quote`` with exact weight ``numerator/denominator`` can be
walked in either direction (the reverse direction carries the reciprocal
weight). Connected components are decided with a spanning-tree traversal.

A component is trustworthy if and only if *every* closed walk has product
exactly ``1``. With a spanning tree this is equivalent to checking each
non-tree edge against the tree-derived rates, so the check is linear in the
number of active edges; paths are never enumerated and no float tolerance is
used. Every output ordering is canonical (sorted ids), hence independent of
the input arrangement.

Only the Python standard library (:mod:`fractions`) is used.
"""
from fractions import Fraction

READY = 'ready'
MISSING = 'missing'
INCONSISTENT = 'inconsistent'


def _step(quote_id, from_ccy, to_ccy, direction):
    """A directed, auditable quote step.

    ``direction`` is ``'forward'`` when the step walks the quote from its
    ``base`` to its ``quote`` currency and ``'reverse'`` otherwise.
    """
    return {'quote': quote_id, 'from': from_ccy, 'to': to_ccy,
            'direction': direction}


def _invert_step(step):
    return _step(step['quote'], step['to'], step['from'],
                 'reverse' if step['direction'] == 'forward' else 'forward')


def _fraction_pair(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def build_adjacency(active_quotes):
    """Turn validated quote objects into sorted directed adjacency lists.

    Each adjacency entry is ``(neighbor, step, weight)`` where ``weight`` is
    the exact multiplier for one minimal unit of the current node expressed in
    the neighbor's minimal units. A parallel lookup maps
    ``(from_currency, to_currency, quote_id)`` to that same weight.
    """
    adjacency = {}
    weights = {}
    for quote in active_quotes:
        base = quote['base']
        quoted = quote['quote']
        weight = Fraction(quote['numerator'], quote['denominator'])
        forward = _step(quote['id'], base, quoted, 'forward')
        reverse = _step(quote['id'], quoted, base, 'reverse')
        adjacency.setdefault(base, []).append((quoted, forward, weight))
        adjacency.setdefault(quoted, []).append(
            (base, reverse, Fraction(weight.denominator, weight.numerator)))
        weights[(base, quoted, quote['id'])] = weight
        weights[(quoted, base, quote['id'])] = Fraction(
            weight.denominator, weight.numerator)
    for edges in adjacency.values():
        edges.sort(key=lambda edge: (edge[0], edge[1]['quote']))
    return adjacency, weights


def _component_tree(root, adjacency):
    """Breadth-first spanning tree of one connected component.

    Returns ``(members, parent, parent_step, tree_rate)`` with ``tree_rate``
    the exact multiplier ``root -> node``. Iteration is over sorted edges so
    the tree and every derived path are independent of quote ordering.
    """
    parent = {root: None}
    parent_step = {}
    tree_rate = {root: Fraction(1)}
    members = []
    queue = [root]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        members.append(node)
        for neighbor, step, weight in adjacency.get(node, []):
            if neighbor in parent:
                continue
            parent[neighbor] = node
            parent_step[neighbor] = step
            tree_rate[neighbor] = tree_rate[node] * weight
            queue.append(neighbor)
    return members, parent, parent_step, tree_rate


def _ancestors(node, parent):
    chain = []
    current = node
    while current is not None:
        chain.append(current)
        current = parent[current]
    return chain


def _tree_path_steps(source, sink, parent, parent_step):
    """Directed steps of the unique tree path ``source -> sink``."""
    source_chain = _ancestors(source, parent)
    sink_chain = set(_ancestors(sink, parent))
    lca = next(node for node in source_chain if node in sink_chain)
    steps = []
    current = source
    while current != lca:  # climb source up to the lowest common ancestor
        steps.append(_invert_step(parent_step[current]))
        current = parent[current]
    downward = []
    current = sink
    while current != lca:  # collect the LCA -> sink branch, then reverse it
        downward.append(parent_step[current])
        current = parent[current]
    steps.extend(reversed(downward))
    return steps


def _steps_product(steps, weights):
    product = Fraction(1)
    for step in steps:
        product *= weights[(step['from'], step['to'], step['quote'])]
    return product


def _assert_closed_walk(steps):
    if not steps:
        raise AssertionError('witness walk must not be empty')
    for earlier, later in zip(steps, steps[1:]):
        if earlier['to'] != later['from']:
            raise AssertionError('witness walk is not continuous')
    if steps[0]['from'] != steps[-1]['to']:
        raise AssertionError('witness walk does not close')


def resolve_day(active_quotes, target, needed_currencies):
    """Resolve every needed currency for one day.

    ``active_quotes`` are the quote objects valid on that day; ``target`` is
    the display currency id and ``needed_currencies`` the currencies that must
    receive a status record (the target is always included). Returns a dict
    mapping currency id to a JSON-serialisable status record.

    The target currency is always ``ready`` with rate ``1/1`` and no path,
    even when quotes mentioning it contradict each other; every other
    currency of a contradicted component is ``inconsistent`` (this takes
    precedence over reachability), a consistent component with no path to the
    target is ``missing`` and the rest is ``ready`` with an exact reduced rate
    and a real, checkable quote-id path.
    """
    adjacency, weights = build_adjacency(active_quotes)
    nodes = set(adjacency) | {target}
    visited = set()
    records = {}

    for root in sorted(nodes):
        if root in visited:
            continue
        members, parent, parent_step, tree_rate = _component_tree(root, adjacency)
        visited.update(members)
        member_set = set(members)

        # A component is consistent iff every edge agrees with the tree rates.
        # The first contradicting edge (canonical order) becomes the witness.
        contradiction = None
        for node in sorted(member_set):
            for neighbor, step, weight in adjacency.get(node, []):
                if parent.get(neighbor) == node:
                    continue  # tree edge, already encoded in tree_rate
                if tree_rate[node] * weight != tree_rate[neighbor]:
                    candidate = (node, neighbor, step, weight)
                    if contradiction is None or \
                            (node, step['quote']) < \
                            (contradiction[0], contradiction[2]['quote']):
                        contradiction = candidate
            if contradiction is not None:
                break

        if contradiction is not None:
            node_u, node_v, edge_step, edge_weight = contradiction
            tree_steps = _tree_path_steps(node_u, node_v, parent, parent_step)
            # Close the walk by returning along the contradicting edge itself.
            witness_steps = tree_steps + [_invert_step(edge_step)]
            _assert_closed_walk(witness_steps)
            product = _steps_product(witness_steps, weights)
            if product == 1:  # pragma: no cover - would mean no contradiction
                raise AssertionError('contradiction witness product is 1')
            witness = {'closed': True, 'steps': witness_steps,
                       'product': _fraction_pair(product)}
            for currency in sorted(member_set):
                if currency == target:
                    continue
                records[currency] = {'currency': currency,
                                     'state': INCONSISTENT,
                                     'witness': witness}
            continue

        if target in member_set:
            for currency in sorted(member_set):
                if currency == target:
                    continue
                path = _tree_path_steps(currency, target, parent, parent_step)
                rate = Fraction(tree_rate[target].numerator,
                                tree_rate[target].denominator) / tree_rate[currency]
                if _steps_product(path, weights) != rate:  # pragma: no cover
                    raise AssertionError('rate path product does not match rate')
                records[currency] = {'currency': currency, 'state': READY,
                                     'rate': _fraction_pair(rate), 'path': path}
        else:
            reachable = sorted(member_set)
            for currency in sorted(member_set):
                records[currency] = {'currency': currency, 'state': MISSING,
                                     'reachable_currencies': reachable}

    records[target] = {'currency': target, 'state': READY,
                       'rate': {'numerator': 1, 'denominator': 1}, 'path': []}
    # A needed currency with no active edge at all is an isolated component of
    # one node: consistent but with no path to the target, hence missing.
    for currency in sorted(needed_currencies | {target}):
        if currency not in records:
            records[currency] = {'currency': currency, 'state': MISSING,
                                 'reachable_currencies': [currency]}
    return {currency: records[currency]
            for currency in sorted(needed_currencies | {target})}


__all__ = ['READY', 'MISSING', 'INCONSISTENT', 'build_adjacency',
           'resolve_day']
