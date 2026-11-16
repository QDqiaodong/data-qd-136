"""Tests for the local period close-chain workbench.

Covers the exact largest-remainder allocation (including the +5/-5 and
order-independence acceptance cases), full request/period validation,
per-currency debit/credit conservation and retained-earnings closes,
rework propagation from a revised period through the whole chain tail,
immutable generation persistence, hash-verified historical reads, fault
injection around the publication point, cross-process publish races and the
real ``close-chain`` CLI.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from journal_check.closechain import ConflictError, CorruptionError, LimitError
from journal_check.closechain import NotFoundError, ValidationError
from journal_check.closechain import engine, open_store
from journal_check.closechain.store import (
    FAULT_AFTER_HEAD_SWITCH,
    FAULT_BEFORE_HEAD_SWITCH,
    FAULT_RESULT_WRITE,
    FAULT_SOURCE_WRITE,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def acc(account_id, normal):
    return {'id': account_id, 'normal': normal}


def line(account, side, amount):
    return {'account': account, 'side': side, 'amount': amount}


def entry(entry_id, lines, currency='USD'):
    return {'id': entry_id, 'currency': currency, 'lines': lines}


def role(account, kind):
    return {'account': account, 'kind': kind}


def check(currency, account, amount):
    return {'currency': currency, 'account': account, 'amount': amount}


def target(account, weight=1):
    return {'account': account, 'weight': weight}


def standard_accounts():
    return [acc('cash', 'debit'), acc('retained', 'credit'),
            acc('revenue', 'credit'), acc('expense', 'debit')]


def standard_roles():
    return [role('cash', 'carry'), role('retained', 'carry'),
            role('revenue', 'result'), role('expense', 'result')]


def revenue_expense_journal():
    # Revenue credited 100, expense debited 30; cash ends at debit 70.
    return {
        'accounts': standard_accounts(),
        'entries': [
            entry('sale', [line('cash', 'debit', 100),
                           line('revenue', 'credit', 100)]),
            entry('cost', [line('expense', 'debit', 30),
                           line('cash', 'credit', 30)]),
        ],
    }


def period_one(period_id='A', journal=None, opening_zero_currencies=('USD',)):
    journal = journal or revenue_expense_journal()
    checks = [check(currency, account, 0)
              for currency in opening_zero_currencies
              for account in ('cash', 'retained')]
    return {
        'id': period_id,
        'journal': journal,
        'roles': standard_roles(),
        'retained': 'retained',
        'incoming': [],
        'opening_checks': checks,
    }


def revised_period_one(period_id='A'):
    # Same account set and roles as period_one, but the expense is 40: cash
    # closes at 60 (was 70) while retained closes at -60 (was -70). The
    # debit/credit conservation still holds; only the carry distribution
    # across the same carry accounts changes.
    journal = {
        'accounts': standard_accounts(),
        'entries': [
            entry('sale', [line('cash', 'debit', 100),
                           line('revenue', 'credit', 100)]),
            entry('cost', [line('expense', 'debit', 40),
                           line('cash', 'credit', 40)]),
        ],
    }
    return period_one(period_id, journal=journal)


def request(chain_id, expected_head, start, periods):
    return {'chain_id': chain_id, 'expected_head': expected_head,
            'start': start, 'periods': periods}


def result_index(period_result):
    return {block['currency']: {row['account']: row
                                for row in block['accounts']}
            for block in period_result['currencies']}


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

class AllocateTests(unittest.TestCase):
    def test_acceptance_plus_and_minus_five(self):
        positive, _ = engine.allocate(5, [('a', 1), ('b', 1), ('c', 1)])
        negative, _ = engine.allocate(-5, [('a', 1), ('b', 1), ('c', 1)])
        self.assertEqual(positive, {'a': 2, 'b': 2, 'c': 1})
        self.assertEqual(negative, {'a': -2, 'b': -2, 'c': -1})

    def test_order_independence(self):
        one, _ = engine.allocate(7, [('a', 1), ('b', 2), ('c', 3)])
        two, _ = engine.allocate(7, [('c', 3), ('a', 1), ('b', 2)])
        self.assertEqual(one, two)
        self.assertEqual(one, {'a': 1, 'b': 2, 'c': 4})
        self.assertEqual(sum(one.values()), 7)

    def test_zero_source_is_listed_and_conserved(self):
        allocations, basis = engine.allocate(
            0, [('a', 2), ('b', 3), ('c', 5)])
        self.assertEqual(allocations, {'a': 0, 'b': 0, 'c': 0})
        self.assertEqual(sum(allocations.values()), 0)
        self.assertEqual([b['remainder_numerator'] for b in basis], [0, 0, 0])

    def test_large_amount_no_float_no_unit_loop(self):
        allocations, _ = engine.allocate(
            10 ** 12, [('a', 1), ('b', 2)])
        self.assertEqual(sum(allocations.values()), 10 ** 12)
        self.assertEqual(allocations,
                         {'a': 333333333333, 'b': 666666666667})

    def test_negative_symmetric_and_per_source_conservation(self):
        for amount in (1, 2, 3, 17, 100, 10 ** 9):
            weights = [('a', 1), ('b', 3), ('c', 7)]
            pos, _ = engine.allocate(amount, weights)
            neg, _ = engine.allocate(-amount, weights)
            self.assertEqual(sum(pos.values()), amount)
            self.assertEqual(sum(neg.values()), -amount)
            self.assertEqual(neg, {k: -v for k, v in pos.items()})

    def test_tie_break_uses_target_id(self):
        allocations, _ = engine.allocate(
            1, [('z', 1), ('a', 1), ('m', 1)])
        self.assertEqual(allocations, {'z': 0, 'a': 1, 'm': 0})

    def test_empty_targets_rejected(self):
        with self.assertRaises(ValidationError):
            engine.allocate(10, [])


# ---------------------------------------------------------------------------
# Request and period validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_top_level_fields_exact(self):
        req = request('c1', None, 1, [period_one()])
        req['unexpected'] = 1
        with self.assertRaises(ValidationError):
            engine.prepare_candidate([], req)

    def test_period_fields_exact(self):
        period = period_one()
        period['extra'] = 1
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_identifier_rules_reused(self):
        period = period_one()
        period['id'] = ' blank '
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_generation_id_must_be_hex64(self):
        with self.assertRaises(ValidationError):
            engine.prepare_candidate(
                [], request('c1', 'not-hex', 1, [period_one()]))

    def test_start_must_be_positive(self):
        with self.assertRaises(ValidationError):
            engine.prepare_candidate([], request('c1', None, 0, [period_one()]))
        with self.assertRaises(ValidationError):
            engine.prepare_candidate(
                [], request('c1', None, True, [period_one()]))

    def test_periods_non_empty_and_bounded(self):
        with self.assertRaises(ValidationError):
            engine.prepare_candidate([], request('c1', None, 1, []))
        many = [period_one('P%02d' % i) for i in range(61)]
        with self.assertRaises(LimitError):
            engine.prepare_candidate([], request('c1', None, 1, many))

    def test_roles_cover_every_account_once(self):
        period = period_one()
        period['roles'] = period['roles'][:-1]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['roles'].append(role('cash', 'result'))
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['roles'][0] = role('unknown', 'carry')
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['roles'][0] = role('cash', 'sideways')
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_retained_must_be_carry_credit(self):
        period = period_one()
        period['retained'] = 'revenue'  # result account
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['retained'] = 'cash'  # carry but normal=debit
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['retained'] = 'missing'
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_journal_rules_are_the_v1_rules(self):
        period = period_one()
        period['journal']['entries'][0]['lines'][0]['amount'] = 999
        with self.assertRaises(ValueError):
            engine.parse_period(period)
        period = period_one()
        period['journal']['entries'][0]['lines'][0]['amount'] = True
        with self.assertRaises(ValueError):
            engine.parse_period(period)

    def test_first_period_incoming_must_be_empty(self):
        period = period_one()
        period['incoming'] = [
            {'account': 'cash', 'targets': [target('cash')]}]
        with self.assertRaises(ValidationError):
            engine.compute_chain(engine.assemble_chain([], [period], 1))

    def test_incoming_weight_bounds_and_target_uniqueness(self):
        period = period_one()
        row = {'account': 'cash', 'targets': [target('cash', 0)]}
        period['incoming'] = [row]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        row = {'account': 'cash',
               'targets': [target('cash', 10 ** 6 + 1)]}
        period['incoming'] = [row]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        row = {'account': 'cash',
               'targets': [target('cash'), target('cash')]}
        period['incoming'] = [row]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        row = {'account': 'cash', 'targets': []}
        period['incoming'] = [row]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_incoming_rejects_result_targets_and_unknown_accounts(self):
        period = period_one()
        period['incoming'] = [
            {'account': 'cash', 'targets': [target('revenue')]}]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period['incoming'] = [
            {'account': 'cash', 'targets': [target('ghost')]}]
        with self.assertRaises(ValidationError):
            engine.parse_period(period)

    def test_check_amount_bounds_and_boolean_ban(self):
        period = period_one()
        period['opening_checks'][0] = check('USD', 'cash', True)
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period = period_one()
        period['opening_checks'][0] = check('USD', 'cash', 10 ** 30 + 1)
        with self.assertRaises(ValidationError):
            engine.parse_period(period)
        period['opening_checks'][0] = check('USD', 'cash', -(10 ** 30))
        engine.parse_period(period)  # boundary is accepted structurally

    def test_check_rejects_result_account(self):
        period = period_one()
        period['opening_checks'][0] = check('USD', 'revenue', 0)
        with self.assertRaises(ValidationError):
            engine.parse_period(period)


# ---------------------------------------------------------------------------
# Opening checks against computed openings
# ---------------------------------------------------------------------------

def second_period(period_id='B', checks=None, incoming=None,
                  accounts=None, roles=None):
    accounts = accounts or [
        acc('a', 'debit'), acc('b', 'debit'), acc('c', 'debit'),
        acc('retained', 'credit'), acc('revenue', 'credit')]
    roles = roles or [role('a', 'carry'), role('b', 'carry'),
                      role('c', 'carry'), role('retained', 'carry'),
                      role('revenue', 'result')]
    incoming = incoming if incoming is not None else [
        {'account': 'cash',
         'targets': [target('a'), target('b'), target('c')]},
        {'account': 'retained', 'targets': [target('retained')]},
    ]
    checks = checks if checks is not None else [
        check('USD', 'a', 24), check('USD', 'b', 23), check('USD', 'c', 23),
        check('USD', 'retained', -70)]
    return {
        'id': period_id,
        'journal': {'accounts': accounts, 'entries': []},
        'roles': roles,
        'retained': 'retained',
        'incoming': incoming,
        'opening_checks': checks,
    }


class OpeningCheckTests(unittest.TestCase):
    def _chain(self, second):
        first = engine.parse_period(period_one())
        return engine.compute_chain(
            engine.assemble_chain([first], [second], 2))

    def test_missing_zero_item_rejected(self):
        period = second_period()
        period['opening_checks'] = [
            c for c in period['opening_checks'] if c['account'] != 'c']
        with self.assertRaises(ValidationError) as ctx:
            self._chain(period)
        self.assertIn('missing', str(ctx.exception))
        self.assertIn('account=c', str(ctx.exception))

    def test_duplicate_key_rejected(self):
        period = second_period()
        period['opening_checks'].append(check('USD', 'a', 24))
        with self.assertRaises(ValidationError) as ctx:
            self._chain(period)
        self.assertIn('duplicate', str(ctx.exception))

    def test_wrong_amount_reports_first_sorted_mismatch(self):
        period = second_period()
        period['opening_checks'] = [
            check('USD', 'a', 24), check('USD', 'b', 99),
            check('USD', 'c', 23), check('USD', 'retained', -70)]
        with self.assertRaises(ValidationError) as ctx:
            self._chain(period)
        message = str(ctx.exception)
        self.assertIn('currency=USD account=b', message)
        self.assertIn('expected 23 but control figure is 99', message)

    def test_double_empty_sets_accepted(self):
        # First period, empty journal: no currencies and therefore an empty
        # check set is the required Cartesian product.
        period = period_one(journal={
            'accounts': standard_accounts(), 'entries': []},
            opening_zero_currencies=())
        period['opening_checks'] = []
        results = engine.compute_chain(
            engine.assemble_chain([], [period], 1))
        self.assertEqual(results[0]['currencies'], [])

    def test_inherited_currency_requires_zero_checks(self):
        first = period_one()
        first_parsed = engine.parse_period(first)
        second = second_period()
        # USD is inherited even though period B has no journal entries; drop
        # its checks and the inherited currency must still be required.
        second['opening_checks'] = []
        with self.assertRaises(ValidationError):
            self._chain(second)


# ---------------------------------------------------------------------------
# Close computation
# ---------------------------------------------------------------------------

class CloseComputationTests(unittest.TestCase):
    def test_acceptance_retained_net_credit_70(self):
        result = engine.compute_chain(
            engine.assemble_chain([], [period_one()], 1))[0]
        block = result['currencies'][0]
        rows = {row['account']: row for row in block['accounts']}
        self.assertEqual(rows['revenue']['closing_after'], 0)
        self.assertEqual(rows['expense']['closing_after'], 0)
        # Debit-positive / credit-negative: retained grows by a credit 70.
        self.assertEqual(rows['retained']['closing_after'], -70)
        self.assertEqual(rows['cash']['closing_after'], 70)
        clearings = {c['account']: c['cleared_amount']
                     for c in block['result_clearings']}
        self.assertEqual(clearings, {'revenue': -100, 'expense': 30})
        self.assertEqual(block['retained_transfer']['transfer_amount'], -70)

    def test_output_carries_full_movement_and_balance_columns(self):
        result = engine.compute_chain(
            engine.assemble_chain([], [period_one()], 1))[0]
        rows = {row['account']: row
                for row in result['currencies'][0]['accounts']}
        cash = rows['cash']
        self.assertEqual(
            (cash['opening'], cash['movement_debit'], cash['movement_credit'],
             cash['closing_before'], cash['closing_after']),
            (0, 100, 30, 70, 70))
        revenue = rows['revenue']
        self.assertEqual(revenue['closing_before'], -100)
        self.assertEqual(revenue['closing_after'], 0)

    def test_abnormal_balance_uses_actual_direction(self):
        # A revenue (normal=credit) with a debit balance must clear as a
        # positive amount and reduce retained accordingly.
        journal = {
            'accounts': standard_accounts(),
            'entries': [
                entry('weird', [line('revenue', 'debit', 40),
                                line('cash', 'credit', 40)]),
            ],
        }
        result = engine.compute_chain(
            engine.assemble_chain([], [period_one(journal=journal)], 1))[0]
        rows = {row['account']: row
                for row in result['currencies'][0]['accounts']}
        self.assertEqual(rows['revenue']['closing_before'], 40)
        self.assertEqual(rows['revenue']['closing_after'], 0)
        self.assertEqual(rows['retained']['closing_after'], 40)

    def test_currencies_never_net_across_each_other(self):
        journal = revenue_expense_journal()
        euro = entry('euro-sale', [line('cash', 'debit', 55),
                                   line('revenue', 'credit', 55)], 'EUR')
        journal['entries'].append(euro)
        result = engine.compute_chain(
            engine.assemble_chain([], [period_one(
                journal=journal, opening_zero_currencies=('EUR', 'USD'))], 1))[0]
        currencies = {block['currency']: block for block in result['currencies']}
        self.assertEqual(set(currencies), {'EUR', 'USD'})
        for block in result['currencies']:
            pre = sum(row['closing_before'] for row in block['accounts'])
            post = sum(row['closing_after'] for row in block['accounts'])
            self.assertEqual(pre, 0)
            self.assertEqual(post, 0)
        rows = result_index(result)
        self.assertEqual(rows['EUR']['retained']['closing_after'], -55)
        self.assertEqual(rows['USD']['retained']['closing_after'], -70)

    def test_incoming_distribution_and_conservation_report(self):
        results = engine.compute_chain(engine.assemble_chain(
            [engine.parse_period(period_one())], [second_period()], 2))
        second = results[1]
        rows = result_index(second)['USD']
        self.assertEqual(
            (rows['a']['opening'], rows['b']['opening'], rows['c']['opening']),
            (24, 23, 23))
        self.assertEqual(rows['retained']['opening'], -70)
        source_row = next(row for row in second['incoming']
                          if row['account'] == 'cash')
        currency_row = source_row['currencies'][0]
        self.assertEqual(currency_row['source_amount'], 70)
        self.assertTrue(currency_row['conserved'])
        allocation = {a['account']: a['amount']
                      for a in currency_row['allocations']}
        self.assertEqual(allocation, {'a': 24, 'b': 23, 'c': 23})
        basis = {b['account']: b for b in currency_row['remainder_basis']}
        self.assertEqual(basis['a']['extra_unit'], 1)
        self.assertEqual(basis['b']['extra_unit'], 0)
        self.assertEqual(basis['c']['extra_unit'], 0)

    def test_negative_carried_balance_distributes_symmetrically(self):
        # retained closes at credit 70 (-70) and maps to itself.
        results = engine.compute_chain(engine.assemble_chain(
            [engine.parse_period(period_one())], [second_period()], 2))
        retained_row = next(row for row in results[1]['incoming']
                            if row['account'] == 'retained')
        self.assertEqual(
            retained_row['currencies'][0]['source_amount'], -70)
        self.assertEqual(
            retained_row['currencies'][0]['allocations'][0]['amount'], -70)

    def test_multi_source_merge_into_one_target(self):
        first = engine.parse_period(period_one())
        incoming = [
            {'account': 'cash', 'targets': [target('a')]},
            {'account': 'retained', 'targets': [target('a')]},
        ]
        second = second_period(incoming=incoming, checks=[
            check('USD', 'a', 0), check('USD', 'b', 0), check('USD', 'c', 0),
            check('USD', 'retained', 0)])
        results = engine.compute_chain(
            engine.assemble_chain([first], [second], 2))
        rows = result_index(results[1])['USD']
        # 70 (cash) + (-70) (retained) merge into a single target.
        self.assertEqual(rows['a']['opening'], 0)


# ---------------------------------------------------------------------------
# Chain assembly and rework semantics
# ---------------------------------------------------------------------------

class ChainAssemblyTests(unittest.TestCase):
    def test_overwrite_keeps_period_id_append_keeps_suffix(self):
        first = engine.parse_period(period_one('A'))
        second = engine.parse_period(second_period('B'))
        candidate = engine.assemble_chain([first, second], [second_period('B')], 2)
        self.assertEqual([p['id'] for p in candidate], ['A', 'B'])
        with self.assertRaises(ConflictError):
            engine.assemble_chain([first, second], [second_period('X')], 2)

    def test_gap_and_duplicate_ids_rejected(self):
        first = engine.parse_period(period_one('A'))
        with self.assertRaises(ValidationError):
            engine.assemble_chain([first], [second_period('C')], 3)
        dup = dict(period_one('A'))
        with self.assertRaises(ValidationError):
            engine.assemble_chain([first], [dup], 2)

    def test_append_only_extends_contiguously(self):
        first = engine.parse_period(period_one('A'))
        second = engine.parse_period(second_period('B'))
        c_incoming = [
            {'account': 'a', 'targets': [target('a')]},
            {'account': 'b', 'targets': [target('b')]},
            {'account': 'c', 'targets': [target('c')]},
            {'account': 'retained', 'targets': [target('retained')]},
        ]
        c_checks = [check('USD', 'a', 24), check('USD', 'b', 23),
                    check('USD', 'c', 23), check('USD', 'retained', -70)]
        third = second_period('C', checks=c_checks, incoming=c_incoming)
        candidate = engine.assemble_chain([first, second], [third], 3)
        self.assertEqual([p['id'] for p in candidate], ['A', 'B', 'C'])

    def test_later_incoming_must_cover_every_prior_carry(self):
        first = engine.parse_period(period_one())
        second = second_period()
        second['incoming'] = second['incoming'][:1]  # retained row dropped
        with self.assertRaises(ValidationError) as ctx:
            engine.compute_chain(
                engine.assemble_chain([first], [second], 2))
        self.assertIn('retained', str(ctx.exception))

    def test_prior_carry_become_result_invalidates_old_incoming(self):
        # Period A revised so 'cash' becomes a result account; period B still
        # names cash as an incoming source, which must now fail.
        revised_a = period_one()
        revised_a['roles'] = [role('cash', 'result'),
                              role('retained', 'carry'),
                              role('revenue', 'result'),
                              role('expense', 'result')]
        # retained remains the only carry; cash result must be cleared too.
        first = engine.parse_period(period_one())
        with self.assertRaises(ValidationError):
            engine.compute_chain(engine.assemble_chain(
                [first], [revised_a, second_period()], 1))


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = open_store(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _apply(self, req, digest=None):
        digest = digest or hashlib.sha256(
            json.dumps(req, sort_keys=True).encode()).hexdigest()
        return self.store.apply(req, digest)

    def build_ab(self):
        r1 = self._apply(request('c1', None, 1, [period_one('A')]))
        head1 = r1['generation_id']
        r2 = self._apply(request('c1', head1, 2, [second_period('B')]))
        return head1, r2['generation_id']


class StoreCrudTests(StoreTest):
    def test_apply_publishes_and_default_show_pins_head(self):
        head1, head2 = self.build_ab()
        self.assertEqual(self.store.head()['generation_id'], head2)
        shown = self.store.show(None, 'B')
        self.assertEqual(shown['generation_id'], head2)
        self.assertEqual(shown['position'], 2)
        self.assertEqual(shown['source']['id'], 'B')
        self.assertEqual(shown['result']['period'], 'B')
        self.assertEqual(len(shown['source_sha256']), 64)

    def test_historical_generation_is_pinned_and_reproducible(self):
        head1, head2 = self.build_ab()
        old = self.store.show(head1, 'A')
        self.assertEqual(old['generation_id'], head1)
        self.assertEqual(old['parent'], None)
        rows = result_index(old['result'])['USD']
        self.assertEqual(rows['retained']['closing_after'], -70)
        # A third generation must not change the pinned historical read.
        c_incoming = [
            {'account': 'a', 'targets': [target('a')]},
            {'account': 'b', 'targets': [target('b')]},
            {'account': 'c', 'targets': [target('c')]},
            {'account': 'retained', 'targets': [target('retained')]},
        ]
        self._apply(request('c1', head2, 3, [second_period(
            'C', checks=[check('USD', 'a', 24), check('USD', 'b', 23),
                         check('USD', 'c', 23),
                         check('USD', 'retained', -70)],
            incoming=c_incoming)]))
        self.assertEqual(
            self.store.show(head1, 'A')['result'], old['result'])

    def test_expected_head_conflict_is_explicit(self):
        head1, _ = self.build_ab()
        c = self._valid_period_c()
        with self.assertRaises(ConflictError):
            self._apply(request('c1', '0' * 64, 3, [c]))
        with self.assertRaises(ConflictError):
            self._apply(request('c1', None, 1, [period_one('A')]))

    def test_chain_id_cannot_change(self):
        head1, _ = self.build_ab()
        with self.assertRaises(ConflictError):
            self._apply(request('other', head1, 3, [self._valid_period_c()]))

    @staticmethod
    def _valid_period_c():
        return second_period(
            'C', checks=[check('USD', 'a', 24), check('USD', 'b', 23),
                         check('USD', 'c', 23),
                         check('USD', 'retained', -70)],
            incoming=[
                {'account': 'a', 'targets': [target('a')]},
                {'account': 'b', 'targets': [target('b')]},
                {'account': 'c', 'targets': [target('c')]},
                {'account': 'retained', 'targets': [target('retained')]},
            ])

    def test_not_found_paths(self):
        with self.assertRaises(NotFoundError):
            self.store.show(None, 'A')
        head1, _ = self.build_ab()
        with self.assertRaises(NotFoundError):
            self.store.show('f' * 64, 'A')
        with self.assertRaises(NotFoundError):
            self.store.show(head1, 'Z')

    def test_rejected_apply_leaves_head_and_history_untouched(self):
        head1, head2 = self.build_ab()
        bad_b = second_period()
        bad_b['opening_checks'][0] = check('USD', 'a', 999)
        with self.assertRaises(ValidationError):
            self._apply(request('c1', head2, 2, [bad_b]))
        self.assertEqual(self.store.head()['generation_id'], head2)
        self.assertEqual(
            self.store.show(head1, 'A')['generation_id'], head1)

    def test_hash_corruption_is_rejected(self):
        head1, head2 = self.build_ab()
        gen_dir = Path(self.tempdir.name) / ('gen-' + head2)
        source_file = next(p for p in gen_dir.iterdir()
                           if p.name.endswith('.source.json') and 'B' in p.name)
        original = source_file.read_bytes()
        source_file.write_bytes(original + b' ')
        with self.assertRaises(CorruptionError):
            self.store.show(head2, 'B')
        source_file.write_bytes(original)
        self.assertEqual(self.store.show(head2, 'B')['period_id'], 'B')

        result_file = next(p for p in gen_dir.iterdir()
                           if p.name.endswith('.result.json') and 'B' in p.name)
        original_result = result_file.read_bytes()
        result_file.write_bytes(original_result + b' ')
        with self.assertRaises(CorruptionError):
            self.store.show(head2, 'B')
        result_file.write_bytes(original_result)
        self.store.show(head2, 'B')

    def test_show_only_opens_the_requested_period_files(self):
        head1, head2 = self.build_ab()
        gen_dir = Path(self.tempdir.name) / ('gen-' + head2)
        # Corrupt an unrelated period file: reading B must not touch A.
        other = next(p for p in gen_dir.iterdir()
                     if p.name.endswith('.source.json') and 'A' in p.name)
        other.write_bytes(b'{broken')
        shown = self.store.show(head2, 'B')
        self.assertEqual(shown['period_id'], 'B')
        with self.assertRaises(CorruptionError):
            self.store.show(head2, 'A')


class ReworkPropagationTests(StoreTest):
    def _three_period_chain(self):
        head_a = self._apply(request('c1', None, 1, [period_one('A')]))[
            'generation_id']
        head_b = self._apply(
            request('c1', head_a, 2, [second_period('B')]))['generation_id']
        c_checks = [check('USD', 'a', 24), check('USD', 'b', 23),
                    check('USD', 'c', 23), check('USD', 'retained', -70)]
        period_c = second_period('C', checks=c_checks, incoming=[
            {'account': 'a', 'targets': [target('a')]},
            {'account': 'b', 'targets': [target('b')]},
            {'account': 'c', 'targets': [target('c')]},
            {'account': 'retained', 'targets': [target('retained')]},
        ])
        head_c = self._apply(
            request('c1', head_b, 3, [period_c]))['generation_id']
        return head_a, head_b, head_c, period_c

    def test_rework_a_recomputes_b_and_c_and_rejects_stale_checks(self):
        head_a, head_b, head_c, period_c = self._three_period_chain()
        # Revise A's journal so its carry distribution changes (cash 70 -> 60,
        # retained -70 -> -60) while both periods stay balanced.
        revised_a = revised_period_one('A')
        head_before = self.store.head()['generation_id']
        with self.assertRaises(ValidationError) as ctx:
            self._apply(request('c1', head_before, 1, [revised_a]))
        # The first recomputed period B carries stale opening checks.
        self.assertIn('opening check mismatch', str(ctx.exception))
        self.assertEqual(self.store.head()['generation_id'], head_before)
        # C is untouched and still readable.
        self.assertEqual(
            self.store.show(head_c, 'C')['generation_id'], head_c)

    def test_joint_rework_with_follow_up_check_fixes_publishes(self):
        head_a, head_b, head_c, period_c = self._three_period_chain()
        revised_a = revised_period_one('A')
        # New openings after the distribution change.
        fixed_b = second_period('B', checks=[
            check('USD', 'a', 20), check('USD', 'b', 20),
            check('USD', 'c', 20), check('USD', 'retained', -60)])
        fixed_c = second_period('C', checks=[
            check('USD', 'a', 20), check('USD', 'b', 20),
            check('USD', 'c', 20), check('USD', 'retained', -60)],
            incoming=[
                {'account': 'a', 'targets': [target('a')]},
                {'account': 'b', 'targets': [target('b')]},
                {'account': 'c', 'targets': [target('c')]},
                {'account': 'retained', 'targets': [target('retained')]},
            ])
        receipt = self._apply(request(
            'c1', head_c, 1, [revised_a, fixed_b, fixed_c]),
            digest='joint-rework-request')
        new_head = receipt['generation_id']
        rows_b = result_index(self.store.show(new_head, 'B')['result'])
        self.assertEqual(rows_b['USD']['a']['opening'], 20)
        rows_c = result_index(self.store.show(new_head, 'C')['result'])
        self.assertEqual(rows_c['USD']['a']['opening'], 20)
        # The old generation still reproduces the original distribution.
        old_b = result_index(self.store.show(head_b, 'B')['result'])
        self.assertEqual(old_b['USD']['a']['opening'], 24)

    def test_weight_only_rework_keeps_total_but_breaks_stale_b_checks(self):
        # Spec joint scenario: A's carry *distribution* changes via incoming
        # weights while the carried total stays 70; B's original checks fail.
        head_a = self._apply(request('c1', None, 1, [period_one('A')]))[
            'generation_id']
        head_b = self._apply(
            request('c1', head_a, 2, [second_period('B')]))['generation_id']
        c_incoming = [
            {'account': 'a', 'targets': [target('a')]},
            {'account': 'b', 'targets': [target('b')]},
            {'account': 'c', 'targets': [target('c')]},
            {'account': 'retained', 'targets': [target('retained')]},
        ]
        c = second_period('C', checks=[
            check('USD', 'a', 24), check('USD', 'b', 23),
            check('USD', 'c', 23), check('USD', 'retained', -70)],
            incoming=c_incoming)
        head_c = self._apply(request('c1', head_b, 3, [c]))['generation_id']

        # Same A source, but B redistributes cash 2:1:1 instead of 1:1:1;
        # the 70 total is unchanged, openings become 35/18/17.
        revised_b = second_period('B', checks=[
            check('USD', 'a', 35), check('USD', 'b', 18),
            check('USD', 'c', 17), check('USD', 'retained', -70)],
            incoming=[
                {'account': 'cash', 'targets': [
                    target('a', 2), target('b', 1), target('c', 1)]},
                {'account': 'retained',
                 'targets': [target('retained')]},
            ])
        # Submitting only the B revision keeps C's stale checks and fails the
        # whole publication; the head and C's stored result do not move.
        with self.assertRaises(ValidationError) as ctx:
            self._apply(request('c1', head_c, 2, [revised_b]))
        self.assertIn('period C', str(ctx.exception))
        self.assertEqual(self.store.head()['generation_id'], head_c)

        # One request fixing B and C together publishes the联动 chain.
        fixed_c = second_period('C', checks=[
            check('USD', 'a', 35), check('USD', 'b', 18),
            check('USD', 'c', 17), check('USD', 'retained', -70)],
            incoming=c_incoming)
        receipt = self._apply(
            request('c1', head_c, 2, [revised_b, fixed_c]),
            digest='weight-only-joint-rework')
        new_head = receipt['generation_id']
        rows_b = result_index(self.store.show(new_head, 'B')['result'])
        self.assertEqual(
            (rows_b['USD']['a']['opening'], rows_b['USD']['b']['opening'],
             rows_b['USD']['c']['opening']), (35, 18, 17))
        rows_c = result_index(self.store.show(new_head, 'C')['result'])
        self.assertEqual(rows_c['USD']['a']['opening'], 35)
        # The old generation still reproduces 24/23/23.
        old_c = result_index(self.store.show(head_c, 'C')['result'])
        self.assertEqual(old_c['USD']['a']['opening'], 24)

    def test_append_single_period(self):
        head_a, head_b = self.build_ab()
        c = second_period('C', checks=[
            check('USD', 'a', 24), check('USD', 'b', 23),
            check('USD', 'c', 23), check('USD', 'retained', -70)],
            incoming=[
                {'account': 'a', 'targets': [target('a')]},
                {'account': 'b', 'targets': [target('b')]},
                {'account': 'c', 'targets': [target('c')]},
                {'account': 'retained', 'targets': [target('retained')]},
            ])
        receipt = self._apply(request('c1', head_b, 3, [c]))
        self.assertEqual(receipt['period_count'], 3)
        self.assertEqual(self.store.show(None, 'C')['period_id'], 'C')


# ---------------------------------------------------------------------------
# Fault injection and cross-process behaviour via the real CLI
# ---------------------------------------------------------------------------

class CliTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.tempdir.name) / 'store'
        self.req_path = Path(self.tempdir.name) / 'request.json'
        self.req_path.write_text(json.dumps(
            request('c1', None, 1, [period_one('A')])))
        self.env = dict(os.environ, PYTHONPATH=str(SRC))

    def tearDown(self):
        self.tempdir.cleanup()

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, '-m', 'journal_check.cli',
             'close-chain', *args],
            capture_output=True, text=True,
            env=env or self.env, cwd=self.tempdir.name)

    def head_id(self):
        return json.loads((self.store_dir / 'head.json').read_text())[
            'generation_id']

    def test_apply_and_show_round_trip(self):
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(self.req_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        generation_id = json.loads(result.stdout)['generation_id']
        shown = self.cli('show', '--store', str(self.store_dir),
                         '--generation', generation_id, '--period', 'A')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        payload = json.loads(shown.stdout)
        self.assertEqual(payload['period_id'], 'A')
        self.assertEqual(
            payload['result']['currencies'][0][
                'retained_transfer']['transfer_amount'], -70)

    def test_business_error_exit_code_2(self):
        bad = Path(self.tempdir.name) / 'bad.json'
        bad.write_text(json.dumps(request('c1', '0' * 64, 1, [period_one()])))
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(bad))
        self.assertEqual(result.returncode, 2)
        self.assertIn('expected_head', result.stderr)

    def test_strict_eight_megabyte_json(self):
        oversized = Path(self.tempdir.name) / 'big.json'
        oversized.write_text('{"x": 1}' + ' ' * (8 * 1024 * 1024))
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(oversized))
        self.assertEqual(result.returncode, 2)
        duplicate = Path(self.tempdir.name) / 'dup.json'
        duplicate.write_text('{"chain_id": "c1", "chain_id": "c1", '
                             '"expected_head": null, "start": 1, '
                             '"periods": []}')
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(duplicate))
        self.assertEqual(result.returncode, 2)

    def test_output_file_is_atomically_published(self):
        out = Path(self.tempdir.name) / 'report.json'
        out.write_text('prior')
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(self.req_path),
                          '--output', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(out.read_text(), 'prior')
        bad = Path(self.tempdir.name) / 'bad.json'
        bad.write_text('{}')
        failed = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(bad), '--output', str(out))
        self.assertEqual(failed.returncode, 2)

    def _publish_first(self):
        result = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(self.req_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.head_id()

    def test_faults_before_publication_keep_old_head_and_clean_up(self):
        first_head = self._publish_first()
        second_req = Path(self.tempdir.name) / 'second.json'
        second_req.write_text(json.dumps(
            request('c1', first_head, 2, [second_period('B')])))
        for fault in (FAULT_SOURCE_WRITE, FAULT_RESULT_WRITE,
                      FAULT_BEFORE_HEAD_SWITCH):
            # Each fault gets an independent process; the fault flag fires
            # once and the store must remain on the complete old chain.
            env = dict(self.env)
            env[fault] = '1'
            failed = self.cli('apply', '--store', str(self.store_dir),
                              '--source', str(second_req), env=env)
            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(self.head_id(), first_head)
            entries = os.listdir(self.store_dir)
            self.assertFalse(
                any(name.startswith('tmp-') for name in entries))
            # Unpublished residue must not block the next legal publish.
            ok = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(second_req))
            self.assertEqual(ok.returncode, 0, ok.stderr)
            first_head = self.head_id()
            third_req = Path(self.tempdir.name) / 'third.json'
            third_req.write_text(json.dumps(request(
                'c1', first_head, 3, [second_period(
                    'C', checks=[check('USD', 'a', 24),
                                 check('USD', 'b', 23),
                                 check('USD', 'c', 23),
                                 check('USD', 'retained', -70)],
                    incoming=[
                        {'account': 'a', 'targets': [target('a')]},
                        {'account': 'b', 'targets': [target('b')]},
                        {'account': 'c', 'targets': [target('c')]},
                        {'account': 'retained',
                         'targets': [target('retained')]}])])))
            second_req = third_req

    def test_fault_after_head_switch_exposes_only_complete_new_chain(self):
        first_head = self._publish_first()
        second_req = Path(self.tempdir.name) / 'second.json'
        second_req.write_text(json.dumps(
            request('c1', first_head, 2, [second_period('B')])))
        env = dict(self.env)
        env[FAULT_AFTER_HEAD_SWITCH] = '1'
        failed = self.cli('apply', '--store', str(self.store_dir),
                          '--source', str(second_req), env=env)
        self.assertEqual(failed.returncode, 2)
        new_head = self.head_id()
        self.assertNotEqual(new_head, first_head)
        shown = self.cli('show', '--store', str(self.store_dir),
                         '--generation', new_head, '--period', 'B')
        self.assertEqual(shown.returncode, 0, shown.stderr)

    def test_stale_head_temp_file_does_not_block_publish(self):
        first_head = self._publish_first()
        # Simulate a crash after writing the temp head but before replace.
        (self.store_dir / 'head.json.new').write_text('{"stale": true}')
        second_req = Path(self.tempdir.name) / 'second.json'
        second_req.write_text(json.dumps(
            request('c1', first_head, 2, [second_period('B')])))
        ok = self.cli('apply', '--store', str(self.store_dir),
                      '--source', str(second_req))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotEqual(self.head_id(), first_head)
        self.assertFalse((self.store_dir / 'head.json.new').exists())

    def test_concurrent_publish_only_one_wins(self):
        first_head = self._publish_first()
        competing = []
        for suffix in ('one', 'two'):
            path = Path(self.tempdir.name) / ('req-%s.json' % suffix)
            period = second_period('B')
            path.write_text(json.dumps(
                request('c1', first_head, 2, [period]),
                sort_keys=True))
            competing.append(path)
        processes = [
            subprocess.Popen(
                [sys.executable, '-m', 'journal_check.cli',
                 'close-chain', 'apply', '--store', str(self.store_dir),
                 '--source', str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.env, cwd=self.tempdir.name)
            for path in competing]
        outcomes = [process.communicate() for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 2])
        loser_stderr = next(
            err.decode() for out, err in outcomes if err)
        self.assertIn('expected_head', loser_stderr)
        # Exactly one complete generation is reachable from the head.
        head = self.head_id()
        shown = self.cli('show', '--store', str(self.store_dir),
                         '--generation', head, '--period', 'B')
        self.assertEqual(shown.returncode, 0, shown.stderr)


# ---------------------------------------------------------------------------
# Extra edge cases
# ---------------------------------------------------------------------------

class EdgeCaseTests(unittest.TestCase):
    def test_exact_sixty_periods_accepted_sixty_one_rejected(self):
        # Build a genuinely linked 60-period chain: every period keeps the
        # same two carry accounts at zero and maps them onto themselves.
        carry_journal = {
            'accounts': [acc('cash', 'debit'), acc('retained', 'credit'),
                         acc('revenue', 'credit')],
            # Self-cancelling entry keeps USD present with zero net movement.
            'entries': [entry('neutral', [
                line('cash', 'debit', 10), line('cash', 'credit', 10)])],
        }
        carry_roles = [role('cash', 'carry'), role('retained', 'carry'),
                       role('revenue', 'result')]
        carry_incoming = [
            {'account': 'cash', 'targets': [target('cash')]},
            {'account': 'retained', 'targets': [target('retained')]},
        ]
        carry_checks = [check('USD', 'cash', 0),
                        check('USD', 'retained', 0)]
        first = engine.parse_period({
            'id': 'P01', 'journal': carry_journal, 'roles': carry_roles,
            'retained': 'retained', 'incoming': [],
            'opening_checks': carry_checks})
        def carry_period(period_id):
            return {
                'id': period_id, 'journal': carry_journal,
                'roles': carry_roles, 'retained': 'retained',
                'incoming': carry_incoming, 'opening_checks': carry_checks}
        rest = [carry_period('P%02d' % i) for i in range(2, 61)]
        chain = engine.assemble_chain([first], rest, 2)
        self.assertEqual(len(chain), 60)
        self.assertEqual(len(engine.compute_chain(chain)), 60)
        with self.assertRaises(LimitError):
            engine.compute_chain(engine.assemble_chain(
                [first], rest + [carry_period('P61')], 2))
        with self.assertRaises(LimitError):
            engine.prepare_candidate(
                [], request('c1', None, 1,
                            [period_one('R%03d' % i) for i in range(61)]))

    def test_request_level_order_independence(self):
        first = engine.parse_period(period_one('A'))
        second = second_period('B')
        shuffled = copy.deepcopy(second)
        shuffled['roles'].reverse()
        shuffled['incoming'].reverse()
        shuffled['incoming'][0]['targets'].reverse()
        shuffled['opening_checks'].reverse()
        one = engine.compute_chain(
            engine.assemble_chain([first], [second], 2))
        two = engine.compute_chain(
            engine.assemble_chain([first], [shuffled], 2))
        self.assertEqual(one, two)

    def test_new_currency_in_later_period_requires_zero_openings(self):
        first = engine.parse_period(period_one())
        # Period B introduces EUR while still inheriting USD.
        second = second_period()
        second['journal'] = {
            'accounts': [
                acc('a', 'debit'), acc('b', 'debit'), acc('c', 'debit'),
                acc('retained', 'credit'), acc('revenue', 'credit')],
            'entries': [entry('euro', [
                line('a', 'debit', 10), line('revenue', 'credit', 10)], 'EUR')],
        }
        # Checks covering only USD miss the EUR Cartesian product.
        with self.assertRaises(ValidationError) as ctx:
            engine.compute_chain(
                engine.assemble_chain([first], [second], 2))
        self.assertIn('currency=EUR', str(ctx.exception))
        second['opening_checks'] += [
            check('EUR', account, 0)
            for account in ('a', 'b', 'c', 'retained')]
        results = engine.compute_chain(
            engine.assemble_chain([first], [second], 2))
        blocks = {b['currency']: b for b in results[1]['currencies']}
        self.assertEqual(set(blocks), {'EUR', 'USD'})
        eur_rows = {r['account']: r for r in blocks['EUR']['accounts']}
        self.assertEqual(eur_rows['a']['opening'], 0)
        self.assertEqual(eur_rows['a']['closing_after'], 10)

    def test_incoming_runs_per_currency_without_cross_netting(self):
        journal = revenue_expense_journal()
        journal['entries'].append(entry(
            'euro', [line('cash', 'debit', 9),
                     line('revenue', 'credit', 9)], 'EUR'))
        first = engine.parse_period(
            period_one(journal=journal,
                       opening_zero_currencies=('EUR', 'USD')))
        second = second_period()
        second['opening_checks'] += [
            check('EUR', 'a', 3), check('EUR', 'b', 3),
            check('EUR', 'c', 3), check('EUR', 'retained', -9)]
        # USD cash = 70 -> 24/23/23; EUR cash = 9 -> 3/3/3 independently.
        results = engine.compute_chain(
            engine.assemble_chain([first], [second], 2))
        incoming = next(r for r in results[1]['incoming']
                        if r['account'] == 'cash')
        per_currency = {row['currency']: row for row in incoming['currencies']}
        usd = {a['account']: a['amount']
               for a in per_currency['USD']['allocations']}
        eur = {a['account']: a['amount']
               for a in per_currency['EUR']['allocations']}
        self.assertEqual(usd, {'a': 24, 'b': 23, 'c': 23})
        self.assertEqual(eur, {'a': 3, 'b': 3, 'c': 3})
        self.assertTrue(
            all(row['conserved'] for row in per_currency.values()))

    def test_check_mismatch_reports_currency_order_before_account(self):
        first = engine.parse_period(period_one())
        second = second_period()
        second['opening_checks'] += [
            check('EUR', 'a', 0), check('EUR', 'b', 0),
            check('EUR', 'c', 0), check('EUR', 'retained', -5)]
        second['journal'] = {
            'accounts': [
                acc('a', 'debit'), acc('b', 'debit'), acc('c', 'debit'),
                acc('retained', 'credit'), acc('revenue', 'credit')],
            'entries': [entry('euro', [
                line('a', 'debit', 5), line('revenue', 'credit', 5)], 'EUR')]}
        # Wrong EUR figure and a wrong USD figure: EUR sorts first.
        second['opening_checks'] = [
            (check('EUR', 'a', 5) if c['currency'] == 'EUR'
             and c['account'] == 'a' else c)
            for c in second['opening_checks']]
        second['opening_checks'] = [
            (check('USD', 'a', 999) if c['currency'] == 'USD'
             and c['account'] == 'a' else c)
            for c in second['opening_checks']]
        with self.assertRaises(ValidationError) as ctx:
            engine.compute_chain(
                engine.assemble_chain([first], [second], 2))
        self.assertIn('currency=EUR account=a', str(ctx.exception))
        self.assertIn('expected 0 but control figure is 5', str(ctx.exception))

    def test_zero_balance_sources_still_listed(self):
        # A carry account closing at zero must still appear in incoming with
        # zero allocations rather than being omitted.
        journal = {
            'accounts': standard_accounts(),
            'entries': [
                entry('wash', [line('cash', 'debit', 10),
                               line('cash', 'credit', 10)]),
                entry('zero-rev', [line('revenue', 'debit', 5),
                                   line('revenue', 'credit', 5)]),
            ],
        }
        first = engine.parse_period(period_one(journal=journal))
        second = second_period(checks=[
            check('USD', 'a', 0), check('USD', 'b', 0),
            check('USD', 'c', 0), check('USD', 'retained', 0)])
        results = engine.compute_chain(
            engine.assemble_chain([first], [second], 2))
        sources = {row['account'] for row in results[1]['incoming']}
        self.assertEqual(sources, {'cash', 'retained'})
        cash_row = next(row for row in results[1]['incoming']
                        if row['account'] == 'cash')
        self.assertEqual(
            {a['amount'] for a in cash_row['currencies'][0]['allocations']},
            {0})


# ---------------------------------------------------------------------------
# Regression: v1 analyze and the legacy command stay untouched
# ---------------------------------------------------------------------------

class LegacyRegressionTests(unittest.TestCase):
    def test_analyze_signature_and_report_unchanged(self):
        from journal_check.model import analyze
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        report = analyze(data)
        block = report['currencies'][0]
        rows = {row['account']: row for row in block['accounts']}
        self.assertEqual(rows['cash']['normal_balance'], 700)
        self.assertEqual(block['total_debits'], block['total_credits'])
        self.assertNotIn('close', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
