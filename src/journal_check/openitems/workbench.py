"""Workbench API: ingest, status, plan and commit over a SQLite ledger.

The functions here are the test-callable Python interface. They accept plain
Python objects and return plain JSON-serialisable reports; every failure uses
the exception convention defined in :mod:`journal_check.openitems.errors`.
"""
import datetime
import hashlib
import json

from ..common import ident, number, rows
from ..journal import parse_journal
from . import planner
from .errors import (
    ConflictError,
    ExhaustedError,
    LimitError,
    NotFoundError,
    OpenItemError,
    ValidationError,
)
from .storage import assert_conservation, rollback_silent

ITEM_LIMIT = 20000
PRIORITY_MAX = 10 ** 6


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _prepare_ingest(data):
    """Validate a batch fully in memory; nothing is written on failure."""
    if not isinstance(data, dict) or set(data) != {'batch_id', 'journal', 'items'}:
        raise ValidationError('expected object fields: batch_id journal items')
    batch_id = ident(data['batch_id'])
    accounts, entries = parse_journal(data['journal'])

    raw_items = rows(data['items'], ITEM_LIMIT)
    if not raw_items:
        raise ValidationError('at least one open item is required')

    line_index = {}
    entry_currency = {}
    entry_order = []
    for entry_id, currency, lines in entries:
        entry_currency[entry_id] = currency
        entry_order.append(entry_id)
        for line_idx, (account, side, amount) in enumerate(lines):
            line_index[(entry_id, line_idx)] = (account, side, amount)

    prepared = []
    seen_item_ids = set()
    seen_lines = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {'id', 'entry', 'line', 'party',
                                                      'settlement_key', 'priority'}:
            raise ValidationError(
                'item must have exactly id entry line party settlement_key priority')
        item_id = ident(raw['id'])
        entry_id = ident(raw['entry'])
        line = raw['line']
        party = ident(raw['party'])
        settlement_key = ident(raw['settlement_key'])
        priority = number(raw['priority'], 0, PRIORITY_MAX)
        if type(line) is not int or line < 0:
            raise ValidationError('line must be a non-negative integer index')
        if item_id in seen_item_ids:
            raise ValidationError('duplicate open item id within batch: ' + item_id)
        seen_item_ids.add(item_id)
        if entry_id not in entry_currency:
            raise ValidationError('item references unknown entry: ' + entry_id)
        key = (entry_id, line)
        if key not in line_index:
            raise ValidationError(
                'item %s references line %d of entry %s, which does not exist'
                % (item_id, line, entry_id))
        if key in seen_lines:
            raise ValidationError('the same journal line can only be opened once: '
                                  + entry_id + '#' + str(line))
        seen_lines.add(key)
        account, side, amount = line_index[key]
        prepared.append({
            'id': item_id,
            'batch_id': batch_id,
            'entry_id': entry_id,
            'line_idx': line,
            'party': party,
            'settlement_key': settlement_key,
            'priority': priority,
            'side': side,
            'currency': entry_currency[entry_id],
            'account_id': account,
            'initial_amount': amount,
            'remaining': amount,
        })
    return batch_id, accounts, entries, entry_order, prepared


def _check_against_database(conn, batch_id, source_sha256, accounts, entry_order, prepared):
    existing_batch = conn.execute(
        'SELECT source_sha256 FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
    if existing_batch is not None:
        if existing_batch['source_sha256'] != source_sha256:
            raise ConflictError('batch_id already exists with different bytes: ' + batch_id)
        return True

    known_accounts = {row['account_id']: row['normal']
                      for row in conn.execute('SELECT account_id, normal FROM accounts')}
    for account_id, normal in accounts.items():
        if account_id in known_accounts and known_accounts[account_id] != normal:
            raise ConflictError('account %s changes normal side across batches' % account_id)

    known_entries = {row['entry_id'] for row in conn.execute('SELECT entry_id FROM entries')}
    for entry_id in entry_order:
        if entry_id in known_entries:
            raise ConflictError('entry id already exists in another batch: ' + entry_id)

    placeholders = ','.join('?' for _ in prepared)
    existing_item = conn.execute(
        'SELECT item_id FROM open_items WHERE item_id IN (%s)' % placeholders,
        [item['id'] for item in prepared]).fetchone()
    if existing_item is not None:
        raise ConflictError('open item id must be unique across the ledger: '
                            + existing_item['item_id'])
    return False


def ingest(conn, data, source_sha256):
    """Validate and persist one batch atomically.

    ``source_sha256`` is the SHA256 of the exact ingested file bytes; it is
    stored with the batch so an identical replay is idempotent. On any invalid
    reference or conflict the transaction is rolled back and no partial batch,
    account or open item remains.
    """
    batch_id, accounts, entries, entry_order, prepared = _prepare_ingest(data)
    # Pre-check outside the write transaction so failures leave no transaction
    # open at all; the same checks are repeated under the write lock.
    replayed = _check_against_database(conn, batch_id, source_sha256, accounts,
                                       entry_order, prepared)
    if replayed:
        return _ingest_receipt(batch_id, source_sha256, prepared, True)

    conn.execute('BEGIN IMMEDIATE')
    try:
        replayed = _check_against_database(conn, batch_id, source_sha256, accounts,
                                           entry_order, prepared)
        if replayed:
            conn.commit()
            return _ingest_receipt(batch_id, source_sha256, prepared, True)

        conn.execute('INSERT INTO batches(batch_id, source_sha256) VALUES (?, ?)',
                     (batch_id, source_sha256))
        for account_id, normal in accounts.items():
            conn.execute('INSERT OR IGNORE INTO accounts(account_id, normal) VALUES (?, ?)',
                         (account_id, normal))
        for entry_id, currency, lines in entries:
            conn.execute('INSERT INTO entries(entry_id, batch_id, currency) VALUES (?, ?, ?)',
                         (entry_id, batch_id, currency))
            for line_idx, (account, side, amount) in enumerate(lines):
                conn.execute(
                    'INSERT INTO entry_lines(entry_id, line_idx, account_id, side, amount) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (entry_id, line_idx, account, side, amount))
        for item in prepared:
            conn.execute(
                'INSERT INTO open_items(item_id, batch_id, entry_id, line_idx, party, '
                'settlement_key, priority, side, currency, account_id, initial_amount, remaining) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (item['id'], item['batch_id'], item['entry_id'], item['line_idx'], item['party'],
                 item['settlement_key'], item['priority'], item['side'], item['currency'],
                 item['account_id'], item['initial_amount'], item['remaining']))
        assert_conservation(conn)
        conn.commit()
    except Exception:
        rollback_silent(conn)
        raise
    return _ingest_receipt(batch_id, source_sha256, prepared, False)


def _ingest_receipt(batch_id, source_sha256, prepared, replayed):
    return {
        'batch_id': batch_id,
        'source_sha256': source_sha256,
        'stored_sha256': source_sha256,
        'replayed': replayed,
        'item_ids': sorted(item['id'] for item in prepared),
        'item_count': len(prepared),
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _load_open_items(conn, only_positive=False):
    sql = ('SELECT item_id, batch_id, entry_id, line_idx, party, settlement_key, priority, '
           'side, currency, account_id, initial_amount, remaining FROM open_items')
    if only_positive:
        sql += ' WHERE remaining > 0'
    sql += ' ORDER BY item_id'
    return [dict(row) for row in conn.execute(sql).fetchall()]


def status(conn):
    """Report every open item and gross unsettled debit/credit amounts.

    Amounts are summed per currency and account without netting opposite open
    items against each other, so a debit open item and a credit open item on
    the same account remain visible as both a positive open debit and a
    positive open credit.
    """
    items = _load_open_items(conn)
    totals = {}
    for item in items:
        key = (item['currency'], item['account_id'])
        bucket = totals.setdefault(key, {'currency': item['currency'],
                                         'account': item['account_id'],
                                         'open_debit': 0, 'open_credit': 0})
        if item['side'] == 'debit':
            bucket['open_debit'] += item['remaining']
        else:
            bucket['open_credit'] += item['remaining']
    return {
        'open_item_count': len(items),
        'open_items': [
            {
                'id': item['item_id'],
                'batch_id': item['batch_id'],
                'entry': item['entry_id'],
                'line': item['line_idx'],
                'party': item['party'],
                'settlement_key': item['settlement_key'],
                'priority': item['priority'],
                'side': item['side'],
                'currency': item['currency'],
                'account': item['account_id'],
                'initial_amount': item['initial_amount'],
                'remaining': item['remaining'],
            }
            for item in items
        ],
        'unsettled': [totals[key] for key in sorted(totals)],
        'plans': {
            'planned': conn.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE state = 'planned'").fetchone()['n'],
            'committed': conn.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE state = 'committed'").fetchone()['n'],
        },
        'batches': conn.execute('SELECT COUNT(*) AS n FROM batches').fetchone()['n'],
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _canonical_plan_id(payload):
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def _planner_items(conn):
    return [{
        'id': item['item_id'],
        'side': item['side'],
        'currency': item['currency'],
        'account_id': item['account_id'],
        'party': item['party'],
        'settlement_key': item['settlement_key'],
        'priority': item['priority'],
        'remaining': item['remaining'],
    } for item in _load_open_items(conn, only_positive=True)]


def plan(conn, seeds, rules):
    """Plan settlements for the components reachable from ``seeds``."""
    # Validate the request shape before touching ledger contents.
    rules = planner.validate_rules(rules)
    seeds = planner.validate_seeds(seeds)
    # Load every item (including exhausted ones) so an existing but depleted
    # seed is reported as exhausted rather than missing.
    all_items = _load_open_items(conn)
    by_id = {item['item_id']: item for item in all_items}
    for seed in seeds:
        if seed not in by_id:
            raise NotFoundError('seed open item not found: ' + seed)
        if by_id[seed]['remaining'] <= 0:
            raise ExhaustedError('seed open item has no remaining quantity: ' + seed)
    planner_items = [{
        'id': item['item_id'],
        'side': item['side'],
        'currency': item['currency'],
        'account_id': item['account_id'],
        'party': item['party'],
        'settlement_key': item['settlement_key'],
        'priority': item['priority'],
        'remaining': item['remaining'],
    } for item in all_items if item['remaining'] > 0]
    body = planner.plan_request(planner_items, seeds, rules)
    plan_id = _canonical_plan_id({
        'seeds': body['seeds'],
        'rules': body['rules'],
        'items_snapshot': body['items_snapshot'],
        'edges': body['edges'],
        'allocations': body['allocations'],
        'total_amount': body['total_amount'],
        'component_item_ids': body['component_item_ids'],
    })

    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute('SELECT 1 FROM plans WHERE plan_id = ?', (plan_id,)).fetchone()
        if existing is None:
            conn.execute(
                'INSERT INTO plans(plan_id, state, created_at, seed_json, rules_json, '
                'items_json, edges_json, components_json, allocation_json, total_amount) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (plan_id, 'planned', _now(),
                 json.dumps(body['seeds'], sort_keys=True),
                 json.dumps(body['rules'], sort_keys=True),
                 json.dumps(body['items_snapshot'], sort_keys=True),
                 json.dumps(body['edges'], sort_keys=True),
                 json.dumps(body['component_item_ids'], sort_keys=True),
                 json.dumps(body['allocations'], sort_keys=True),
                 body['total_amount']))
        conn.commit()
    except Exception:
        rollback_silent(conn)
        raise
    return get_plan(conn, plan_id)


def get_plan(conn, plan_id):
    """Return the durable, re-readable report stored for a plan_id."""
    row = conn.execute('SELECT * FROM plans WHERE plan_id = ?', (plan_id,)).fetchone()
    if row is None:
        raise NotFoundError('plan not found: ' + plan_id)
    snapshot = json.loads(row['items_json'])
    allocations = json.loads(row['allocation_json'])
    remaining = {item['id']: item['remaining'] for item in snapshot}
    for allocation in allocations:
        remaining[allocation['debit_item_id']] -= allocation['amount']
        remaining[allocation['credit_item_id']] -= allocation['amount']
    return {
        'plan_id': plan_id,
        'state': row['state'],
        'seeds': json.loads(row['seed_json']),
        'rules': json.loads(row['rules_json']),
        'components': json.loads(row['components_json']),
        'edges': [
            {key: edge[key] for key in ('edge_seq', 'debit_item_id', 'credit_item_id',
                                        'rule_priority', 'debit_priority', 'credit_priority')}
            for edge in json.loads(row['edges_json'])
        ],
        'allocations': [
            {
                'debit_item_id': allocation['debit_item_id'],
                'credit_item_id': allocation['credit_item_id'],
                'amount': allocation['amount'],
            }
            for allocation in allocations
        ],
        'total_amount': row['total_amount'],
        'item_remaining_after': [
            {
                'item_id': item_id,
                'remaining_before': next(
                    item['remaining'] for item in snapshot if item['id'] == item_id),
                'remaining_after': amount,
            }
            for item_id, amount in sorted(remaining.items())
        ],
    }


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Commit with full revalidation
# ---------------------------------------------------------------------------

def _revalidate(conn, plan_row):
    """Rebuild the planned component from current rows and compare exactly."""
    snapshot = json.loads(plan_row['items_json'])
    edges = json.loads(plan_row['edges_json'])
    rules = json.loads(plan_row['rules_json'])
    seeds = json.loads(plan_row['seed_json'])
    expected_ids = {item['id'] for item in snapshot}

    placeholders = ','.join('?' for _ in expected_ids)
    current = conn.execute(
        'SELECT item_id, side, currency, account_id, party, settlement_key, priority, remaining '
        'FROM open_items WHERE item_id IN (%s)' % placeholders, list(expected_ids)).fetchall()
    if len(current) != len(expected_ids):
        raise ConflictError('planned component lost an open item')
    current_map = {
        row['item_id']: {
            'id': row['item_id'],
            'side': row['side'],
            'currency': row['currency'],
            'account_id': row['account_id'],
            'party': row['party'],
            'settlement_key': row['settlement_key'],
            'priority': row['priority'],
            'remaining': row['remaining'],
        }
        for row in current
    }
    expected_map = {item['id']: item for item in snapshot}
    for item_id, expected in expected_map.items():
        if current_map[item_id] != expected:
            raise ConflictError('planned component changed before commit: ' + item_id)

    # Rebuild the implicit graph with the same rules. A newly imported bridge
    # item that connects a planned item to the outside must invalidate the
    # plan, so adjacency is rebuilt over every open item in the ledger, not
    # just the planned snapshot.
    all_rows = conn.execute(
        'SELECT item_id, side, currency, account_id, party, settlement_key, priority, remaining '
        'FROM open_items WHERE remaining > 0 ORDER BY item_id').fetchall()
    all_items = [{
        'id': row['item_id'],
        'side': row['side'],
        'currency': row['currency'],
        'account_id': row['account_id'],
        'party': row['party'],
        'settlement_key': row['settlement_key'],
        'priority': row['priority'],
        'remaining': row['remaining'],
    } for row in all_rows]
    rebuilt_edges, adjacency = planner.build_graph(all_items, rules)
    planned_edges = [edge for edge in rebuilt_edges
                     if edge['debit_item_id'] in expected_ids
                     and edge['credit_item_id'] in expected_ids]
    keys = ('debit_item_id', 'credit_item_id', 'rule_priority',
            'debit_priority', 'credit_priority')
    if [{key: edge[key] for key in keys} for edge in planned_edges] != \
            [{key: edge[key] for key in keys} for edge in edges]:
        raise ConflictError('candidate settlement relationships changed before commit')

    rebuilt_closure = set()
    for component in planner.seed_components(seeds, adjacency):
        rebuilt_closure |= component
    if rebuilt_closure != expected_ids:
        raise ConflictError('component membership changed before commit')

    # Every edge touching a planned item must be part of the planned set.
    for edge in rebuilt_edges:
        if (edge['debit_item_id'] in expected_ids or
                edge['credit_item_id'] in expected_ids) and \
                edge['credit_item_id'] not in expected_ids:
            raise ConflictError('new bridge settlement relationship appeared before commit')


def commit(conn, plan_id):
    """Commit a plan after re-deriving and re-checking its component.

    The state transition, remaining-balance deductions, traceable flows and
    commit receipt all succeed or all fail inside one transaction. A plan that
    was already committed returns the original receipt even across processes.
    """
    if not isinstance(plan_id, str) or not plan_id:
        raise ValidationError('plan_id required')
    plan_row = conn.execute('SELECT * FROM plans WHERE plan_id = ?', (plan_id,)).fetchone()
    if plan_row is None:
        raise NotFoundError('plan not found: ' + plan_id)

    prior_receipt = conn.execute(
        'SELECT receipt_json FROM commits WHERE plan_id = ?', (plan_id,)).fetchone()
    if prior_receipt is not None:
        return json.loads(prior_receipt['receipt_json'])

    conn.execute('BEGIN IMMEDIATE')
    try:
        prior_receipt = conn.execute(
            'SELECT receipt_json FROM commits WHERE plan_id = ?', (plan_id,)).fetchone()
        if prior_receipt is not None:
            conn.commit()
            return json.loads(prior_receipt['receipt_json'])
        if conn.execute('SELECT state FROM plans WHERE plan_id = ?',
                        (plan_id,)).fetchone()['state'] != 'planned':
            raise ConflictError('plan is no longer committable: ' + plan_id)

        _revalidate(conn, plan_row)
        allocations = json.loads(plan_row['allocation_json'])

        # Re-check and deduct each participating item by the amount it
        # actually participates in; debit and credit sides are symmetric.
        totals = {}
        for allocation in allocations:
            for item_id in (allocation['debit_item_id'], allocation['credit_item_id']):
                totals[item_id] = totals.get(item_id, 0) + allocation['amount']
        for item_id, amount in totals.items():
            row = conn.execute(
                'SELECT remaining FROM open_items WHERE item_id = ?', (item_id,)).fetchone()
            if row is None or row['remaining'] < amount:
                raise ConflictError('insufficient remaining balance for item ' + item_id)

        next_seq = conn.execute(
            'SELECT COALESCE(MAX(commit_seq), 0) + 1 AS seq FROM commits').fetchone()['seq']
        for item_id, amount in totals.items():
            # Flow is recorded first; the update trigger then proves the new
            # remaining plus every committed flow equals the initial amount.
            conn.execute('INSERT INTO item_flows(plan_id, item_id, amount) VALUES (?, ?, ?)',
                         (plan_id, item_id, amount))
            conn.execute('UPDATE open_items SET remaining = remaining - ? WHERE item_id = ?',
                         (amount, item_id))
        for edge_seq, allocation in enumerate(allocations):
            conn.execute(
                'INSERT INTO plan_allocations(plan_id, edge_seq, debit_item_id, '
                'credit_item_id, amount) VALUES (?, ?, ?, ?, ?)',
                (plan_id, edge_seq, allocation['debit_item_id'],
                 allocation['credit_item_id'], allocation['amount']))
        conn.execute("UPDATE plans SET state = 'committed' WHERE plan_id = ?", (plan_id,))
        assert_conservation(conn)

        receipt = {
            'plan_id': plan_id,
            'commit_seq': next_seq,
            'state': 'committed',
            'total_amount': plan_row['total_amount'],
            'allocations': [
                {
                    'debit_item_id': allocation['debit_item_id'],
                    'credit_item_id': allocation['credit_item_id'],
                    'amount': allocation['amount'],
                }
                for allocation in allocations
            ],
        }
        conn.execute('INSERT INTO commits(plan_id, commit_seq, receipt_json) VALUES (?, ?, ?)',
                     (plan_id, next_seq, json.dumps(receipt, sort_keys=True)))
        conn.commit()
    except Exception:
        rollback_silent(conn)
        raise
    return receipt


__all__ = ['ingest', 'status', 'plan', 'commit', 'get_plan',
           'ConflictError', 'NotFoundError', 'ExhaustedError', 'LimitError',
           'ValidationError', 'OpenItemError']
