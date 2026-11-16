"""Tests for the inventory-cost attribution and catch-up invoice workbench.

Covers the exact batch-prefix costing (no float, no averaged unit price), the
traced physical interval flow (FIFO issues, reverse-order returns restoring
original receipt indices, cross-batch issues, partial and over-returns),
reprice revaluation along the current ownership partition (including units
issued, returned and re-issued), double-entry postings with per-attribution
line traceability, the per-currency trial balance and its reconciliations,
10**12-quantity / 10**18-cost scaling, strict validation, input-arrangement
independence and the real ``inventory-cost`` CLI with safe publication.
"""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from journal_check.inventory_cost import inventory_cost, run_file
from journal_check.inventory_cost.costing import prefix_cost, segments_cost
from journal_check.inventory_cost.errors import InventoryCostError
from journal_check.inventory_cost.intervals import (
    restore,
    take_prefix,
    take_tail,
    total_length,
)
from journal_check.inventory_cost.validation import (
    COST_MAX,
    EVENT_LIMIT,
    QUANTITY_MAX,
    STOCK_LIMIT,
)
from journal_check.journal import parse_journal

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'inventory_cost_request.json'


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def account(account_id, normal='debit'):
    return {'id': account_id, 'normal': normal}


def stock(sku='S', currency='USD', inventory_account='inv',
          payable_account='ap'):
    return {'sku': sku, 'currency': currency,
            'inventory_account': inventory_account,
            'payable_account': payable_account}


def receipt(rid, seq, sku='S', currency='USD', quantity=1, total_cost=0):
    return {'id': rid, 'seq': seq, 'type': 'receipt', 'sku': sku,
            'currency': currency, 'quantity': quantity,
            'total_cost': total_cost}


def issue(eid, seq, quantity, expense_account='exp', sku='S',
          currency='USD'):
    return {'id': eid, 'seq': seq, 'type': 'issue', 'sku': sku,
            'currency': currency, 'quantity': quantity,
            'expense_account': expense_account}


def ret(rid, seq, issue_id, quantity):
    return {'id': rid, 'seq': seq, 'type': 'return', 'issue': issue_id,
            'quantity': quantity}


def reprice(rid, seq, receipt_id, total_cost):
    return {'id': rid, 'seq': seq, 'type': 'reprice',
            'receipt': receipt_id, 'total_cost': total_cost}


ACCOUNTS = [account('inv'), account('ap', 'credit'),
            account('expA'), account('expB')]


def request(events, stocks=None, accounts=None):
    return {'accounts': accounts or ACCOUNTS,
            'stocks': stocks if stocks is not None else [stock()],
            'events': events}


def unit_costs(total_cost, quantity):
    return [prefix_cost(total_cost, quantity, i, i + 1)
            for i in range(quantity)]


def tb_map(report, currency='USD'):
    block = next(b for b in report['trial_balance']['currencies']
                 if b['currency'] == currency)
    return {row['account']: row for row in block['accounts']}


def trace_map(report):
    return {row['event']: row for row in report['physical_trace']}


def entry_map(report):
    return {row['entry']: row for row in report['entries']}


# ---------------------------------------------------------------------------
# Exact prefix costing
# ---------------------------------------------------------------------------

class PrefixCostTests(unittest.TestCase):
    def test_q3_c5_gives_1_2_2_not_three_equal_costs(self):
        self.assertEqual(unit_costs(5, 3), [1, 2, 2])

    def test_q3_c7_gives_2_2_3(self):
        self.assertEqual(unit_costs(7, 3), [2, 2, 3])

    def test_reprice_differences_are_not_averaged_equal(self):
        old = unit_costs(5, 3)
        new = unit_costs(7, 3)
        differences = [n - o for n, o in zip(new, old)]
        # The three units move by 1, 0, 1 — never three equal 2/3-style diffs.
        self.assertEqual(differences, [1, 0, 1])

    def test_markdown_and_zero(self):
        self.assertEqual(unit_costs(4, 3), [1, 1, 2])
        self.assertEqual(unit_costs(0, 3), [0, 0, 0])
        self.assertEqual(prefix_cost(0, 3, 0, 3), 0)

    def test_whole_batch_prefix_always_equals_total_cost(self):
        for cost in (0, 1, 5, 17, 10 ** 18, 10 ** 18 - 1):
            self.assertEqual(prefix_cost(cost, 10 ** 12, 0, 10 ** 12),
                             cost)

    def test_interval_sums_match_prefix_definition(self):
        # floor(C*b/Q) - floor(C*a/Q) over disjoint pieces equals the whole.
        pieces = [(0, 2), (2, 7), (7, 11)]
        self.assertEqual(segments_cost(123457, 11, pieces),
                         prefix_cost(123457, 11, 0, 11))

    def test_no_float_at_scale(self):
        # 10**18 over 10**12 units; endpoints anywhere, arithmetic stays exact.
        q = 10 ** 12
        c = 10 ** 18
        self.assertEqual(prefix_cost(c, q, q // 3, (2 * q) // 3),
                         (c * ((2 * q) // 3)) // q
                         - (c * (q // 3)) // q)

    def test_bounds_rejected(self):
        with self.assertRaises(ValueError):
            prefix_cost(5, 3, 1, 4)
        with self.assertRaises(ValueError):
            prefix_cost(5, 3, 2, 1)


# ---------------------------------------------------------------------------
# Interval algebra (unit tests of the physical primitives)
# ---------------------------------------------------------------------------

class IntervalAlgebraTests(unittest.TestCase):
    def test_take_prefix_keeps_lowest_indices(self):
        taken, remaining = take_prefix([(0, 5)], 3)
        self.assertEqual(taken, [(0, 3)])
        self.assertEqual(remaining, [(3, 5)])

    def test_take_prefix_across_fragments(self):
        taken, remaining = take_prefix([(0, 2), (4, 7)], 4)
        self.assertEqual(taken, [(0, 2), (4, 6)])
        self.assertEqual(remaining, [(6, 7)])
        self.assertEqual(total_length(taken), 4)

    def test_take_prefix_insufficient_fails_without_mutation(self):
        original = [(0, 1), (3, 4)]
        with self.assertRaises(ValueError):
            take_prefix(original, 5)
        self.assertEqual(original, [(0, 1), (3, 4)])

    def test_take_tail_reverses_allocation_order(self):
        # The newest fragment [4,7) has exactly 3 units and fully satisfies
        # the return, so the older fragment is left untouched.
        removed, kept = take_tail([(0, 2), (4, 7)], 3)
        self.assertEqual(removed, [(4, 7)])
        self.assertEqual(kept, [(0, 2)])

    def test_take_tail_spills_into_previous_fragment(self):
        # Need 4: the whole newest fragment (3) plus the tail unit (1) of the
        # older fragment.
        removed, kept = take_tail([(0, 2), (4, 7)], 4)
        self.assertEqual(removed, [(1, 2), (4, 7)])
        self.assertEqual(kept, [(0, 1)])

    def test_take_tail_within_single_fragment(self):
        removed, kept = take_tail([(4, 7)], 2)
        self.assertEqual(removed, [(5, 7)])
        self.assertEqual(kept, [(4, 5)])

    def test_take_tail_labeled_keeps_receipt_identity_across_batches(self):
        from journal_check.inventory_cost.intervals import take_tail_labeled
        # Indices restart at 0 in each receipt; labels disambiguate them.
        segments = [('r1', 0, 2), ('r2', 0, 1)]
        removed, kept = take_tail_labeled(segments, 2)
        self.assertEqual(removed, [('r1', 1, 2), ('r2', 0, 1)])
        self.assertEqual(kept, [('r1', 0, 1)])

    def test_take_tail_over_hold_rejected(self):
        with self.assertRaises(ValueError):
            take_tail([(0, 2)], 3)

    def test_restore_merges_contacts_and_sorts(self):
        merged = restore([(3, 5)], [(0, 1), (1, 3)])
        self.assertEqual(merged, [(0, 5)])

    def test_restore_dedupes_contained_fragments(self):
        merged = restore([(0, 5)], [(1, 3)])
        self.assertEqual(merged, [(0, 5)])


# ---------------------------------------------------------------------------
# Physical flow: FIFO issues, reverse-order returns, cross-batch traces
# ---------------------------------------------------------------------------

class PhysicalFlowTests(unittest.TestCase):
    def test_spec_acceptance_return_is_1_3_and_next_issue_reuses_it(self):
        # Same batch [0,5): issue 3, return 2 of that issue, then issue 2.
        events = [
            receipt('r', 1, quantity=5, total_cost=10),
            issue('i1', 2, 3, 'expA'),
            ret('rt', 3, 'i1', 2),
            issue('i2', 4, 2, 'expB'),
        ]
        report = inventory_cost(request(events))
        trace = trace_map(report)
        self.assertEqual(trace['i1']['allocation'],
                         [{'receipt': 'r', 'start': 0, 'end': 3}])
        # Reverse allocation order: unit 2 then unit 1 -> restored [1,3).
        self.assertEqual(trace['rt']['allocation'],
                         [{'receipt': 'r', 'start': 1, 'end': 3}])
        # The restored units keep their original receipt age and are taken
        # again before any newer batch.
        self.assertEqual(trace['i2']['allocation'],
                         [{'receipt': 'r', 'start': 1, 'end': 3}])
        positions = {p['issue']: p
                     for p in report['final_positions']['issues']}
        self.assertEqual(positions['i1']['unreturned_intervals'],
                         [{'receipt': 'r', 'start': 0, 'end': 1}])
        self.assertEqual(positions['i2']['unreturned_intervals'],
                         [{'receipt': 'r', 'start': 1, 'end': 3}])
        batch = report['final_positions']['receipts'][0]
        self.assertEqual(batch['available_intervals'],
                         [{'receipt': 'r', 'start': 3, 'end': 5}])

    def test_cross_batch_issue_spans_receipts_in_fifo_order(self):
        events = [
            receipt('r1', 1, quantity=2, total_cost=2),
            receipt('r2', 2, quantity=3, total_cost=30),
            issue('i1', 3, 4, 'expA'),
        ]
        report = inventory_cost(request(events))
        self.assertEqual(trace_map(report)['i1']['allocation'], [
            {'receipt': 'r1', 'start': 0, 'end': 2},
            {'receipt': 'r2', 'start': 0, 'end': 2},
        ])

    def test_returned_unit_is_not_pushed_behind_newer_batches(self):
        events = [
            receipt('r1', 1, quantity=2, total_cost=2),
            receipt('r2', 2, quantity=2, total_cost=100),
            issue('i1', 3, 2, 'expA'),
            ret('rt', 4, 'i1', 1),
            issue('i2', 5, 2, 'expB'),
        ]
        report = inventory_cost(request(events))
        # i2 takes the restored r1 unit first, then only one r2 unit.
        self.assertEqual(trace_map(report)['i2']['allocation'], [
            {'receipt': 'r1', 'start': 1, 'end': 2},
            {'receipt': 'r2', 'start': 0, 'end': 1},
        ])

    def test_restored_unit_keeps_fifo_age_within_same_batch(self):
        events = [
            receipt('r', 1, quantity=6, total_cost=60),
            issue('iA', 2, 4, 'expA'),
            ret('rt', 3, 'iA', 1),      # unit 3 restored; pool has [3,6)
            issue('iB', 4, 2, 'expB'),  # must take [3,5), not [4,6)
        ]
        report = inventory_cost(request(events))
        self.assertEqual(trace_map(report)['iB']['allocation'],
                         [{'receipt': 'r', 'start': 3, 'end': 5}])
        batch = report['final_positions']['receipts'][0]
        self.assertEqual(batch['available_intervals'],
                         [{'receipt': 'r', 'start': 5, 'end': 6}])

    def test_non_contiguous_restoration_merges_into_canonical_availability(self):
        events = [
            receipt('r', 1, quantity=6, total_cost=60),
            issue('i1', 2, 6, 'expA'),
            ret('rt1', 3, 'i1', 1),      # restore unit 5
            ret('rt2', 4, 'i1', 1),      # restore unit 4 -> merge into [4,6)
            ret('rt3', 5, 'i1', 2),      # restore units 3, 2 -> [2,6)
        ]
        report = inventory_cost(request(events))
        batch = report['final_positions']['receipts'][0]
        self.assertEqual(batch['available_intervals'],
                         [{'receipt': 'r', 'start': 2, 'end': 6}])
        self.assertEqual(batch['available_quantity'], 4)

    def test_repeated_partial_returns_and_reissues(self):
        events = [
            receipt('r', 1, quantity=6, total_cost=60),
            issue('i1', 2, 4, 'expA'),
            ret('rt1', 3, 'i1', 1),   # undo unit 3
            issue('i2', 4, 1, 'expB'),  # re-take unit 3
            ret('rt2', 5, 'i1', 2),   # undo units 2, 1 (reverse order)
            issue('i3', 6, 2, 'expB'),  # re-take units 1, 2
        ]
        report = inventory_cost(request(events))
        trace = trace_map(report)
        self.assertEqual(trace['rt1']['allocation'],
                         [{'receipt': 'r', 'start': 3, 'end': 4}])
        self.assertEqual(trace['i2']['allocation'],
                         [{'receipt': 'r', 'start': 3, 'end': 4}])
        self.assertEqual(trace['rt2']['allocation'],
                         [{'receipt': 'r', 'start': 1, 'end': 3}])
        self.assertEqual(trace['i3']['allocation'],
                         [{'receipt': 'r', 'start': 1, 'end': 3}])
        self.assertTrue(report['reconciliations']['all_balanced'])

    def test_cross_batch_return_undoes_newest_receipt_first(self):
        events = [
            receipt('r1', 1, quantity=2, total_cost=2),
            receipt('r2', 2, quantity=2, total_cost=20),
            issue('i1', 3, 3, 'expA'),       # r1[0,2) + r2[0,1)
            ret('rt', 4, 'i1', 2),           # r2 unit0 then r1 unit1
        ]
        report = inventory_cost(request(events))
        self.assertEqual(trace_map(report)['rt']['allocation'], [
            {'receipt': 'r1', 'start': 1, 'end': 2},
            {'receipt': 'r2', 'start': 0, 'end': 1},
        ])
        positions = {p['issue']: p
                     for p in report['final_positions']['issues']}
        self.assertEqual(positions['i1']['unreturned_intervals'],
                         [{'receipt': 'r1', 'start': 0, 'end': 1}])

    def test_over_return_rejects_whole_request(self):
        events = [receipt('r', 1, quantity=3, total_cost=3),
                  issue('i1', 2, 2, 'expA'),
                  ret('rt', 3, 'i1', 3)]
        with self.assertRaises(InventoryCostError):
            inventory_cost(request(events))

    def test_cumulative_return_cannot_exceed_issue_quantity(self):
        events = [receipt('r', 1, quantity=5, total_cost=5),
                  issue('i1', 2, 2, 'expA'),
                  ret('rt1', 3, 'i1', 1),
                  ret('rt2', 4, 'i1', 1),
                  ret('rt3', 5, 'i1', 1)]
        with self.assertRaises(InventoryCostError):
            inventory_cost(request(events))

    def test_issue_more_than_pool_rejected(self):
        events = [receipt('r', 1, quantity=2, total_cost=2),
                  issue('i1', 2, 3, 'expA')]
        with self.assertRaises(InventoryCostError):
            inventory_cost(request(events))

    def test_unknown_pool_and_cross_currency_rejected(self):
        events = [receipt('r', 1, quantity=2, total_cost=2),
                  issue('i1', 2, 1, 'expA', sku='S', currency='EUR')]
        with self.assertRaises(InventoryCostError):
            inventory_cost(request(events))

    def test_intervals_never_overlap_or_lose_units(self):
        events = [
            receipt('r', 1, quantity=8, total_cost=80),
            issue('i1', 2, 5, 'expA'),
            ret('rt1', 3, 'i1', 2),
            issue('i2', 4, 3, 'expB'),
            ret('rt2', 5, 'i2', 1),
            ret('rt3', 6, 'i1', 1),
        ]
        report = inventory_cost(request(events))
        batch = report['final_positions']['receipts'][0]
        covered = list(batch['available_intervals'])
        for p in report['final_positions']['issues']:
            covered.extend(p['unreturned_intervals'])
        covered = [(c['start'], c['end']) for c in covered]
        covered.sort()
        # Reconstruct [0,8) exactly once.
        cursor = 0
        for start, end in covered:
            self.assertEqual(start, cursor)
            cursor = end
        self.assertEqual(cursor, 8)


# ---------------------------------------------------------------------------
# Revaluation ownership and the joint acceptance narrative
# ---------------------------------------------------------------------------

class RevaluationTests(unittest.TestCase):
    def _joint_events(self):
        return [
            receipt('r1', 1, quantity=3, total_cost=5),
            issue('iA', 2, 2, 'expA'),
            ret('rt1', 3, 'iA', 1),
            issue('iB', 4, 1, 'expB'),
            reprice('rp1', 5, 'r1', 7),
        ]

    def test_q3_c5_to_c7_owner_adjustments(self):
        report = inventory_cost(request(self._joint_events()))
        rev = report['revaluations'][0]
        diffs = {(a['owner'], a['start'], a['end']): a['difference']
                 for a in rev['allocations']}
        # A holds unit 0 (1->2, +1); B holds unit 1 (2->2, 0);
        # inventory holds unit 2 (2->3, +1).
        self.assertEqual(diffs[('iA', 0, 1)], 1)
        self.assertEqual(diffs[('iB', 1, 2)], 0)
        self.assertEqual(diffs[('inventory', 2, 3)], 1)
        self.assertEqual(sum(diffs.values()), 2)
        self.assertEqual(rev['total_change'], 2)

    def test_joint_balances_after_first_reprice(self):
        report = inventory_cost(request(self._joint_events()))
        rows = tb_map(report)
        self.assertEqual(rows['expA']['net_debit'], 2)
        self.assertEqual(rows['expB']['net_debit'], 2)
        self.assertEqual(rows['inv']['net_debit'], 3)
        self.assertEqual(rows['ap']['net_credit'], 7)

    def test_reprice_only_touches_current_owner_not_original_department(self):
        # A returns unit 1 which B then holds; the catch-up difference on that
        # unit must hit B (here zero) and never A or inventory for that unit.
        report = inventory_cost(request(self._joint_events()))
        unit1 = next(a for a in report['revaluations'][0]['allocations']
                     if a['start'] == 1 and a['end'] == 2)
        self.assertEqual(unit1['owner'], 'iB')
        self.assertEqual(unit1['account'], 'expB')
        # A's revaluation lines mention only expA, B's only expB.
        rp_lines = entry_map(report)['rp1']['lines']
        for line in rp_lines:
            if line['account'] == 'expA':
                attr = report['cost_attributions'][line['attribution']]
                self.assertEqual(attr['owner'], 'iA')
            if line['account'] == 'expB':
                attr = report['cost_attributions'][line['attribution']]
                self.assertEqual(attr['owner'], 'iB')

    def test_different_expense_accounts_adjusted_separately(self):
        events = [
            receipt('r', 1, quantity=4, total_cost=4),
            issue('iA', 2, 2, 'expA'),
            issue('iB', 3, 2, 'expB'),
            reprice('rp', 4, 'r', 8),
        ]
        report = inventory_cost(request(events))
        rp = entry_map(report)['rp']
        accounts = [line['account'] for line in rp['lines']]
        self.assertIn('expA', accounts)
        self.assertIn('expB', accounts)
        rows = tb_map(report)
        # Each department's two units double in cost independently.
        self.assertEqual(rows['expA']['net_debit'], 4)
        self.assertEqual(rows['expB']['net_debit'], 4)
        # No netting across issues: four distinct attributions.
        rp_attributions = {line['attribution'] for line in rp['lines']}
        self.assertEqual(len(rp_attributions), 2)

    def test_markdown_then_return_at_new_cost(self):
        events = self._joint_events() + [
            reprice('rp2', 6, 'r1', 4),
            ret('rt2', 7, 'iA', 1),
        ]
        report = inventory_cost(request(events))
        rows = tb_map(report)
        self.assertEqual(rows['expA']['net_debit'], 0)
        self.assertEqual(rows['expB']['net_debit'], 1)
        self.assertEqual(rows['inv']['net_debit'], 3)
        self.assertEqual(rows['ap']['net_credit'], 4)
        batch = report['final_positions']['receipts'][0]
        self.assertEqual(batch['available_quantity'], 2)
        self.assertEqual(batch['available_intervals'], [
            {'receipt': 'r1', 'start': 0, 'end': 1},
            {'receipt': 'r1', 'start': 2, 'end': 3},
        ])
        # The return reverses A's expense at the NEW cost of unit 0 under C=4.
        rt2 = entry_map(report)['rt2']
        self.assertEqual(
            [(l['account'], l['side'], l['amount']) for l in rt2['lines']],
            [('inv', 'debit', 1), ('expA', 'credit', 1)])

    def test_reprice_to_zero_writes_reversal_only(self):
        events = [
            receipt('r', 1, quantity=2, total_cost=10),
            issue('i1', 2, 1, 'expA'),
            reprice('rp', 3, 'r', 0),
        ]
        report = inventory_cost(request(events))
        rows = tb_map(report)
        self.assertEqual(rows['ap']['net_credit'], 0)
        self.assertEqual(rows['expA']['net_debit'], 0)
        self.assertEqual(rows['inv']['net_debit'], 0)
        rp = entry_map(report)['rp']
        # Negative revision: debit payable, credit each current owner.
        self.assertEqual(
            sorted((l['account'], l['side']) for l in rp['lines']),
            [('ap', 'debit'), ('ap', 'debit'),
             ('expA', 'credit'), ('inv', 'credit')])

    def test_reprice_to_same_value_has_no_lines_but_keeps_evidence(self):
        events = [receipt('r', 1, quantity=2, total_cost=5),
                  reprice('rp', 2, 'r', 5)]
        report = inventory_cost(request(events))
        rp = entry_map(report)['rp']
        self.assertEqual(rp['lines'], [])
        self.assertEqual(rp['debit'], 0)
        self.assertEqual(rp['credit'], 0)
        self.assertTrue(report['revaluations'][0]['allocations'])

    def test_reprice_difference_sum_equals_batch_cost_change(self):
        events = [
            receipt('r', 1, quantity=7, total_cost=101),
            issue('i1', 2, 3, 'expA'),
            issue('i2', 3, 2, 'expB'),
            reprice('rp1', 4, 'r', 50),
            reprice('rp2', 5, 'r', 222),
            reprice('rp3', 6, 'r', 0),
        ]
        report = inventory_cost(request(events))
        for rev, change in zip(report['revaluations'],
                               [50 - 101, 222 - 50, -222]):
            self.assertEqual(
                sum(a['difference'] for a in rev['allocations']), change)
            self.assertEqual(rev['total_change'], change)
        self.assertTrue(report['reconciliations']['all_balanced'])

    def test_reprice_does_not_move_physical_units(self):
        events = self._joint_events() + [reprice('rp2', 6, 'r1', 99)]
        before = inventory_cost(request(self._joint_events()))
        after = inventory_cost(request(events))
        self.assertEqual(before['physical_trace'], after['physical_trace'])

    def test_reprice_after_full_return_only_touches_inventory(self):
        events = [
            receipt('r', 1, quantity=2, total_cost=10),
            issue('i', 2, 2, 'expA'),
            ret('rt', 3, 'i', 2),
            reprice('rp', 4, 'r', 4),
        ]
        report = inventory_cost(request(events))
        rows = tb_map(report)
        # The department was fully credited at the old cost; the markdown now
        # lands entirely on inventory (the only current owner).
        self.assertEqual(rows['expA']['net_debit'], 0)
        self.assertEqual(rows['inv']['net_debit'], 4)
        self.assertEqual(rows['ap']['net_credit'], 4)
        rev = report['revaluations'][0]
        self.assertTrue(all(a['owner_kind'] == 'inventory'
                            for a in rev['allocations']))
        self.assertEqual(sum(a['difference']
                             for a in rev['allocations']), -6)
        rp = entry_map(report)['rp']
        self.assertEqual(rp['debit'], rp['credit'])

    def test_repeated_reprices_chain_and_settle_to_zero(self):
        events = [
            receipt('r', 1, quantity=4, total_cost=8),
            reprice('rp1', 2, 'r', 16),
            issue('i', 3, 1, 'expA'),
            reprice('rp2', 4, 'r', 0),
        ]
        report = inventory_cost(request(events))
        rows = tb_map(report)
        self.assertEqual(rows['ap']['net_credit'], 0)
        self.assertEqual(rows['inv']['net_debit'], 0)
        self.assertEqual(rows['expA']['net_debit'], 0)
        for entry in report['entries']:
            self.assertEqual(entry['debit'], entry['credit'])
        self.assertTrue(report['reconciliations']['all_balanced'])
        # Each reprice keeps its own auditable difference record.
        changes = [rev['total_change'] for rev in report['revaluations']]
        self.assertEqual(changes, [8, -16])


# ---------------------------------------------------------------------------
# Postings, attribution linkage and trial balance semantics
# ---------------------------------------------------------------------------

class AccountingTests(unittest.TestCase):
    def test_every_event_debit_equals_credit(self):
        events = [
            receipt('r', 1, quantity=5, total_cost=17),
            issue('i1', 2, 3, 'expA'),
            ret('rt1', 3, 'i1', 1),
            issue('i2', 4, 2, 'expB'),
            reprice('rp1', 5, 'r', 20),
            reprice('rp2', 6, 'r', 4),
        ]
        report = inventory_cost(request(events))
        for entry in report['entries']:
            self.assertEqual(entry['debit'], entry['credit'], entry)
        block = report['trial_balance']['currencies'][0]
        self.assertEqual(block['total_debits'], block['total_credits'])
        self.assertEqual(block['net_debits'], block['net_credits'])

    def test_every_line_traces_to_its_attribution_and_vice_versa(self):
        events = [
            receipt('r', 1, quantity=4, total_cost=9),
            issue('i1', 2, 2, 'expA'),
            ret('rt', 3, 'i1', 1),
            reprice('rp', 4, 'r', 13),
        ]
        report = inventory_cost(request(events))
        attributions = report['cost_attributions']
        # Every posting line points at a real attribution with matching
        # event, amount magnitude and currency.
        for entry in report['entries']:
            for line in entry['lines']:
                attr = attributions[line['attribution']]
                self.assertEqual(attr['event'], entry['entry'])
                self.assertEqual(attr['amount'], line['amount'])
                self.assertEqual(attr['currency'], entry['currency'])
                self.assertGreater(line['amount'], 0)
        # Every non-zero attribution has at least one line; zero-cost flows
        # keep evidence but must not produce zero-amount lines.
        linked = {line['attribution']
                  for entry in report['entries'] for line in entry['lines']}
        for index, attr in enumerate(attributions):
            if attr['amount'] == 0:
                self.assertNotIn(index, linked)
            else:
                self.assertIn(index, linked)
        self.assertFalse(any(line['amount'] == 0
                             for entry in report['entries']
                             for line in entry['lines']))

    def test_zero_cost_flow_keeps_physical_evidence_without_lines(self):
        events = [
            receipt('r', 1, quantity=3, total_cost=0),
            issue('i1', 2, 2, 'expA'),
            ret('rt', 3, 'i1', 1),
        ]
        report = inventory_cost(request(events))
        self.assertEqual(entry_map(report)['r']['lines'], [])
        self.assertEqual(entry_map(report)['i1']['lines'], [])
        self.assertEqual(entry_map(report)['rt']['lines'], [])
        self.assertEqual(trace_map(report)['i1']['allocation'],
                         [{'receipt': 'r', 'start': 0, 'end': 2}])

    def test_trial_balance_lists_every_defined_account_including_zeros(self):
        accounts = ACCOUNTS + [account('dormant', 'credit')]
        report = inventory_cost(request(
            [receipt('r', 1, quantity=1, total_cost=5)],
            accounts=accounts))
        rows = tb_map(report)
        self.assertEqual(set(rows), {a['id'] for a in accounts})
        self.assertEqual(rows['dormant']['debit'], 0)
        self.assertEqual(rows['dormant']['credit'], 0)
        self.assertEqual(rows['dormant']['normal_balance'], 0)

    def test_normal_balance_direction_semantics_reused(self):
        # The same ledger semantics as v1: debit-normal accounts carry the
        # net debit, credit-normal accounts the negated net debit; fully
        # reversed flows settle back to zero rather than leaving a residue.
        report = inventory_cost(request([
            receipt('r', 1, quantity=1, total_cost=5),
            issue('i1', 2, 1, 'expA'),
            ret('rt', 3, 'i1', 1),       # credits the expense back out
            reprice('rp', 4, 'r', 0),    # reverses the remaining payable
        ]))
        rows = tb_map(report)
        self.assertEqual(rows['inv']['normal_balance'], 0)
        self.assertEqual(rows['ap']['normal_balance'], 0)
        self.assertEqual(rows['expA']['normal_balance'], 0)
        positive = inventory_cost(request(
            [receipt('r', 1, quantity=1, total_cost=5)]))
        positive_rows = tb_map(positive)
        self.assertEqual(positive_rows['inv']['normal_balance'], 5)
        self.assertEqual(positive_rows['ap']['normal_balance'], 5)
        self.assertEqual(positive_rows['inv']['net_debit'], 5)
        self.assertEqual(positive_rows['ap']['net_credit'], 5)

    def test_reconciliation_identities(self):
        events = [
            receipt('r1', 1, quantity=3, total_cost=5),
            receipt('r2', 2, quantity=2, total_cost=9),
            issue('i1', 3, 3, 'expA'),
            ret('rt', 4, 'i1', 1),
            issue('i2', 5, 2, 'expB'),
            reprice('rp', 6, 'r1', 7),
        ]
        report = inventory_cost(request(events))
        rec = report['reconciliations']
        self.assertTrue(rec['all_balanced'])
        # 1. Per-batch current cost = inventory valuation + issue costs.
        for check in rec['batch_cost_partitions']:
            self.assertEqual(
                check['inventory_cost'] + check['issued_unreturned_cost'],
                check['total_cost'])
        # 2. Inventory TB figure equals mapped valuation totals.
        for check in rec['inventory_valuations']:
            self.assertEqual(check['expected_debit'],
                             check['trial_net_debit'])
        # 3. Expense TB figure equals unreturned issue cost totals.
        for check in rec['expense_balances']:
            self.assertEqual(check['expected_debit'],
                             check['trial_net_debit'])
        # 4. Payable TB credit equals current batch total costs.
        for check in rec['payable_balances']:
            self.assertEqual(check['expected_credit'],
                             check['trial_net_credit'])

    def test_shared_inventory_account_aggregates_pool_valuations(self):
        stocks = [stock('S', 'USD', 'inv', 'ap'),
                  stock('T', 'USD', 'inv', 'ap')]
        events = [
            receipt('r1', 1, sku='S', quantity=2, total_cost=4),
            receipt('r2', 2, sku='T', quantity=3, total_cost=9),
        ]
        report = inventory_cost(request(events, stocks=stocks))
        rows = tb_map(report)
        self.assertEqual(rows['inv']['net_debit'], 13)
        self.assertEqual(rows['ap']['net_credit'], 13)

    def test_generated_amounts_may_exceed_legacy_entry_cap(self):
        # The v1 journal cap (10**12 per line) is untouched, but inventory
        # postings legitimately carry costs up to 10**18.
        big = 10 ** 18
        report = inventory_cost(request(
            [receipt('r', 1, quantity=10 ** 12, total_cost=big)]))
        self.assertEqual(tb_map(report)['inv']['net_debit'], big)
        with self.assertRaises(ValueError):
            parse_journal({
                'accounts': [{'id': 'a', 'normal': 'debit'},
                             {'id': 'b', 'normal': 'credit'}],
                'entries': [{'id': 'e', 'currency': 'USD', 'lines': [
                    {'account': 'a', 'side': 'debit',
                     'amount': 10 ** 12 + 1},
                    {'account': 'b', 'side': 'credit',
                     'amount': 10 ** 12 + 1}]}]})


# ---------------------------------------------------------------------------
# Scaling: 10**12 quantities, interval-space complexity only
# ---------------------------------------------------------------------------

class ScaleTests(unittest.TestCase):
    def test_ten_to_twelve_quantity_full_lifecycle(self):
        q = QUANTITY_MAX
        c = COST_MAX
        events = [
            receipt('r', 1, quantity=q, total_cost=c),
            issue('i1', 2, q // 2, 'expA'),
            ret('rt', 3, 'i1', 10 ** 6),
            reprice('rp', 4, 'r', c - 987654321),
            issue('i2', 5, 10 ** 6, 'expB'),
        ]
        report = inventory_cost(request(events))
        self.assertTrue(report['reconciliations']['all_balanced'])
        batch = report['final_positions']['receipts'][0]
        # i1 leaves q/2 in the pool; the returned million is fully re-issued
        # by i2, so the pool settles back to exactly q/2 units.
        self.assertEqual(batch['available_quantity'], q // 2)
        # Interval count stays tiny regardless of the quantity.
        self.assertLessEqual(len(batch['available_intervals']), 3)
        issues = {p['issue']: p for p in
                  report['final_positions']['issues']}
        self.assertEqual(issues['i2']['unreturned_quantity'], 10 ** 6)

    def test_event_limits_are_enforced(self):
        too_many_stocks = request(
            [receipt('r', 1, quantity=1, total_cost=1)],
            stocks=[stock('S%04d' % i) for i in range(STOCK_LIMIT + 1)])
        with self.assertRaises(ValueError):
            inventory_cost(too_many_stocks)
        too_many_events = request([
            receipt('r%04d' % i, i, quantity=1, total_cost=1)
            for i in range(EVENT_LIMIT + 1)])
        with self.assertRaises(ValueError):
            inventory_cost(too_many_events)


# ---------------------------------------------------------------------------
# Strict validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def _base(self):
        # Fresh copy per test: the mutating cases must never leak into each
        # other (the module-level builders would otherwise share list state).
        return copy.deepcopy(request([
            receipt('r', 1, quantity=2, total_cost=5),
            issue('i1', 2, 1, 'expA'),
            ret('rt', 3, 'i1', 1),
            reprice('rp', 4, 'r', 6),
        ]))

    def assert_invalid(self, data):
        with self.assertRaises((ValueError, TypeError)):
            inventory_cost(data)

    def test_top_level_fields_exact(self):
        data = self._base()
        data['unexpected'] = 1
        self.assert_invalid(data)
        del data['events']
        self.assert_invalid(data)

    def test_booleans_rejected_everywhere(self):
        for path, value in (
            (('events', 0, 'quantity'), True),
            (('events', 0, 'total_cost'), False),
            (('events', 1, 'quantity'), True),
            (('events', 2, 'quantity'), True),
            (('events', 3, 'total_cost'), False),
            (('events', 0, 'seq'), True),
        ):
            data = self._base()
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.assert_invalid(data)

    def test_float_rejected(self):
        data = self._base()
        data['events'][0]['quantity'] = 2.0
        self.assert_invalid(data)

    def test_unknown_event_field_and_type(self):
        data = self._base()
        data['events'][0]['bogus'] = 1
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['type'] = 'teleport'
        self.assert_invalid(data)

    def test_duplicate_event_id_and_seq(self):
        data = self._base()
        data['events'][1]['id'] = 'r'
        self.assert_invalid(data)
        data = self._base()
        data['events'][1]['seq'] = 1
        self.assert_invalid(data)

    def test_identifier_rules(self):
        data = self._base()
        data['events'][0]['id'] = ' blank'
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['id'] = 123
        self.assert_invalid(data)

    def test_numeric_bounds(self):
        data = self._base()
        data['events'][0]['quantity'] = 0
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['quantity'] = QUANTITY_MAX + 1
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['total_cost'] = COST_MAX + 1
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['seq'] = 10 ** 9 + 1
        self.assert_invalid(data)
        data = self._base()
        data['events'][0]['seq'] = -1
        self.assert_invalid(data)

    def test_future_references_rejected(self):
        # Return referencing an event later in seq order.
        data = request([
            receipt('r', 1, quantity=2, total_cost=2),
            ret('rt', 2, 'future_issue', 1),
            issue('future_issue', 3, 1, 'expA'),
        ])
        self.assert_invalid(data)
        # Reprice referencing a future receipt.
        data = request([
            reprice('rp', 1, 'future_receipt', 5),
            receipt('future_receipt', 2, quantity=1, total_cost=1),
        ])
        self.assert_invalid(data)

    def test_return_references_issue_not_receipt(self):
        data = request([
            receipt('r', 1, quantity=2, total_cost=2),
            ret('rt', 2, 'r', 1),
        ])
        self.assert_invalid(data)

    def test_reprice_references_receipt_not_issue(self):
        data = request([
            receipt('r', 1, quantity=2, total_cost=2),
            issue('i1', 2, 1, 'expA'),
            reprice('rp', 3, 'i1', 5),
        ])
        self.assert_invalid(data)

    def test_expense_account_family_rules(self):
        for bad_account in ('inv', 'ap'):
            data = request([receipt('r', 1, quantity=1, total_cost=1),
                            issue('i1', 2, 1, bad_account)])
            self.assert_invalid(data)
        data = request([receipt('r', 1, quantity=1, total_cost=1),
                        issue('i1', 2, 1, 'ghost')])
        self.assert_invalid(data)

    def test_inventory_and_payable_accounts_must_be_disjoint(self):
        data = request(
            [receipt('r', 1, quantity=1, total_cost=1)],
            stocks=[stock(inventory_account='inv', payable_account='inv')])
        self.assert_invalid(data)

    def test_duplicate_stock_pool_rejected(self):
        data = request([], stocks=[stock(), stock()])
        self.assert_invalid(data)

    def test_unknown_stock_account_rejected(self):
        data = request([], stocks=[stock(inventory_account='ghost')])
        self.assert_invalid(data)

    def test_account_rules_match_v1(self):
        data = self._base()
        data['accounts'][0]['normal'] = 'sideways'
        self.assert_invalid(data)
        data = self._base()
        data['accounts'].append(dict(data['accounts'][0]))
        self.assert_invalid(data)

    def test_empty_stocks_and_events_is_legal(self):
        report = inventory_cost(request([], stocks=[]))
        self.assertEqual(report['statistics']['event_count'], 0)
        # With no pools there is no currency to seed; the TB is simply empty.
        self.assertEqual(report['trial_balance']['currencies'], [])
        # A pool with no events still seeds its currency and every account.
        with_pool = inventory_cost(request([], stocks=[stock()]))
        block = with_pool['trial_balance']['currencies'][0]
        self.assertEqual(block['total_debits'], 0)
        self.assertEqual(len(block['accounts']), len(ACCOUNTS))

    def test_sha256_echoed(self):
        report = inventory_cost(self._base(), 'f' * 64)
        self.assertEqual(report['source_sha256'], 'f' * 64)
        self.assertIsNone(inventory_cost(self._base())['source_sha256'])


# ---------------------------------------------------------------------------
# Input arrangement independence
# ---------------------------------------------------------------------------

class OrderIndependenceTests(unittest.TestCase):
    def test_report_independent_of_input_arrangement(self):
        import random
        data = request([
            receipt('r1', 3, quantity=3, total_cost=5),
            receipt('r2', 1, quantity=2, total_cost=8),
            issue('i1', 5, 3, 'expA'),
            ret('rt', 6, 'i1', 1),
            issue('i2', 7, 2, 'expB'),
            reprice('rp', 9, 'r1', 7),
        ], stocks=[stock('S'), stock('T', 'EUR', 'inv', 'ap')],
            accounts=ACCOUNTS)
        canonical = json.dumps(inventory_cost(copy.deepcopy(data)),
                               sort_keys=True)
        rng = random.Random(99173)

        def shuffle(obj):
            if isinstance(obj, dict):
                items = list(obj.items())
                rng.shuffle(items)
                return {k: shuffle(v) for k, v in items}
            if isinstance(obj, list):
                new = [shuffle(v) for v in obj]
                rng.shuffle(new)
                return new
            return obj

        for _ in range(8):
            self.assertEqual(
                json.dumps(inventory_cost(shuffle(copy.deepcopy(data))),
                           sort_keys=True),
                canonical)


# ---------------------------------------------------------------------------
# Real CLI and safe publication
# ---------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    def _call(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'journal_check.cli', 'inventory-cost',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)

    def test_cli_success_sha256_and_fixture_totals(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'report.json'
            result = self._call(FIXTURE, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
            self.assertEqual(report['source_sha256'], digest)
            rows = tb_map(report)
            self.assertEqual(rows['trade_payables']['net_credit'], 20)
            self.assertEqual(rows['dept_a_expense']['net_debit'], 8)
            self.assertTrue(report['reconciliations']['all_balanced'])

    def test_run_file_interface(self):
        report = run_file(FIXTURE)
        digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        self.assertEqual(report['source_sha256'], digest)
        self.assertEqual(report['schema_version'], 1)

    def test_duplicate_json_key_rejected_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'dup.json'
            source.write_text('{"accounts":[],"accounts":[],"stocks":[],"events":[]}')
            out = Path(td) / 'out.json'
            out.write_text('PRIOR')
            result = self._call(source, out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('duplicate JSON key', result.stderr)
            self.assertEqual(out.read_text(), 'PRIOR')

    def test_failure_exits_two_and_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'bad.json'
            out = Path(td) / 'out.json'
            out.write_text('prior report')
            for body in ('{}', 'NaN',
                         json.dumps(request([
                             receipt('r', 1, quantity=1, total_cost=1),
                             issue('i1', 2, 2, 'expA')]))):
                source.write_text(body)
                result = self._call(source, out)
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr.strip(), '')
                self.assertEqual(out.read_text(), 'prior report')

    def test_same_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'in.json'
            source.write_bytes(FIXTURE.read_bytes())
            result = self._call(source, source)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), FIXTURE.read_bytes())

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'target.json'
            link = Path(td) / 'link.json'
            link.symlink_to(target)
            result = self._call(FIXTURE, link)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_eight_megabyte_limit(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'huge.json'
            source.write_text('{"accounts":[],"stocks":[],"events":[],'
                              '"pad": "' + 'x' * (8 * 10 ** 6) + '"}')
            out = Path(td) / 'out.json'
            result = self._call(source, out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)

    def test_legacy_v1_command_and_other_workbenches_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'v1.json'
            result = subprocess.run(
                [sys.executable, '-m', 'journal_check.cli',
                 '--source', str(ROOT / 'fixtures' / 'input.json'),
                 '--output', str(out)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            v1 = json.loads(out.read_text())
            self.assertEqual(v1['schema_version'], 1)
            block = v1['result']['currencies'][0]
            accounts = {a['account']: a for a in block['accounts']}
            self.assertEqual(accounts['cash']['normal_balance'], 700)
            # Translate subcommand still parses and runs independently.
            t_out = Path(td) / 'translate.json'
            t_result = subprocess.run(
                [sys.executable, '-m', 'journal_check.cli', 'translate',
                 '--source', str(ROOT / 'fixtures' /
                                 'translate_request.json'),
                 '--output', str(t_out)],
                capture_output=True, text=True)
            self.assertEqual(t_result.returncode, 0, t_result.stderr)


if __name__ == '__main__':
    unittest.main()
