"""Tests for the local open-item write-off workbench.

Covers ingest validation and atomicity, the deterministic planner (including
the D1/D2/C1/C2 acceptance graph, multi-optimum canonical choice, isolated
seeds and 10**12 capacity), revalidating commits, idempotent cross-process
retry, concurrent competing commits and the real ``open-items`` CLI.
"""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from journal_check.openitems import (
    ConflictError,
    ExhaustedError,
    LimitError,
    NotFoundError,
    OpenItemError,
    ValidationError,
    commit,
    connect,
    get_plan,
    ingest,
    plan,
    status,
)
from journal_check.openitems import planner
from journal_check.openitems.storage import assert_conservation

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()


def entry(eid, debit_account, credit_account, amount, currency='USD'):
    return {
        'id': eid,
        'currency': currency,
        'lines': [
            {'account': debit_account, 'side': 'debit', 'amount': amount},
            {'account': credit_account, 'side': 'credit', 'amount': amount},
        ],
    }


def item(item_id, eid, line, party, settlement_key='K', priority=0):
    return {'id': item_id, 'entry': eid, 'line': line, 'party': party,
            'settlement_key': settlement_key, 'priority': priority}


def batch(batch_id, entries, items, accounts=None):
    if accounts is None:
        accounts = [{'id': 'ar', 'normal': 'debit'},
                    {'id': 'ap', 'normal': 'credit'}]
    return {'batch_id': batch_id, 'journal': {'accounts': accounts, 'entries': entries},
            'items': items}


def d1d2c1c2_batch(batch_id='b1'):
    entries = [entry('e1', 'ar', 'ap', 5), entry('e2', 'ar', 'ap', 5),
               entry('e3', 'ap', 'ar', 5), entry('e4', 'ap', 'ar', 5)]
    items = [item('D1', 'e1', 0, 'P1'), item('D2', 'e2', 0, 'P2'),
             item('C1', 'e3', 1, 'Q1'), item('C2', 'e4', 1, 'Q2')]
    return batch(batch_id, entries, items)


D1D2C1C2_RULES = [
    {'debit_party': 'P1', 'credit_party': 'Q1', 'priority': 0},
    {'debit_party': 'P1', 'credit_party': 'Q2', 'priority': 1},
    {'debit_party': 'P2', 'credit_party': 'Q1', 'priority': 2},
]


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / 'ledger.sqlite'
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def ingest_batch(self, data, digest=None):
        return ingest(self.conn, data, digest or sha(data))


class IngestTests(WorkbenchTest):
    def test_top_level_fields_exact(self):
        data = d1d2c1c2_batch()
        data['extra'] = 1
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)

    def test_item_fields_exact(self):
        data = d1d2c1c2_batch()
        data['items'][0]['extra'] = 1
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)

    def test_unknown_entry_reference_fails_atomically(self):
        data = d1d2c1c2_batch()
        data['items'][0]['entry'] = 'missing'
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)
        self.assertEqual(status(self.conn)['batches'], 0)
        self.assertEqual(status(self.conn)['open_item_count'], 0)

    def test_bad_line_index_fails_atomically(self):
        data = d1d2c1c2_batch()
        data['items'][1]['line'] = 9
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)
        self.assertEqual(status(self.conn)['open_item_count'], 0)

    def test_negative_line_rejected(self):
        data = d1d2c1c2_batch()
        data['items'][0]['line'] = -1
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)

    def test_same_line_opened_twice_rejected(self):
        data = d1d2c1c2_batch()
        duplicate = dict(data['items'][0])
        duplicate['id'] = 'DUP'
        data['items'].append(duplicate)
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)
        self.assertEqual(status(self.conn)['open_item_count'], 0)

    def test_duplicate_item_id_within_batch(self):
        data = d1d2c1c2_batch()
        data['items'][1]['id'] = 'D1'
        with self.assertRaises(ValidationError):
            self.ingest_batch(data)

    def test_duplicate_item_id_across_batches(self):
        self.ingest_batch(d1d2c1c2_batch())
        second = d1d2c1c2_batch('b2')
        second['items'][0]['id'] = 'D1'
        second['items'][0]['entry'] = 'e5'
        second['journal']['entries'].append(entry('e5', 'ar', 'ap', 7))
        with self.assertRaises(ConflictError):
            self.ingest_batch(second, sha(second))
        self.assertEqual(status(self.conn)['batches'], 1)

    def test_duplicate_account_lines_are_distinct_sources(self):
        data = d1d2c1c2_batch()
        # Both entries hit the same ar/ap accounts but remain separate items.
        receipt = self.ingest_batch(data)
        self.assertEqual(receipt['item_count'], 4)
        accounts = {row['account_id']: row for row in
                    self.conn.execute('SELECT account_id FROM accounts')}
        self.assertEqual(set(accounts), {'ar', 'ap'})

    def test_priority_bounds(self):
        data = d1d2c1c2_batch()
        data['items'][0]['priority'] = 10 ** 6
        self.ingest_batch(data)
        too_big = d1d2c1c2_batch('b2')
        too_big['items'][0]['priority'] = 10 ** 6 + 1
        with self.assertRaises(ValueError):
            self.ingest_batch(too_big, sha(too_big))

    def test_boolean_priority_rejected(self):
        data = d1d2c1c2_batch()
        data['items'][0]['priority'] = True
        with self.assertRaises(ValueError):
            self.ingest_batch(data)

    def test_cross_batch_account_normal_must_match(self):
        self.ingest_batch(d1d2c1c2_batch())
        other = batch('b2', [entry('z1', 'ar', 'newacct', 3)],
                      [item('Z', 'z1', 0, 'PZ')],
                      accounts=[{'id': 'ar', 'normal': 'credit'},
                                {'id': 'newacct', 'normal': 'credit'}])
        with self.assertRaises(ConflictError):
            self.ingest_batch(other, sha(other))
        self.assertEqual(status(self.conn)['batches'], 1)

    def test_new_account_allowed(self):
        self.ingest_batch(d1d2c1c2_batch())
        other = batch('b2', [entry('z1', 'newacct', 'ap', 3)],
                      [item('Z', 'z1', 0, 'PZ')],
                      accounts=[{'id': 'newacct', 'normal': 'debit'},
                                {'id': 'ap', 'normal': 'credit'}])
        self.ingest_batch(other, sha(other))
        self.assertEqual(status(self.conn)['batches'], 2)

    def test_duplicate_entry_across_batches_rejected(self):
        self.ingest_batch(d1d2c1c2_batch())
        other = d1d2c1c2_batch('b2')
        with self.assertRaises(ConflictError):
            self.ingest_batch(other, sha(other))

    def test_idempotent_same_bytes(self):
        data = d1d2c1c2_batch()
        digest = sha(data)
        first = self.ingest_batch(data, digest)
        second = self.ingest_batch(copy.deepcopy(data), digest)
        self.assertFalse(first['replayed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(status(self.conn)['open_item_count'], 4)
        self.assertEqual(status(self.conn)['batches'], 1)

    def test_same_batch_id_different_bytes_conflicts(self):
        data = d1d2c1c2_batch()
        self.ingest_batch(data, sha(data))
        changed = copy.deepcopy(data)
        changed['items'][0]['priority'] = 5
        with self.assertRaises(ConflictError):
            self.ingest_batch(changed, sha(changed))
        self.assertEqual(status(self.conn)['batches'], 1)

    def test_derived_side_currency_account(self):
        self.ingest_batch(d1d2c1c2_batch())
        open_items = {row['item_id']: row for row in self.conn.execute(
            'SELECT item_id, side, currency, account_id, initial_amount, remaining FROM open_items')}
        self.assertEqual(open_items['D1']['side'], 'debit')
        self.assertEqual(open_items['C1']['side'], 'credit')
        self.assertEqual(open_items['D1']['currency'], 'USD')
        self.assertEqual(open_items['D1']['account_id'], 'ar')
        self.assertEqual(open_items['D1']['initial_amount'], 5)
        self.assertEqual(open_items['D1']['remaining'], 5)

    def test_history_is_immutable(self):
        data = d1d2c1c2_batch()
        self.ingest_batch(data)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                'INSERT INTO entry_lines(entry_id, line_idx, account_id, side, amount) '
                "VALUES ('e1', 0, 'ar', 'debit', 1)")


class StatusTests(WorkbenchTest):
    def test_gross_amounts_not_netted(self):
        # Same account carries both an open debit and an open credit.
        entries = [entry('e1', 'shared', 'ap', 4), entry('e2', 'ap', 'shared', 3)]
        entries[0]['lines'][0]['account'] = 'shared'
        entries[1]['lines'][1]['account'] = 'shared'
        data = batch('b1', entries,
                     [item('D', 'e1', 0, 'P1'), item('C', 'e2', 1, 'Q1')],
                     accounts=[{'id': 'shared', 'normal': 'debit'},
                               {'id': 'ap', 'normal': 'credit'}])
        self.ingest_batch(data)
        report = status(self.conn)
        shared = [row for row in report['unsettled'] if row['account'] == 'shared'][0]
        self.assertEqual(shared['open_debit'], 4)
        self.assertEqual(shared['open_credit'], 3)
        initials = {row['id']: (row['initial_amount'], row['remaining'])
                    for row in report['open_items']}
        self.assertEqual(initials['D'], (4, 4))


class PlannerValidationTests(WorkbenchTest):
    def test_seed_not_found(self):
        self.ingest_batch(d1d2c1c2_batch())
        with self.assertRaises(NotFoundError):
            plan(self.conn, ['NOPE'], D1D2C1C2_RULES)

    def test_seed_exhausted_fails(self):
        self.ingest_batch(d1d2c1c2_batch())
        made = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        commit(self.conn, made['plan_id'])
        with self.assertRaises(ExhaustedError):
            plan(self.conn, ['D1'], D1D2C1C2_RULES)

    def test_rule_parties_must_differ(self):
        self.ingest_batch(d1d2c1c2_batch())
        rules = [{'debit_party': 'P1', 'credit_party': 'P1', 'priority': 0}]
        with self.assertRaises(ValidationError):
            plan(self.conn, ['D1'], rules)

    def test_rule_priority_bounds_and_uniqueness(self):
        self.ingest_batch(d1d2c1c2_batch())
        bad = [{'debit_party': 'P1', 'credit_party': 'Q1', 'priority': 10 ** 6 + 1}]
        with self.assertRaises(ValidationError):
            plan(self.conn, ['D1'], bad)
        dup = [
            {'debit_party': 'P1', 'credit_party': 'Q1', 'priority': 0},
            {'debit_party': 'P1', 'credit_party': 'Q1', 'priority': 1},
        ]
        with self.assertRaises(ValidationError):
            plan(self.conn, ['D1'], dup)

    def test_too_many_rules(self):
        rules = [{'debit_party': 'P%d' % i, 'credit_party': 'Q', 'priority': 0}
                 for i in range(501)]
        with self.assertRaises(ValidationError):
            plan(self.conn, ['D1'], rules)

    def test_component_limits_fail_explicitly(self):
        # Build >100 items in one component via many entries.
        accounts = [{'id': 'ar', 'normal': 'debit'}, {'id': 'ap', 'normal': 'credit'}]
        entries = []
        items = []
        for i in range(51):
            entries.append(entry('d%d' % i, 'ar', 'ap', 1))
            items.append(item('D%03d' % i, 'd%d' % i, 0, 'P'))
        for i in range(51):
            entries.append(entry('c%d' % i, 'ap', 'ar', 1))
            items.append(item('C%03d' % i, 'c%d' % i, 1, 'Q'))
        self.ingest_batch(batch('big', entries, items, accounts))
        rules = [{'debit_party': 'P', 'credit_party': 'Q', 'priority': 0}]
        with self.assertRaises(LimitError):
            plan(self.conn, ['D000'], rules)
        # Nothing was persisted for the failed plan.
        self.assertEqual(status(self.conn)['plans']['planned'], 0)


class PlannerOptimisationTests(WorkbenchTest):
    def test_acceptance_graph_global_maximum(self):
        self.ingest_batch(d1d2c1c2_batch())
        report = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        self.assertEqual(report['total_amount'], 10)
        allocations = {(a['debit_item_id'], a['credit_item_id']): a['amount']
                       for a in report['allocations']}
        # The highest-priority edge D1-C1 must carry zero, not first-come 5.
        self.assertEqual(allocations.get(('D1', 'C1'), 0), 0)
        self.assertEqual(allocations[('D1', 'C2')], 5)
        self.assertEqual(allocations[('D2', 'C1')], 5)
        after = {row['item_id']: row['remaining_after']
                 for row in report['item_remaining_after']}
        self.assertTrue(all(value == 0 for value in after.values()))
        self.assertIsInstance(report['plan_id'], str)
        self.assertEqual(len(report['plan_id']), 64)

    def test_deterministic_independent_of_input_order(self):
        self.ingest_batch(d1d2c1c2_batch())
        first = plan(self.conn, ['D2', 'D1', 'C2', 'C1'], list(reversed(D1D2C1C2_RULES)))
        second = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        self.assertEqual(first['plan_id'], second['plan_id'])
        self.assertEqual(first['allocations'], second['allocations'])

    def test_multi_optimum_canonical_choice(self):
        entries = [entry('e1', 'ar', 'ap', 10), entry('e2', 'ap', 'ar', 10),
                   entry('e3', 'ap', 'ar', 10)]
        data = batch('b1', entries,
                     [item('D', 'e1', 0, 'P'), item('C1', 'e2', 1, 'Q1'),
                      item('C2', 'e3', 1, 'Q2')])
        self.ingest_batch(data)
        rules = [{'debit_party': 'P', 'credit_party': 'Q1', 'priority': 0},
                 {'debit_party': 'P', 'credit_party': 'Q2', 'priority': 0}]
        report = plan(self.conn, ['D'], rules)
        self.assertEqual(report['total_amount'], 10)
        self.assertEqual(report['allocations'],
                         [{'debit_item_id': 'D', 'credit_item_id': 'C1', 'amount': 10}])

    def test_isolated_seed_is_legal(self):
        self.ingest_batch(d1d2c1c2_batch())
        report = plan(self.conn, ['D1'], [])
        self.assertEqual(report['total_amount'], 0)
        self.assertEqual(report['components'], [['D1']])
        self.assertEqual(report['allocations'], [])
        self.assertEqual(report['item_remaining_after'][0]['remaining_after'], 5)

    def test_large_capacity_10_power_12(self):
        entries = [entry('e1', 'ar', 'ap', 10 ** 12),
                   entry('e2', 'ap', 'ar', 10 ** 12)]
        data = batch('b1', entries,
                     [item('D', 'e1', 0, 'P'), item('C', 'e2', 1, 'Q')])
        self.ingest_batch(data)
        report = plan(self.conn, ['D'],
                      [{'debit_party': 'P', 'credit_party': 'Q', 'priority': 0}])
        self.assertEqual(report['total_amount'], 10 ** 12)
        self.assertTrue(all(row['remaining_after'] == 0
                            for row in report['item_remaining_after']))

    def test_edges_require_same_currency_and_settlement_key(self):
        entries = [entry('e1', 'ar', 'ap', 5, currency='USD'),
                   entry('e2', 'ap', 'ar', 5, currency='EUR')]
        data = batch('b1', entries,
                     [item('D', 'e1', 0, 'P', 'K1'),
                      item('C', 'e2', 1, 'Q', 'K1')])
        self.ingest_batch(data)
        report = plan(self.conn, ['D'],
                      [{'debit_party': 'P', 'credit_party': 'Q', 'priority': 0}])
        self.assertEqual(report['total_amount'], 0)

    def test_only_seed_component_is_planned(self):
        # Two disconnected components; seed only the first.
        entries = [entry('e1', 'ar', 'ap', 5), entry('e2', 'ap', 'ar', 5),
                   entry('e3', 'ar', 'ap', 5), entry('e4', 'ap', 'ar', 5)]
        data = batch('b1', entries,
                     [item('D1', 'e1', 0, 'P1', 'K1'),
                      item('C1', 'e2', 1, 'Q1', 'K1'),
                      item('D2', 'e3', 0, 'P2', 'K2'),
                      item('C2', 'e4', 1, 'Q2', 'K2')])
        self.ingest_batch(data)
        rules = [{'debit_party': 'P1', 'credit_party': 'Q1', 'priority': 0},
                 {'debit_party': 'P2', 'credit_party': 'Q2', 'priority': 0}]
        report = plan(self.conn, ['D1'], rules)
        self.assertEqual(report['components'], [['C1', 'D1']])
        self.assertEqual(report['total_amount'], 5)


class CommitTests(WorkbenchTest):
    def plan_and_commit(self):
        self.ingest_batch(d1d2c1c2_batch())
        made = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        receipt = commit(self.conn, made['plan_id'])
        return made, receipt

    def test_commit_deducts_and_conserves(self):
        made, receipt = self.plan_and_commit()
        self.assertEqual(receipt['total_amount'], 10)
        assert_conservation(self.conn)
        report = status(self.conn)
        self.assertTrue(all(item['remaining'] == 0 for item in report['open_items']))
        stored = get_plan(self.conn, made['plan_id'])
        self.assertEqual(stored['state'], 'committed')

    def test_commit_unknown_plan(self):
        with self.assertRaises(NotFoundError):
            commit(self.conn, 'deadbeef')

    def test_retry_returns_original_receipt(self):
        made, receipt = self.plan_and_commit()
        again = commit(self.conn, made['plan_id'])
        self.assertEqual(again, receipt)
        # A retry must not deduct again.
        assert_conservation(self.conn)
        self.assertEqual(status(self.conn)['plans']['committed'], 1)

    def test_conflict_when_component_changes(self):
        self.ingest_batch(d1d2c1c2_batch())
        made = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)

        # A new bridge open item connects this component to a formerly
        # disconnected item C, satisfying an existing allowed party pair.
        bridge_entries = [entry('e5', 'ar', 'ap', 5)]
        bridge = batch('b2', bridge_entries,
                       [item('D3', 'e5', 0, 'P2', 'K')])
        before = {row['item_id']: row['remaining']
                  for row in self.conn.execute('SELECT item_id, remaining FROM open_items')}
        self.ingest_batch(bridge, sha(bridge))
        with self.assertRaises(ConflictError):
            commit(self.conn, made['plan_id'])
        # No balance changed by the failed commit.
        after = {row['item_id']: row['remaining']
                 for row in self.conn.execute('SELECT item_id, remaining FROM open_items')}
        self.assertEqual(before, {key: value for key, value in after.items()
                                  if key in before})
        self.assertEqual(get_plan(self.conn, made['plan_id'])['state'], 'planned')

    def test_disconnected_batch_does_not_invalidate(self):
        self.ingest_batch(d1d2c1c2_batch())
        made = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        other = batch('b2', [entry('e9', 'ar', 'ap', 5)],
                      [item('ISO', 'e9', 0, 'ZZ', 'OTHER-KEY')])
        self.ingest_batch(other, sha(other))
        receipt = commit(self.conn, made['plan_id'])
        self.assertEqual(receipt['state'], 'committed')

    def test_revalidation_catches_remaining_change(self):
        self.ingest_batch(d1d2c1c2_batch())
        made = plan(self.conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        # Commit a different plan touching D1 first.
        other_rules = [{'debit_party': 'P1', 'credit_party': 'Q2', 'priority': 0}]
        competing = plan(self.conn, ['D1'], other_rules)
        commit(self.conn, competing['plan_id'])
        with self.assertRaises(ConflictError):
            commit(self.conn, made['plan_id'])

    def test_traceable_flows_written(self):
        made, receipt = self.plan_and_commit()
        flows = self.conn.execute(
            'SELECT item_id, amount FROM item_flows WHERE plan_id = ? ORDER BY item_id',
            (made['plan_id'],)).fetchall()
        self.assertEqual({row['item_id']: row['amount'] for row in flows},
                         {'D1': 5, 'D2': 5, 'C1': 5, 'C2': 5})


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / 'ledger.sqlite'
        conn = connect(self.db)
        ingest(conn, d1d2c1c2_batch(), sha(d1d2c1c2_batch()))
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_two_connections_competing_commit(self):
        conn_a = connect(self.db)
        conn_b = connect(self.db)
        plan_a = plan(conn_a, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        # Different rules produce a different plan_id over the same balances.
        other_rules = [
            {'debit_party': 'P1', 'credit_party': 'Q2', 'priority': 0},
            {'debit_party': 'P2', 'credit_party': 'Q1', 'priority': 0},
        ]
        plan_b = plan(conn_b, ['D1', 'D2', 'C1', 'C2'], other_rules)
        self.assertNotEqual(plan_a['plan_id'], plan_b['plan_id'])

        receipt_a = commit(conn_a, plan_a['plan_id'])
        with self.assertRaises(ConflictError):
            commit(conn_b, plan_b['plan_id'])

        # Reopen and confirm the winning plan still returns its original receipt.
        conn_a.close()
        conn_b.close()
        reopened = connect(self.db)
        self.assertEqual(commit(reopened, plan_a['plan_id']), receipt_a)
        report = status(reopened)
        self.assertTrue(all(item['remaining'] == 0 for item in report['open_items']))
        assert_conservation(reopened)
        reopened.close()

    def test_error_before_commit_point_still_committable_after_reopen(self):
        conn = connect(self.db)
        made = plan(conn, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        # Force an error (invalid plan id) before any commit point.
        with self.assertRaises(NotFoundError):
            commit(conn, 'does-not-exist')
        conn.close()
        reopened = connect(self.db)
        receipt = commit(reopened, made['plan_id'])
        self.assertEqual(receipt['state'], 'committed')
        reopened.close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.td = Path(self.tempdir.name)
        self.db = self.td / 'ledger.sqlite'
        self.batch_path = self.td / 'batch.json'
        self.batch_path.write_text(json.dumps(d1d2c1c2_batch()), encoding='utf-8')
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

    def tearDown(self):
        self.tempdir.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'journal_check.cli', *args],
                              capture_output=True, text=True, env=self.env, cwd=str(ROOT))

    def test_full_cli_lifecycle(self):
        result = self.cli('open-items', 'ingest', '--db', str(self.db),
                          '--source', str(self.batch_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['item_count'], 4)

        result = self.cli('open-items', 'status', '--db', str(self.db))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['open_item_count'], 4)

        request = self.td / 'request.json'
        request.write_text(json.dumps(
            {'seeds': ['D1', 'D2', 'C1', 'C2'], 'rules': D1D2C1C2_RULES}))
        result = self.cli('open-items', 'plan', '--db', str(self.db),
                          '--request', str(request))
        self.assertEqual(result.returncode, 0, result.stderr)
        planned = json.loads(result.stdout)
        self.assertEqual(planned['total_amount'], 10)
        plan_id = planned['plan_id']

        result = self.cli('open-items', 'commit', '--db', str(self.db),
                          '--plan-id', plan_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        committed = json.loads(result.stdout)
        self.assertEqual(committed['state'], 'committed')

        # Retry in a brand new process returns the same receipt.
        result = self.cli('open-items', 'commit', '--db', str(self.db),
                          '--plan-id', plan_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), committed)

    def test_cli_failure_exit_2_and_keeps_output(self):
        bad = self.td / 'bad.json'
        bad.write_text(json.dumps({'batch_id': 'x'}))
        out = self.td / 'out.json'
        out.write_text('prior')
        result = self.cli('open-items', 'ingest', '--db', str(self.db),
                          '--source', str(bad), '--output', str(out))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'prior')

    def test_cli_output_file_written(self):
        out = self.td / 'status.json'
        self.cli('open-items', 'ingest', '--db', str(self.db),
                 '--source', str(self.batch_path))
        result = self.cli('open-items', 'status', '--db', str(self.db),
                          '--output', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text())['open_item_count'], 4)


class JointAcceptanceTests(unittest.TestCase):
    """End-to-end scenarios from the joint acceptance narrative."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.td = Path(self.tempdir.name)
        self.db = self.td / 'ledger.sqlite'
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

    def tearDown(self):
        self.tempdir.cleanup()

    def two_components_batch(self):
        # Component A uses settlement key KA; component B uses KB.
        entries = [entry('a1', 'ar', 'ap', 5), entry('a2', 'ap', 'ar', 5),
                   entry('b1', 'ar', 'ap', 7), entry('b2', 'ap', 'ar', 7)]
        items = [item('AD', 'a1', 0, 'PA', 'KA'), item('AC', 'a2', 1, 'QA', 'KA'),
                 item('BD', 'b1', 0, 'PB', 'KB'), item('BC', 'b2', 1, 'QB', 'KB')]
        return batch('joint', entries, items)

    def test_plan_a_then_import_disjoint_b_batch_still_commits(self):
        conn = connect(self.db)
        ingest(conn, self.two_components_batch(), sha(self.two_components_batch()))
        rules_a = [{'debit_party': 'PA', 'credit_party': 'QA', 'priority': 0}]
        planned = plan(conn, ['AD'], rules_a)
        self.assertEqual(planned['components'], [['AC', 'AD']])

        # Import a second batch that only touches an unrelated component B'.
        other = batch('other', [entry('o1', 'ar', 'ap', 3), entry('o2', 'ap', 'ar', 3)],
                      [item('OD', 'o1', 0, 'PX', 'KC'), item('OC', 'o2', 1, 'QX', 'KC')])
        ingest(conn, other, sha(other))

        receipt = commit(conn, planned['plan_id'])
        self.assertEqual(receipt['state'], 'committed')
        assert_conservation(conn)
        # Component B is untouched.
        remaining = {row['item_id']: row['remaining']
                     for row in conn.execute('SELECT item_id, remaining FROM open_items')}
        self.assertEqual(remaining['BD'], 7)
        self.assertEqual(remaining['BC'], 7)
        conn.close()

    def test_plan_a_then_bridge_to_c_conflicts_without_balance_change(self):
        conn = connect(self.db)
        # Same settlement key partition, but C (CD, party PC) is isolated:
        # no rule pairs PC with the existing credit QA.
        entries = [entry('a1', 'ar', 'ap', 5), entry('a2', 'ap', 'ar', 5),
                   entry('c1', 'ar', 'ap', 4)]
        data = batch('joint', entries,
                     [item('AD', 'a1', 0, 'PA', 'KA'),
                      item('AC', 'a2', 1, 'QA', 'KA'),
                      item('CD', 'c1', 0, 'PC', 'KA')])
        ingest(conn, data, sha(data))
        # PA->QB and PC->QB are dormant at plan time: no QB credit exists yet.
        rules = [
            {'debit_party': 'PA', 'credit_party': 'QA', 'priority': 0},
            {'debit_party': 'PA', 'credit_party': 'QB', 'priority': 1},
            {'debit_party': 'PC', 'credit_party': 'QB', 'priority': 2},
        ]
        planned = plan(conn, ['AD'], rules)
        self.assertEqual(planned['components'], [['AC', 'AD']])
        balances_before = {row['item_id']: row['remaining']
                           for row in conn.execute('SELECT item_id, remaining FROM open_items')}

        # A new credit open item (party QB, same key) satisfies the dormant
        # rules and bridges component A to the formerly isolated item CD.
        bridge_batch = batch('bridge', [entry('br1', 'ap', 'ar', 4)],
                             [item('BR', 'br1', 1, 'QB', 'KA')])
        ingest(conn, bridge_batch, sha(bridge_batch))

        with self.assertRaises(ConflictError):
            commit(conn, planned['plan_id'])

        # No half deduction: balances unchanged, no flows/allocations, plan stays planned.
        balances_after = {row['item_id']: row['remaining']
                          for row in conn.execute('SELECT item_id, remaining FROM open_items')}
        self.assertEqual(balances_before,
                         {key: value for key, value in balances_after.items()
                          if key in balances_before})
        self.assertEqual(
            conn.execute('SELECT COUNT(*) AS n FROM item_flows WHERE plan_id = ?',
                         (planned['plan_id'],)).fetchone()['n'], 0)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) AS n FROM plan_allocations WHERE plan_id = ?',
                         (planned['plan_id'],)).fetchone()['n'], 0)
        self.assertEqual(get_plan(conn, planned['plan_id'])['state'], 'planned')
        assert_conservation(conn)
        conn.close()

    def test_competing_plans_one_wins_then_reopen_retry_and_conservation(self):
        conn_a = connect(self.db)
        ingest(conn_a, d1d2c1c2_batch(), sha(d1d2c1c2_batch()))
        conn_b = connect(self.db)

        plan_a = plan(conn_a, ['D1', 'D2', 'C1', 'C2'], D1D2C1C2_RULES)
        rules_b = [
            {'debit_party': 'P1', 'credit_party': 'Q2', 'priority': 0},
            {'debit_party': 'P2', 'credit_party': 'Q1', 'priority': 0},
        ]
        plan_b = plan(conn_b, ['D1', 'D2', 'C1', 'C2'], rules_b)
        self.assertNotEqual(plan_a['plan_id'], plan_b['plan_id'])

        receipt = commit(conn_a, plan_a['plan_id'])
        with self.assertRaises(ConflictError):
            commit(conn_b, plan_b['plan_id'])
        conn_a.close()
        conn_b.close()

        # Reopen like a brand new process: winner retries to its original receipt,
        # loser still conflicts, and per-item conservation holds.
        reopened = connect(self.db)
        self.assertEqual(commit(reopened, plan_a['plan_id']), receipt)
        with self.assertRaises(ConflictError):
            commit(reopened, plan_b['plan_id'])
        report = status(reopened)
        for open_item in report['open_items']:
            self.assertEqual(open_item['initial_amount'],
                             open_item['remaining'] + self._used(reopened, open_item['id']))
            self.assertGreaterEqual(open_item['remaining'], 0)
        assert_conservation(reopened)
        reopened.close()

    @staticmethod
    def _used(conn, item_id):
        row = conn.execute('SELECT COALESCE(SUM(amount), 0) AS used FROM item_flows '
                           'WHERE item_id = ?', (item_id,)).fetchone()
        return row['used']

    def test_subprocess_independent_process_retry(self):
        # Plan and commit through separate real processes.
        batch_file = self.td / 'batch.json'
        batch_file.write_text(json.dumps(d1d2c1c2_batch()))
        request_file = self.td / 'request.json'
        request_file.write_text(json.dumps(
            {'seeds': ['D1', 'D2', 'C1', 'C2'], 'rules': D1D2C1C2_RULES}))

        def cli(*args):
            return subprocess.run(
                [sys.executable, '-m', 'journal_check.cli', *args],
                capture_output=True, text=True, env=self.env, cwd=str(ROOT))

        self.assertEqual(cli('open-items', 'ingest', '--db', str(self.db),
                             '--source', str(batch_file)).returncode, 0)
        planned = json.loads(cli('open-items', 'plan', '--db', str(self.db),
                                 '--request', str(request_file)).stdout)
        first = cli('open-items', 'commit', '--db', str(self.db),
                    '--plan-id', planned['plan_id'])
        second = cli('open-items', 'commit', '--db', str(self.db),
                     '--plan-id', planned['plan_id'])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        report = json.loads(cli('open-items', 'status', '--db', str(self.db)).stdout)
        self.assertTrue(all(item['remaining'] == 0 for item in report['open_items']))


if __name__ == '__main__':
    unittest.main()
