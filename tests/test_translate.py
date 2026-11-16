"""Tests for quote-evidence driven multi-currency translation.

Covers per-day quote graph resolution (inconsistent/missing/ready, forward and
reverse walks, parallel quotes, disconnected contradictory components and
exact closed-walk witnesses), exact rational translation with half-to-even
rounding and separate-line traceability, the accounting linkage between quote
status and posting eligibility (including the two-day joint acceptance, quote
expiry to missing and the fix-that-changes-one-day scenario), strict input
validation and the real ``translate`` CLI with safe publication.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from journal_check.translate import run_file, translate
from journal_check.translate.engine import round_half_even
from journal_check.translate.errors import TranslateError
from journal_check.translate.rates import (
    INCONSISTENT,
    MISSING,
    READY,
    resolve_day,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def account(account_id, normal='debit'):
    return {'id': account_id, 'normal': normal}


def line(account, side, amount):
    return {'account': account, 'side': side, 'amount': amount}


def entry(entry_id, currency, lines):
    return {'id': entry_id, 'currency': currency, 'lines': lines}


def quote(qid, base, quoted, numerator, denominator, valid_from, valid_to):
    return {'id': qid, 'base': base, 'quote': quoted,
            'numerator': numerator, 'denominator': denominator,
            'valid_from': valid_from, 'valid_to': valid_to}


def date_row(entry_id, day):
    return {'entry': entry_id, 'date': day}


def balanced_entry(entry_id, currency, debit_account, credit_account, amount,
                   extra_lines=()):
    lines = [line(debit_account, 'debit', amount),
             line(credit_account, 'credit', amount)]
    lines.extend(extra_lines)
    return entry(entry_id, currency, lines)


ACCOUNTS = [account('cash', 'debit'), account('receivable', 'debit'),
            account('sales', 'credit'), account('fx_rounding', 'credit')]


def request(entries, dates, quotes, target='T', rounding_account='fx_rounding',
            accounts=None):
    return {
        'journal': {'accounts': accounts or ACCOUNTS, 'entries': entries},
        'target': target,
        'rounding_account': rounding_account,
        'entry_dates': dates,
        'quotes': quotes,
    }


def q(qid, base, quoted, numerator, denominator):
    """Quote valid over the whole test window."""
    return quote(qid, base, quoted, numerator, denominator,
                 '2026-01-01', '2026-12-31')


def day_graph(quotes, target='T', needed=('A', 'B', 'T')):
    return resolve_day(quotes, target, set(needed))


# ---------------------------------------------------------------------------
# Quote graph resolution
# ---------------------------------------------------------------------------

class QuoteGraphTests(unittest.TestCase):
    def test_contradiction_marks_whole_component_inconsistent(self):
        # A->B = 2, B->T = 3, A->T = 7; the tree predicts 6 but the direct
        # quote says 7, so neither A nor B is trustworthy.
        result = day_graph([q('qAB', 'A', 'B', 2, 1),
                            q('qBT', 'B', 'T', 3, 1),
                            q('qAT', 'A', 'T', 7, 1)])
        self.assertEqual(result['A']['state'], INCONSISTENT)
        self.assertEqual(result['B']['state'], INCONSISTENT)
        self.assertEqual(result['T']['state'], READY)
        witness = result['A']['witness']
        self.assertTrue(witness['closed'])
        product = Fraction(witness['product']['numerator'],
                           witness['product']['denominator'])
        self.assertNotEqual(product, 1)
        # The witness is a continuous closed walk over real active quotes.
        for earlier, later in zip(witness['steps'], witness['steps'][1:]):
            self.assertEqual(earlier['to'], later['from'])
        self.assertEqual(witness['steps'][0]['from'], witness['steps'][-1]['to'])
        # Recompute its product straight from the quoted rates.
        rates = {(x['base'], x['quote']):
                 Fraction(x['numerator'], x['denominator'])
                 for x in (q('qAB', 'A', 'B', 2, 1), q('qBT', 'B', 'T', 3, 1),
                           q('qAT', 'A', 'T', 7, 1))}
        recomputed = Fraction(1)
        for step in witness['steps']:
            if (step['from'], step['to']) in rates:
                self.assertEqual(step['direction'], 'forward')
                recomputed *= rates[(step['from'], step['to'])]
            else:
                self.assertEqual(step['direction'], 'reverse')
                recomputed /= rates[(step['to'], step['from'])]
        self.assertEqual(recomputed, product)

    def test_removing_direct_quote_gives_rate_six(self):
        result = day_graph([q('qAB', 'A', 'B', 2, 1),
                            q('qBT', 'B', 'T', 3, 1)])
        self.assertEqual(result['A']['state'], READY)
        self.assertEqual(result['A']['rate'], {'numerator': 6, 'denominator': 1})
        self.assertEqual([step['quote'] for step in result['A']['path']],
                         ['qAB', 'qBT'])
        self.assertTrue(all(step['direction'] == 'forward'
                            for step in result['A']['path']))

    def test_reverse_quote_walk(self):
        # T->A = 6 means one A unit is worth 1/6 of T.
        result = day_graph([q('qTA', 'T', 'A', 6, 1)], needed=('A', 'T'))
        self.assertEqual(result['A']['state'], READY)
        self.assertEqual(result['A']['rate'], {'numerator': 1, 'denominator': 6})
        self.assertEqual(result['A']['path'][0]['direction'], 'reverse')
        self.assertEqual(result['A']['path'][0]['quote'], 'qTA')

    def test_parallel_consistent_quotes(self):
        result = day_graph([q('p1', 'A', 'T', 2, 1),
                            q('p2', 'A', 'T', 4, 2)])
        self.assertEqual(result['A']['state'], READY)
        self.assertEqual(result['A']['rate'], {'numerator': 2, 'denominator': 1})

    def test_parallel_contradictory_quotes(self):
        result = day_graph([q('p1', 'A', 'T', 2, 1),
                            q('p2', 'A', 'T', 3, 1)])
        self.assertEqual(result['A']['state'], INCONSISTENT)

    def test_missing_reports_reachable_set(self):
        # A-B is a consistent component with no route to T.
        result = day_graph([q('qAB', 'A', 'B', 2, 1)])
        self.assertEqual(result['A']['state'], MISSING)
        self.assertEqual(result['B']['state'], MISSING)
        self.assertEqual(result['A']['reachable_currencies'], ['A', 'B'])

    def test_isolated_currency_is_missing(self):
        result = day_graph([], needed=('A', 'T'))
        self.assertEqual(result['A']['state'], MISSING)
        self.assertEqual(result['A']['reachable_currencies'], ['A'])

    def test_disconnected_contradiction_does_not_pollute_healthy_component(self):
        result = day_graph([
            q('qAT', 'A', 'T', 5, 1),
            quote('x1', 'X', 'Y', 2, 1, '2026-01-01', '2026-12-31'),
            quote('x2', 'Y', 'X', 1, 1, '2026-01-01', '2026-12-31'),
        ], needed=('A', 'T', 'X', 'Y'))
        self.assertEqual(result['A']['state'], READY)
        self.assertEqual(result['T']['state'], READY)
        self.assertEqual(result['X']['state'], INCONSISTENT)
        self.assertEqual(result['Y']['state'], INCONSISTENT)

    def test_target_is_always_ready_even_with_contradictory_quotes(self):
        result = day_graph([q('t1', 'T', 'X', 2, 1),
                            q('t2', 'T', 'X', 3, 1)],
                           needed=('T', 'X'))
        self.assertEqual(result['T'], {'currency': 'T', 'state': READY,
                                       'rate': {'numerator': 1,
                                                'denominator': 1}, 'path': []})
        self.assertEqual(result['X']['state'], INCONSISTENT)

    def test_rate_is_reduced_fraction(self):
        result = day_graph([q('q1', 'A', 'B', 2, 3),
                            q('q2', 'B', 'T', 4, 5)])
        self.assertEqual(result['A']['rate'], {'numerator': 8, 'denominator': 15})

    def test_graph_resolution_independent_of_quote_order(self):
        quotes = [q('qAB', 'A', 'B', 2, 1), q('qBT', 'B', 'T', 3, 1),
                  q('qAT', 'A', 'T', 7, 1)]
        first = json.dumps(day_graph(quotes), sort_keys=True)
        for perm in (list(reversed(quotes)),
                     [quotes[2], quotes[0], quotes[1]],
                     [quotes[1], quotes[2], quotes[0]]):
            self.assertEqual(json.dumps(day_graph(perm), sort_keys=True), first)


# ---------------------------------------------------------------------------
# Exact rounding
# ---------------------------------------------------------------------------

class RoundingTests(unittest.TestCase):
    def test_half_to_even(self):
        cases = {Fraction(1, 2): 0, Fraction(3, 2): 2, Fraction(5, 2): 2,
                 Fraction(7, 2): 4, Fraction(-1, 2): 0, Fraction(-3, 2): -2,
                 Fraction(2): 2, Fraction(1, 3): 0, Fraction(2, 3): 1,
                 Fraction(-5): -5, Fraction(0): 0}
        for value, expected in cases.items():
            self.assertEqual(round_half_even(value), expected, value)

    def test_no_float_tolerance_in_graph_contradiction(self):
        # Rates whose product is extremely close to, but not exactly, 1.
        result = day_graph([q('a', 'A', 'B', 10 ** 9, 1),
                            q('b', 'B', 'T', 1, 10 ** 9),
                            q('c', 'A', 'T', 10 ** 9, 10 ** 9 - 1)])
        self.assertEqual(result['A']['state'], INCONSISTENT)


# ---------------------------------------------------------------------------
# Translation accounting and traceability
# ---------------------------------------------------------------------------

class AccountingTests(unittest.TestCase):
    def _half_entry(self):
        # Two debit lines of 1 each and one credit line of 2.
        return entry('e', 'A', [line('cash', 'debit', 1),
                                line('receivable', 'debit', 1),
                                line('sales', 'credit', 2)])

    def _request(self, rate_quote):
        e = self._half_entry()
        return request([e], [date_row('e', '2026-02-01')], [rate_quote])

    def test_rate_one_half_rounds_debits_to_zero_and_appends_debit(self):
        report = translate(self._request(q('qAT', 'A', 'T', 1, 2)))
        traces = report['line_trace']
        self.assertEqual([t['line'] for t in traces], [0, 1, 2])
        self.assertEqual([t['rounded_amount'] for t in traces], [0, 0, 1])
        # Zero-rounded original lines keep an exact-fraction trace but never
        # enter the trial balance.
        zero_lines = [t for t in traces if t['rounded_amount'] == 0]
        self.assertEqual(len(zero_lines), 2)
        for trace in zero_lines:
            self.assertEqual(trace['exact_amount'],
                             {'numerator': 1, 'denominator': 2})
            self.assertEqual(trace['kind'], 'original')
        rounding = report['rounding_lines']
        self.assertEqual(len(rounding), 1)
        self.assertEqual(rounding[0], {'entry': 'e', 'account': 'fx_rounding',
                                       'side': 'debit', 'amount': 1,
                                       'kind': 'rounding', 'line': 3})
        tb = {row['account']: row for row in report['trial_balance']['accounts']}
        self.assertEqual(tb['cash']['debit'], 0)
        self.assertEqual(tb['receivable']['debit'], 0)
        self.assertEqual(tb['fx_rounding']['debit'], 1)
        self.assertEqual(tb['sales']['credit'], 1)
        self.assertEqual(report['trial_balance']['total_debits'],
                         report['trial_balance']['total_credits'])

    def test_rate_three_half_rounds_to_two_two_three_and_appends_credit(self):
        report = translate(self._request(q('qAT', 'A', 'T', 3, 2)))
        traces = report['line_trace']
        # 1.5 rounds to the even integer 2; 3.0 stays 3.
        self.assertEqual([t['rounded_amount'] for t in traces], [2, 2, 3])
        rounding = report['rounding_lines']
        self.assertEqual(len(rounding), 1)
        self.assertEqual(rounding[0]['side'], 'credit')
        self.assertEqual(rounding[0]['amount'], 1)
        self.assertEqual(rounding[0]['account'], 'fx_rounding')
        # Every original line remains individually traceable.
        self.assertEqual(len(traces), 3)
        for trace in traces:
            self.assertEqual(trace['rate_evidence']['rate'],
                             {'numerator': 3, 'denominator': 2})
            self.assertEqual(trace['original_currency'], 'A')

    def test_duplicate_account_lines_are_not_merged_before_rounding(self):
        # Two debit lines of 1 on the SAME account: merging would round the
        # combined 2 * 1/2 = 1 instead of rounding each 1/2 to 0.
        e = entry('dup', 'A', [line('cash', 'debit', 1),
                               line('cash', 'debit', 1),
                               line('sales', 'credit', 2)])
        report = translate(request([e], [date_row('dup', '2026-02-01')],
                                   [q('qAT', 'A', 'T', 1, 2)]))
        self.assertEqual([t['rounded_amount'] for t in report['line_trace']],
                         [0, 0, 1])
        tb = {row['account']: row
              for row in report['trial_balance']['accounts']}
        self.assertEqual(tb['cash']['debit'], 0)

    def test_trial_balance_lists_every_defined_account_including_zeros(self):
        e = balanced_entry('e', 'T', 'cash', 'sales', 9)
        report = translate(request([e], [date_row('e', '2026-02-01')], []))
        accounts = [row['account']
                    for row in report['trial_balance']['accounts']]
        self.assertEqual(accounts, sorted(a['id'] for a in ACCOUNTS))
        receivable = next(row for row in report['trial_balance']['accounts']
                          if row['account'] == 'receivable')
        self.assertEqual(receivable['normal_balance'], 0)
        self.assertEqual(receivable['debit'], 0)
        self.assertEqual(receivable['credit'], 0)

    def test_normal_balance_respects_account_direction(self):
        # The signed normal balance follows the account's normal direction:
        # a debit-normal account holding a net credit is negative, and a
        # credit-normal account holding a net debit is negative too.
        accounts = [account('dn', 'debit'), account('cn', 'credit'),
                    account('fx_rounding', 'credit')]
        e = entry('e', 'T', [line('cn', 'debit', 4),
                             line('dn', 'credit', 4)])
        report = translate(request([e], [date_row('e', '2026-02-01')], [],
                                   accounts=accounts))
        rows = {row['account']: row
                for row in report['trial_balance']['accounts']}
        self.assertEqual(rows['dn']['normal_balance'], -4)
        self.assertEqual(rows['cn']['normal_balance'], -4)

    def test_per_entry_and_overall_balance(self):
        e1 = balanced_entry('e1', 'A', 'cash', 'sales', 1)
        e2 = balanced_entry('e2', 'A', 'receivable', 'sales', 3)
        dates = [date_row('e1', '2026-02-01'), date_row('e2', '2026-02-01')]
        report = translate(request([e1, e2], dates,
                                   [q('qAT', 'A', 'T', 1, 2)]))
        self.assertTrue(all(b['balanced'] for b in report['entry_balances']))
        tb = report['trial_balance']
        self.assertEqual(tb['total_debits'], tb['total_credits'])
        self.assertEqual(tb['net_debits'], tb['net_credits'])

    def test_target_currency_entries_need_no_quotes(self):
        e = balanced_entry('e', 'T', 'cash', 'sales', 12)
        report = translate(request([e], [date_row('e', '2026-02-01')], []))
        self.assertEqual(report['entry_qualification'][0]['state'], READY)
        trace = report['line_trace'][0]
        self.assertEqual(trace['rate_evidence']['rate'],
                         {'numerator': 1, 'denominator': 1})
        self.assertEqual(trace['rate_evidence']['path'], [])
        self.assertEqual(trace['rounded_amount'], 12)


# ---------------------------------------------------------------------------
# Qualification drives posting: joint two-day acceptance
# ---------------------------------------------------------------------------

class QualificationTests(unittest.TestCase):
    def _entries(self):
        return [
            balanced_entry('t1', 'T', 'cash', 'sales', 5),
            balanced_entry('t2', 'T', 'receivable', 'sales', 7),
            balanced_entry('a1', 'A', 'cash', 'sales', 1),
            balanced_entry('a2', 'A', 'receivable', 'sales', 1),
        ]

    def _dates(self):
        return [date_row('t1', '2026-01-01'),
                date_row('a1', '2026-01-01'),
                date_row('t2', '2026-01-02'),
                date_row('a2', '2026-01-02')]

    def _quotes(self, contradiction):
        quotes = [quote('qAB', 'A', 'B', 2, 1, '2026-01-01', '2026-01-03'),
                  quote('qBT', 'B', 'T', 3, 1, '2026-01-01', '2026-01-03')]
        if contradiction:
            quotes.append(quote('qATbad', 'A', 'T', 7, 1,
                                '2026-01-02', '2026-01-03'))
        return quotes

    def _state(self, report, entry_id):
        return next(row for row in report['entry_qualification']
                    if row['entry'] == entry_id)['state']

    def test_second_day_contradiction_pends_only_that_day_a_entry(self):
        report = translate(request(self._entries(), self._dates(),
                                   self._quotes(True)))
        self.assertEqual(self._state(report, 't1'), READY)
        self.assertEqual(self._state(report, 't2'), READY)
        self.assertEqual(self._state(report, 'a1'), READY)
        self.assertEqual(self._state(report, 'a2'), INCONSISTENT)
        self.assertEqual([p['entry'] for p in report['pending_entries']],
                         ['a2'])
        pending = report['pending_entries'][0]
        self.assertEqual(pending['reason']['state'], INCONSISTENT)
        self.assertNotEqual(pending['reason']['witness']['product'],
                            {'numerator': 1, 'denominator': 1})
        # No pending line or difference leaks into the target trial balance.
        self.assertEqual({b['entry'] for b in report['entry_balances']},
                         {'t1', 't2', 'a1'})
        self.assertTrue(all(t['entry'] != 'a2' for t in report['line_trace']))
        # Pending amounts are not counted as zero-value successes.
        self.assertEqual(report['statistics']['posted_count'], 3)
        self.assertEqual(report['statistics']['pending_count'], 1)
        self.assertEqual(report['statistics']['by_state'][INCONSISTENT], 1)

    def test_fixing_quote_changes_only_second_day_a_entry(self):
        bad = translate(request(self._entries(), self._dates(),
                                self._quotes(True)))
        good = translate(request(self._entries(), self._dates(),
                                 self._quotes(False)))
        self.assertEqual(self._state(good, 'a2'), READY)
        self.assertEqual(good['pending_entries'], [])
        # Day 1 resolution and the T-currency entries are byte-identical.
        day1_bad = next(d for d in bad['quote_resolution']
                        if d['date'] == '2026-01-01')
        day1_good = next(d for d in good['quote_resolution']
                         if d['date'] == '2026-01-01')
        self.assertEqual(day1_bad, day1_good)
        self.assertEqual(
            [t for t in bad['line_trace'] if t['entry'] in ('t1', 't2')],
            [t for t in good['line_trace'] if t['entry'] in ('t1', 't2')])
        # Only a2 enters the ledger and moves the totals by its rounded 6/6.
        self.assertEqual(good['trial_balance']['total_debits']
                         - bad['trial_balance']['total_debits'], 6)
        a2 = [t for t in good['line_trace'] if t['entry'] == 'a2']
        self.assertTrue(a2)
        self.assertTrue(all(t['rate_evidence']['rate']
                            == {'numerator': 6, 'denominator': 1} for t in a2))

    def test_every_source_entry_appears_exactly_once(self):
        for contradiction in (True, False):
            report = translate(request(self._entries(), self._dates(),
                                       self._quotes(contradiction)))
            posted = {b['entry'] for b in report['entry_balances']}
            pending = {p['entry'] for p in report['pending_entries']}
            self.assertEqual(posted | pending, {'t1', 't2', 'a1', 'a2'})
            self.assertFalse(posted & pending)
            self.assertEqual(len(report['entry_qualification']), 4)

    def test_quote_expiry_leads_to_missing(self):
        quotes = [quote('qAB', 'A', 'B', 2, 1, '2026-01-01', '2026-01-02'),
                  quote('qBT', 'B', 'T', 3, 1, '2026-01-01', '2026-01-03')]
        report = translate(request(self._entries(), self._dates(), quotes))
        self.assertEqual(self._state(report, 'a1'), READY)
        self.assertEqual(self._state(report, 'a2'), MISSING)
        pending = next(p for p in report['pending_entries']
                       if p['entry'] == 'a2')
        self.assertEqual(pending['reason']['reachable_currencies'], ['A'])
        self.assertNotIn('witness', pending['reason'])

    def test_half_open_validity_boundary(self):
        # valid_to is exclusive: a quote ending on the 2nd is not active then.
        quotes = [quote('qAT', 'A', 'T', 2, 1, '2026-01-01', '2026-01-02')]
        report = translate(request(self._entries(), self._dates(), quotes))
        self.assertEqual(self._state(report, 'a1'), READY)
        self.assertEqual(self._state(report, 'a2'), MISSING)
        day2 = next(d for d in report['quote_resolution']
                    if d['date'] == '2026-01-02')
        self.assertEqual(day2['active_quote_ids'], [])

    def test_disconnected_contradiction_keeps_healthy_entries_posted(self):
        quotes = self._quotes(False) + [
            quote('x1', 'X', 'Y', 2, 1, '2026-01-01', '2026-01-03'),
            quote('x2', 'Y', 'X', 1, 1, '2026-01-01', '2026-01-03')]
        report = translate(request(self._entries(), self._dates(), quotes))
        self.assertEqual(report['pending_entries'], [])
        self.assertTrue(all(q['state'] == READY
                            for q in report['entry_qualification']))

    def test_pending_is_business_success_not_zero_amount(self):
        report = translate(request(self._entries(), self._dates(),
                                   self._quotes(True)))
        # Statistics still attribute the pending entry to its currency.
        by_ccy = {row['currency']: row
                  for row in report['statistics']['by_original_currency']}
        self.assertEqual(by_ccy['A'][INCONSISTENT], 1)
        self.assertEqual(by_ccy['A'][READY], 1)


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------

class OrderIndependenceTests(unittest.TestCase):
    def test_report_independent_of_input_arrangement(self):
        entries = [
            balanced_entry('t1', 'T', 'cash', 'sales', 5),
            balanced_entry('a1', 'A', 'cash', 'sales', 1),
            balanced_entry('a2', 'A', 'receivable', 'sales', 1),
        ]
        dates = [date_row('t1', '2026-01-01'),
                 date_row('a1', '2026-01-01'),
                 date_row('a2', '2026-01-02')]
        quotes = [quote('qAB', 'A', 'B', 2, 1, '2026-01-01', '2026-01-03'),
                  quote('qBT', 'B', 'T', 3, 1, '2026-01-01', '2026-01-03')]
        base = request(entries, dates, quotes)
        canonical = json.dumps(translate(copy.deepcopy(base)), sort_keys=True)

        def shuffle(obj, rng, key=None):
            if isinstance(obj, dict):
                items = list(obj.items())
                rng.shuffle(items)
                return {k: shuffle(v, rng, k) for k, v in items}
            if isinstance(obj, list):
                new = [shuffle(v, rng, key) for v in obj]
                if key != 'lines':  # line index is part of the trace contract
                    rng.shuffle(new)
                return new
            return obj

        import random
        rng = random.Random(424242)
        for _ in range(10):
            shuffled = shuffle(copy.deepcopy(base), rng)
            self.assertEqual(
                json.dumps(translate(shuffled), sort_keys=True), canonical)


# ---------------------------------------------------------------------------
# Strict validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def _base(self):
        e = balanced_entry('e', 'A', 'cash', 'sales', 2)
        return request([e], [date_row('e', '2026-02-01')],
                       [q('qAT', 'A', 'T', 1, 1)])

    def assert_invalid(self, data):
        # The original parse_journal failures surface as plain ValueErrors,
        # exactly as in the v1 and other workbenches; TranslateError is itself
        # a ValueError subclass, and the CLI maps the whole family to exit 2.
        with self.assertRaises(ValueError):
            translate(data)

    def test_top_level_fields(self):
        data = self._base()
        data['unexpected'] = 1
        self.assert_invalid(data)
        del data['quotes']
        self.assert_invalid(data)

    def test_journal_contract_is_unchanged(self):
        data = self._base()
        data['journal']['entries'][0]['lines'][0]['amount'] = 999
        self.assert_invalid(data)  # unbalanced
        data = self._base()
        data['journal']['entries'][0]['lines'][0]['amount'] = True
        self.assert_invalid(data)  # booleans forbidden
        data = self._base()
        data['journal']['entries'].append(
            copy.deepcopy(data['journal']['entries'][0]))
        self.assert_invalid(data)  # duplicate entry id

    def test_rounding_account_must_be_defined(self):
        data = self._base()
        data['rounding_account'] = 'ghost'
        with self.assertRaises(TranslateError):
            translate(data)

    def test_target_identifier(self):
        data = self._base()
        data['target'] = 7
        self.assert_invalid(data)

    def test_entry_dates_coverage_and_uniqueness(self):
        data = self._base()
        data['entry_dates'].append(date_row('e', '2026-02-02'))
        self.assert_invalid(data)
        data = self._base()
        data['entry_dates'][0]['entry'] = 'ghost'
        self.assert_invalid(data)
        data = self._base()
        data['entry_dates'] = []
        self.assert_invalid(data)

    def test_real_gregorian_dates(self):
        for bad_day in ('2026-13-01', '2026-02-29', '2026-1-01',
                        '2026-02-30', 'not-a-date'):
            data = self._base()
            data['entry_dates'][0]['date'] = bad_day
            self.assert_invalid(data)

    def test_quote_fields_and_bounds(self):
        data = self._base()
        data['quotes'][0]['numerator'] = True
        self.assert_invalid(data)
        data = self._base()
        data['quotes'][0]['denominator'] = 0
        self.assert_invalid(data)
        data = self._base()
        data['quotes'][0]['numerator'] = 10 ** 9 + 1
        self.assert_invalid(data)
        data = self._base()
        data['quotes'].append(copy.deepcopy(data['quotes'][0]))
        self.assert_invalid(data)  # duplicate quote id
        data = self._base()
        data['quotes'][0]['base'] = data['quotes'][0]['quote']
        self.assert_invalid(data)
        data = self._base()
        del data['quotes'][0]['base']
        self.assert_invalid(data)

    def test_quote_validity_interval(self):
        data = self._base()
        data['quotes'][0]['valid_to'] = data['quotes'][0]['valid_from']
        self.assert_invalid(data)  # empty half-open interval
        data = self._base()
        data['quotes'][0]['valid_to'] = '2025-12-31'
        self.assert_invalid(data)  # from > to
        data = self._base()
        data['quotes'][0]['valid_from'] = '2026-02-30'
        self.assert_invalid(data)

    def test_empty_journal_is_valid(self):
        data = self._base()
        data['journal']['entries'] = []
        data['entry_dates'] = []
        report = translate(data)
        self.assertEqual(report['statistics']['entry_count'], 0)
        self.assertEqual(report['trial_balance']['total_debits'], 0)
        self.assertEqual(len(report['trial_balance']['accounts']),
                         len(ACCOUNTS))

    def test_sha256_is_echoed(self):
        report = translate(self._base(), 'a' * 64)
        self.assertEqual(report['source_sha256'], 'a' * 64)

    def test_hard_limits(self):
        import datetime
        # 2001 quotes rejected (shared bounded-array rule, plain ValueError).
        data = self._base()
        data['quotes'] = [q('q%04d' % i, 'A', 'T', 1, 1) for i in range(2001)]
        with self.assertRaises(ValueError):
            translate(data)
        # 367 distinct entry dates rejected; 366 distinct dates accepted.
        entries, dates = [], []
        for i in range(367):
            eid = 'e%03d' % i
            entries.append(balanced_entry(eid, 'A', 'cash', 'sales', 1))
            day = (datetime.date(2026, 1, 1)
                   + datetime.timedelta(days=i)).isoformat()
            dates.append(date_row(eid, day))
        data = request(entries, dates,
                       [quote('q', 'A', 'T', 1, 1, '2025-01-01', '2027-12-31')])
        with self.assertRaises(TranslateError):
            translate(data)
        data['journal']['entries'].pop()
        data['entry_dates'].pop()
        self.assertEqual(len({d['date'] for d in data['entry_dates']}), 366)
        report = translate(data)
        self.assertEqual(report['statistics']['entry_count'], 366)
        # More than 60 appearing currencies (A, T plus 59 bridge currencies).
        data = self._base()
        data['quotes'] = [q('c%02d' % i, 'C%02d' % i, 'T', 1, 1)
                          for i in range(59)]
        with self.assertRaises(TranslateError):
            translate(data)


# ---------------------------------------------------------------------------
# Real CLI and safe publication
# ---------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    FIXTURE = ROOT / 'fixtures' / 'translate_request.json'

    def _call(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'journal_check.cli', 'translate',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)

    def test_cli_success_and_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'report.json'
            result = self._call(self.FIXTURE, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            digest = hashlib.sha256(self.FIXTURE.read_bytes()).hexdigest()
            self.assertEqual(report['source_sha256'], digest)
            self.assertEqual(report['target_currency'], 'USD')

    def test_pending_entries_still_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'pending.json'
            e = balanced_entry('e', 'A', 'cash', 'sales', 1)
            payload = request([e], [date_row('e', '2026-02-01')], [])
            source.write_text(json.dumps(payload))
            out = Path(td) / 'report.json'
            result = self._call(source, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['pending_entries'][0]['entry'], 'e')

    def test_failure_exits_two_and_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'bad.json'
            out = Path(td) / 'report.json'
            out.write_text('prior report')
            for body in ('{}', '{"a":1,"a":2}', 'NaN',
                         json.dumps({'journal': {}, 'target': 'T',
                                     'rounding_account': 'r',
                                     'entry_dates': [], 'quotes': []})):
                source.write_text(body)
                result = self._call(source, out)
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr.strip(), '')
                self.assertEqual(out.read_text(), 'prior report')

    def test_same_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'in.json'
            source.write_bytes(self.FIXTURE.read_bytes())
            result = self._call(source, source)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), self.FIXTURE.read_bytes())

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'target.json'
            link = Path(td) / 'link.json'
            link.symlink_to(target)
            result = self._call(self.FIXTURE, link)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_eight_megabyte_limit(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'huge.json'
            source.write_text('{' + '"pad": "' + 'x' * (8 * 10 ** 6) + '"}')
            out = Path(td) / 'report.json'
            result = self._call(source, out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)

    def test_run_file_interface(self):
        report = run_file(self.FIXTURE)
        digest = hashlib.sha256(self.FIXTURE.read_bytes()).hexdigest()
        self.assertEqual(report['source_sha256'], digest)
        self.assertEqual(report['schema_version'], 1)


if __name__ == '__main__':
    unittest.main()
