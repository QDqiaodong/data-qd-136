"""Deterministic open-item settlement planning.

A plan builds an implicit bipartite graph between debit-side and credit-side
open items. An edge exists only when both items share the same currency and
settlement key, are on opposite sides and the directed party pair is allowed
by a supplied rule. Only the connected components containing a seed item are
planned; isolated seeds are legal and simply receive no allocation.

The objective on each component is, within every endpoint's remaining
capacity:

1. maximise the total integer settlement amount; then
2. lexicographically maximise the full allocation vector (including zeroes)
   over edges ordered by
   ``(rule.priority, debit_item.priority, credit_item.priority,
   debit_item.id, credit_item.id)``.

Nothing here uses floating point, exchange rates, fuzzy matching or unit
expansion: amounts are exact integers up to 10**12.
"""
from collections import deque

from .errors import ExhaustedError, LimitError, NotFoundError, ValidationError

MAX_RULES = 500
MAX_COMPONENT_ITEMS = 100
MAX_COMPONENT_EDGES = 2500
PRIORITY_MAX = 10 ** 6


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_rules(rules):
    """Validate and normalise the allowed party-pair rules."""
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValidationError('rules must be an array of at most %d objects' % MAX_RULES)
    normalised = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'debit_party', 'credit_party', 'priority'}:
            raise ValidationError('each rule must have exactly debit_party, credit_party, priority')
        debit_party = _ident(rule['debit_party'], 'rule.debit_party')
        credit_party = _ident(rule['credit_party'], 'rule.credit_party')
        priority = rule['priority']
        if type(priority) is not int or not 0 <= priority <= PRIORITY_MAX:
            raise ValidationError('rule priority must be an integer in 0..10**6')
        if debit_party == credit_party:
            raise ValidationError('rule parties must differ')
        pair = (debit_party, credit_party)
        if pair in seen:
            raise ValidationError('duplicate directed party pair in rules')
        seen.add(pair)
        normalised.append({'debit_party': debit_party,
                           'credit_party': credit_party,
                           'priority': priority})
    # Rule lookup does not depend on the caller's array order.
    normalised.sort(key=lambda r: (r['debit_party'], r['credit_party']))
    return normalised


def validate_seeds(seeds):
    if not isinstance(seeds, list) or not seeds:
        raise ValidationError('seeds must be a non-empty array of open item ids')
    result = []
    seen = set()
    for seed in seeds:
        seed = _ident(seed, 'seed')
        if seed in seen:
            raise ValidationError('duplicate seed item id: ' + seed)
        seen.add(seed)
        result.append(seed)
    return result


def _ident(value, label):
    if not isinstance(value, str) or not value or value != value.strip() \
            or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValidationError('invalid identifier for ' + label)
    return value


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph(items, rules):
    """Return ``(edges, adjacency)`` for the currently-open items.

    ``items`` is a list of dicts with keys id, side, currency, settlement_key,
    party, priority, remaining. Edges are returned in the canonical order
    required by the contract and carry a stable ``edge_seq`` (their index in
    that global order).
    """
    rule_map = {(r['debit_party'], r['credit_party']): r['priority'] for r in rules}
    debits = [it for it in items if it['side'] == 'debit']
    credits = [it for it in items if it['side'] == 'credit']
    edges = []
    for debit in debits:
        for credit in credits:
            if debit['currency'] != credit['currency']:
                continue
            if debit['settlement_key'] != credit['settlement_key']:
                continue
            priority = rule_map.get((debit['party'], credit['party']))
            if priority is None:
                continue
            edges.append({
                'debit_item_id': debit['id'],
                'credit_item_id': credit['id'],
                'rule_priority': priority,
                'debit_priority': debit['priority'],
                'credit_priority': credit['priority'],
            })
    edges.sort(key=lambda e: (e['rule_priority'], e['debit_priority'],
                              e['credit_priority'], e['debit_item_id'],
                              e['credit_item_id']))
    for seq, edge in enumerate(edges):
        edge['edge_seq'] = seq
    adjacency = {it['id']: set() for it in items}
    for edge in edges:
        adjacency[edge['debit_item_id']].add(edge['credit_item_id'])
        adjacency[edge['credit_item_id']].add(edge['debit_item_id'])
    return edges, adjacency


def seed_components(seeds, adjacency):
    """Collect the connected components reachable from the seeds."""
    visited = set()
    components = []
    for seed in seeds:
        if seed in visited:
            continue
        queue = deque([seed])
        component = set()
        while queue:
            current = queue.popleft()
            if current in component:
                continue
            component.add(current)
            for neighbour in adjacency[current]:
                if neighbour not in component:
                    queue.append(neighbour)
        visited |= component
        components.append(component)
    return components


def check_component_limits(component, edges):
    if len(component) > MAX_COMPONENT_ITEMS or len(edges) > MAX_COMPONENT_EDGES:
        raise LimitError(
            'component exceeds limits: %d items (max %d), %d candidate edges (max %d)'
            % (len(component), MAX_COMPONENT_ITEMS, len(edges), MAX_COMPONENT_EDGES))


# ---------------------------------------------------------------------------
# Max flow (Dinic)
# ---------------------------------------------------------------------------

class _Dinic:
    def __init__(self, node_count):
        self.n = node_count
        self.graph = [[] for _ in range(node_count)]

    def add_edge(self, u, v, capacity):
        forward = [v, len(self.graph[v]), capacity]
        backward = [u, len(self.graph[u]), 0]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def _bfs(self, source, sink):
        self.level = [-1] * self.n
        self.level[source] = 0
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                v, _rev, capacity = edge
                if capacity > 0 and self.level[v] < 0:
                    self.level[v] = self.level[u] + 1
                    queue.append(v)
        return self.level[sink] >= 0

    def _dfs(self, u, sink, pushed):
        if u == sink:
            return pushed
        while self.ptr[u] < len(self.graph[u]):
            edge = self.graph[u][self.ptr[u]]
            v, rev, capacity = edge
            if capacity > 0 and self.level[v] == self.level[u] + 1:
                flow = self._dfs(v, sink, min(pushed, capacity))
                if flow:
                    edge[2] -= flow
                    self.graph[v][rev][2] += flow
                    return flow
            self.ptr[u] += 1
        return 0

    def max_flow(self, source, sink):
        total = 0
        while self._bfs(source, sink):
            self.ptr = [0] * self.n
            while True:
                pushed = self._dfs(source, sink, 10 ** 30)
                if not pushed:
                    break
                total += pushed
        return total


def _reachable_total(component_items, component_edges, forced, skipped):
    """Best total flow with the canonical prefix already locked.

    ``forced`` maps an ``edge_seq`` to a positive amount that edge must carry;
    ``skipped`` is the set of edges locked to zero. Forced volume is reserved
    on both endpoint capacities (it consumes debit supply and credit demand)
    and forced edges are removed from the free network; skipped edges are
    removed without reserving capacity. Every remaining inter-item edge is
    bounded by the endpoint room left after the forced reservations, so the
    free flow can never reuse locked volume. Returns
    ``sum(forced) + free residual max flow``. A fresh network is built for
    every call, so feasibility probes share no residual state.
    """
    capacities = {it['id']: it['remaining'] for it in component_items}
    debit_used = {}
    credit_used = {}
    forced_total = 0
    for edge in component_edges:
        amount = forced.get(edge['edge_seq'])
        if amount is None:
            continue
        debit_used[edge['debit_item_id']] = debit_used.get(edge['debit_item_id'], 0) + amount
        credit_used[edge['credit_item_id']] = credit_used.get(edge['credit_item_id'], 0) + amount
        forced_total += amount

    debit_ids = [it['id'] for it in component_items if it['side'] == 'debit']
    credit_ids = [it['id'] for it in component_items if it['side'] == 'credit']
    index = {item_id: i for i, item_id in enumerate(debit_ids + credit_ids)}
    source = len(index)
    sink = source + 1
    dinic = _Dinic(sink + 1)
    for item_id in debit_ids:
        dinic.add_edge(source, index[item_id],
                       capacities[item_id] - debit_used.get(item_id, 0))
    for item_id in credit_ids:
        dinic.add_edge(index[item_id], sink,
                       capacities[item_id] - credit_used.get(item_id, 0))
    for edge in component_edges:
        if edge['edge_seq'] in forced or edge['edge_seq'] in skipped:
            continue
        debit_room = capacities[edge['debit_item_id']] - debit_used.get(edge['debit_item_id'], 0)
        credit_room = capacities[edge['credit_item_id']] - credit_used.get(edge['credit_item_id'], 0)
        dinic.add_edge(index[edge['debit_item_id']], index[edge['credit_item_id']],
                       min(debit_room, credit_room))
    return forced_total + dinic.max_flow(source, sink)


def optimise_component(component_items, component_edges):
    """Return ``(allocations, optimum_total)`` with allocations in edge order.

    Lexicographic max flow by locking the canonical prefix one edge at a time.
    The global optimum is computed first. For each edge in canonical order we
    choose the largest amount that still lets the locked prefix reach that
    optimum; a zero decision removes the edge without reserving capacity. The
    reachable-total feasibility predicate is provably non-monotone in the
    candidate amount (feasible amounts can be isolated high values), so the
    exact largest feasible value is found by a downward scan — binary search
    and capacity scaling would both be incorrect here.

    The scan range is the endpoint room remaining after the locked prefix, and
    a probe that hits at the capacity usually terminates immediately. The hard
    component limits (at most 100 items and 2500 edges, amounts up to 10**12)
    bound the work; no unit expansion is performed. Every edge — including
    zero-valued ones — is decided in canonical order, giving the
    lexicographically greatest optimum over the full edge vector.
    """
    if not component_edges:
        return [], 0
    capacities = {it['id']: it['remaining'] for it in component_items}
    optimum_total = _reachable_total(component_items, component_edges, {}, set())
    if optimum_total == 0:
        return [], 0

    forced = {}
    skipped = set()
    debit_used = {}
    credit_used = {}
    vector = [0] * len(component_edges)
    for k, edge in enumerate(component_edges):
        # Only capacity still free after the locked prefix is admissible, so
        # an edge sharing an endpoint with an earlier forced edge can never be
        # assigned volume that endpoint no longer holds.
        maximum = min(capacities[edge['debit_item_id']] - debit_used.get(edge['debit_item_id'], 0),
                      capacities[edge['credit_item_id']] - credit_used.get(edge['credit_item_id'], 0))
        chosen = 0
        for amount in range(maximum, -1, -1):
            trial_forced = dict(forced)
            trial_skipped = set(skipped)
            if amount:
                trial_forced[edge['edge_seq']] = amount
            else:
                trial_skipped.add(edge['edge_seq'])
            if _reachable_total(component_items, component_edges,
                                trial_forced, trial_skipped) == optimum_total:
                chosen = amount
                break
        vector[k] = chosen
        if chosen:
            forced[edge['edge_seq']] = chosen
            debit_used[edge['debit_item_id']] = debit_used.get(edge['debit_item_id'], 0) + chosen
            credit_used[edge['credit_item_id']] = credit_used.get(edge['credit_item_id'], 0) + chosen
        else:
            skipped.add(edge['edge_seq'])
    if sum(vector) != optimum_total:
        raise LimitError('internal allocation mismatch')  # pragma: no cover
    allocations = [
        {
            'edge_seq': component_edges[k]['edge_seq'],
            'debit_item_id': component_edges[k]['debit_item_id'],
            'credit_item_id': component_edges[k]['credit_item_id'],
            'amount': amount,
        }
        for k, amount in enumerate(vector) if amount > 0
    ]
    return allocations, optimum_total


def plan_request(items, seeds, rules):
    """Pure planning step used by both the Python API and the CLI.

    ``items`` must be every positive-remaining open item in the database.
    Returns the serialisable plan body; persistence (id assignment and plan_id
    hashing) lives in the workbench layer.
    """
    rules = validate_rules(rules)
    seeds = validate_seeds(seeds)
    by_id = {it['id']: it for it in items}
    for seed in seeds:
        if seed not in by_id:
            raise NotFoundError('seed open item not found: ' + seed)
        if by_id[seed]['remaining'] <= 0:
            raise ExhaustedError('seed open item has no remaining quantity: ' + seed)

    edges, adjacency = build_graph(items, rules)
    components = seed_components(seeds, adjacency)

    planned_ids = set()
    allocations = []
    total_amount = 0
    planned_remaining = {}
    for component in components:
        component_items = [by_id[item_id] for item_id in sorted(component)]
        component_edges = [e for e in edges if e['debit_item_id'] in component]
        check_component_limits(component, component_edges)
        component_allocations, component_total = optimise_component(component_items, component_edges)
        for allocation in component_allocations:
            amount = allocation['amount']
            planned_remaining[allocation['debit_item_id']] = \
                planned_remaining.get(allocation['debit_item_id'],
                                      by_id[allocation['debit_item_id']]['remaining']) - amount
            planned_remaining[allocation['credit_item_id']] = \
                planned_remaining.get(allocation['credit_item_id'],
                                      by_id[allocation['credit_item_id']]['remaining']) - amount
        allocations.extend(component_allocations)
        total_amount += component_total
        planned_ids |= component

    # Global canonical edge order is the edge_seq order.
    allocations.sort(key=lambda a: a['edge_seq'])
    item_after = [
        {
            'item_id': item_id,
            'remaining_before': by_id[item_id]['remaining'],
            'remaining_after': planned_remaining.get(item_id, by_id[item_id]['remaining']),
        }
        for item_id in sorted(planned_ids)
    ]
    return {
        'seeds': sorted(seeds),
        'rules': rules,
        'component_item_ids': [sorted(component) for component in components],
        'edges': edges,
        'items_snapshot': [
            {
                'id': it['id'],
                'side': it['side'],
                'currency': it['currency'],
                'account_id': it['account_id'],
                'party': it['party'],
                'settlement_key': it['settlement_key'],
                'priority': it['priority'],
                'remaining': it['remaining'],
            }
            for it in (by_id[item_id] for item_id in sorted(planned_ids))
        ],
        'allocations': allocations,
        'total_amount': total_amount,
        'item_remaining_after': item_after,
    }
