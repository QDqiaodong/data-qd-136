"""Inventory-cost event replay engine.

The engine replays the strictly validated, ``seq``-sorted event stream and
maintains two kinds of state:

* **physical ownership** — every receipt batch's available units and every
  issue's not-yet-returned units, always as half-open intervals of the
  original receipt unit indices (see :mod:`intervals`);
* **cost basis** — each batch's current total cost after every reprice; unit
  costs are never stored, they are re-derived on demand from the exact batch
  prefix formula (see :mod:`costing`).

Reprice events never move physical units. They revalue the batch along its
*current* ownership partition: units held back in inventory and units still
held by each issue. The revaluation difference is posted to the account of the
current owner only — a unit that was issued, returned and re-issued belongs to
the later issue, never again to the original department or to inventory.

The module is pure: callers handle the strict 8 MB read and atomic publication.
Only the Python standard library is used.
"""
from ..ledger import balance
from . import intervals
from .costing import prefix_cost, segments_cost
from .errors import InventoryValidationError
from .validation import validate_request

INVENTORY = 'inventory'
ISSUE_HOLDER = 'issue'


def _fail(message):
    raise InventoryValidationError(message)


# ---------------------------------------------------------------------------
# Engine state
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self, validated):
        self.accounts = validated['accounts']
        self.stocks = validated['stocks']
        self.inventory_accounts = validated['inventory_accounts']
        self.payable_accounts = validated['payable_accounts']
        self.pool_by_key = {(s['sku'], s['currency']): s for s in self.stocks}
        # batch id -> batch record
        self.batches = {}
        # issue id -> issue record
        self.issues = {}
        # seq-sorted batch ids, for FIFO across batches and canonical output
        self.batch_order = []
        self.event_records = []
        self.physical_trace = []
        self.attributions = []
        self.entries = []
        self.revaluations = []
        # posting lines fed to the shared ledger semantics, one tuple per line
        self.posting_lines = []
        self.counters = {'receipt': 0, 'issue': 0, 'return': 0,
                         'reprice': 0}
        self.currencies = sorted({s['currency'] for s in self.stocks})

    # -- helpers -----------------------------------------------------------

    def _pool(self, sku, currency, event_id):
        pool = self.pool_by_key.get((sku, currency))
        if pool is None:
            _fail('event %s references unknown stock pool %s/%s'
                  % (event_id, sku, currency))
        return pool

    def _check_expense_account(self, account, event_id):
        if account not in self.accounts:
            _fail('event %s expense_account must be a defined account: %s'
                  % (event_id, account))
        if account in self.inventory_accounts or account in \
                self.payable_accounts:
            _fail('event %s expense_account %s cannot be an inventory or '
                  'payable account used by stocks' % (event_id, account))

    def _emit_line(self, currency, account, side, amount, entry_id, line):
        self.posting_lines.append((entry_id, currency,
                                   (account, side, amount)))

    def _add_attribution(self, record):
        record['attribution'] = len(self.attributions)
        self.attributions.append(record)

    # -- batch ownership ---------------------------------------------------

    def _batch_owners(self, batch):
        """Current owners of a batch as canonical (kind, key, segments) rows.

        Inventory first (one row of the batch's available intervals), then
        every issue still holding units of this batch in issue-id order. Each
        row's segments partition the units held by that owner.
        """
        owners = [(INVENTORY, None, list(batch['available']))]
        holder_rows = []
        for issue_id in sorted(batch['holders']):
            issue = self.issues[issue_id]
            segments = [seg for seg in issue['segments']
                        if seg['receipt'] == batch['id']]
            if segments:
                holder_rows.append((ISSUE_HOLDER, issue_id,
                                    [(seg['start'], seg['end'])
                                     for seg in segments]))
        holder_rows.sort(key=lambda row: row[1])
        owners.extend(holder_rows)
        return owners

    def _assert_batch_partition(self, batch):
        """Invariant: available + every holder partitions [0, quantity)."""
        bound = batch['quantity']
        held = []
        for issue_id in sorted(batch['holders']):
            issue = self.issues[issue_id]
            for seg in issue['segments']:
                if seg['receipt'] == batch['id']:
                    held.append((seg['start'], seg['end']))
        held.sort()
        intervals.assert_partition(batch['available'], bound,
                                   'batch %s inventory' % batch['id'])
        intervals.assert_partition(held, bound,
                                   'batch %s issues' % batch['id'])
        intervals.assert_disjoint(batch['available'], held, bound,
                                  'batch %s' % batch['id'])
        total = (intervals.total_length(batch['available'])
                 + intervals.total_length(held))
        if total != bound:
            raise AssertionError('batch %s lost or duplicated units: %d != %d'
                                 % (batch['id'], total, bound))

    # -- event replay ------------------------------------------------------

    def replay(self, event):
        handler = getattr(self, '_event_' + event['type'])
        handler(event)
        self.counters[event['type']] += 1

    def _event_receipt(self, event):
        event_id = event['id']
        if event_id in self.batches or event_id in self.issues:
            _fail('duplicate event id: ' + event_id)
        pool = self._pool(event['sku'], event['currency'], event_id)
        quantity = event['quantity']
        batch = {
            'id': event_id,
            'seq': event['seq'],
            'sku': event['sku'],
            'currency': event['currency'],
            'quantity': quantity,
            'total_cost': event['total_cost'],
            'inventory_account': pool['inventory_account'],
            'payable_account': pool['payable_account'],
            'available': [(0, quantity)],
            'holders': set(),
        }
        self.batches[event_id] = batch
        self.batch_order.append(event_id)
        self.event_records.append(event)
        self.physical_trace.append({
            'event': event_id, 'seq': event['seq'], 'type': 'receipt',
            'sku': event['sku'], 'currency': event['currency'],
            'quantity': quantity,
            'allocation': [{'receipt': event_id, 'start': 0,
                            'end': quantity}],
        })
        lines = []
        amount = event['total_cost']
        if amount > 0:
            # Zero-cost receipts keep their physical evidence but create no
            # zero-amount posting lines.
            self._add_attribution({
                'event': event_id, 'type': 'receipt', 'kind': 'flow',
                'owner': INVENTORY, 'receipt': event_id,
                'start': 0, 'end': quantity, 'currency': event['currency'],
                'cost_basis': amount, 'amount': amount,
                'signed_amount': amount,
            })
            debit_line = {'line': 0, 'account': pool['inventory_account'],
                          'side': 'debit', 'amount': amount,
                          'attribution': len(self.attributions) - 1}
            credit_line = {'line': 1, 'account': pool['payable_account'],
                           'side': 'credit', 'amount': amount,
                           'attribution': len(self.attributions) - 1}
            lines = [debit_line, credit_line]
            self._emit_line(event['currency'],
                            pool['inventory_account'], 'debit', amount,
                            event_id, 0)
            self._emit_line(event['currency'],
                            pool['payable_account'], 'credit', amount,
                            event_id, 1)
        self.entries.append({
            'entry': event_id, 'seq': event['seq'], 'type': 'receipt',
            'currency': event['currency'],
            'debit': amount, 'credit': amount, 'lines': lines,
        })

    def _event_issue(self, event):
        event_id = event['id']
        if event_id in self.batches or event_id in self.issues:
            _fail('duplicate event id: ' + event_id)
        pool = self._pool(event['sku'], event['currency'], event_id)
        self._check_expense_account(event['expense_account'], event_id)
        quantity = event['quantity']

        # FIFO: oldest receipt batch first (receipt.seq), lowest unit indices
        # first within each batch.
        candidates = [self.batches[bid] for bid in self.batch_order
                      if self.batches[bid]['currency'] == event['currency']
                      and self.batches[bid]['sku'] == event['sku']]
        available_total = sum(intervals.total_length(b['available'])
                              for b in candidates)
        if available_total < quantity:
            _fail('event %s issues %d units but pool %s/%s only holds %d'
                  % (event_id, quantity, event['sku'], event['currency'],
                     available_total))

        issue = {'id': event_id, 'seq': event['seq'],
                 'sku': event['sku'], 'currency': event['currency'],
                 'quantity': quantity,
                 'expense_account': event['expense_account'],
                 'segments': [], 'returned_quantity': 0}
        self.issues[event_id] = issue
        allocation = []
        lines = []
        total_amount = 0
        remaining = quantity
        line_index = 0
        for batch in candidates:
            if remaining == 0:
                break
            held = intervals.total_length(batch['available'])
            if held == 0:
                continue
            take = min(remaining, held)
            taken, left = intervals.take_prefix(batch['available'], take)
            batch['available'] = left
            batch['holders'].add(event_id)
            for start, end in taken:
                amount = prefix_cost(batch['total_cost'],
                                     batch['quantity'], start, end)
                issue['segments'].append({'receipt': batch['id'],
                                           'start': start, 'end': end})
                allocation.append({'receipt': batch['id'], 'start': start,
                                   'end': end})
                self._add_attribution({
                    'event': event_id, 'type': 'issue', 'kind': 'flow',
                    'owner': ISSUE_HOLDER, 'issue': event_id,
                    'receipt': batch['id'], 'start': start, 'end': end,
                    'currency': event['currency'], 'cost_basis': amount,
                    'amount': amount, 'signed_amount': amount,
                })
                if amount > 0:
                    # Each non-zero cost attribution is its own sourced pair of
                    # lines; zero-cost fragments keep evidence but no lines.
                    attribution_index = len(self.attributions) - 1
                    lines.append({'line': line_index,
                                  'account': event['expense_account'],
                                  'side': 'debit', 'amount': amount,
                                  'attribution': attribution_index})
                    self._emit_line(event['currency'],
                                    event['expense_account'], 'debit',
                                    amount, event_id, line_index)
                    line_index += 1
                    lines.append({'line': line_index,
                                  'account': batch['inventory_account'],
                                  'side': 'credit', 'amount': amount,
                                  'attribution': attribution_index})
                    self._emit_line(event['currency'],
                                    batch['inventory_account'], 'credit',
                                    amount, event_id, line_index)
                    line_index += 1
                    total_amount += amount
            remaining -= take
        if remaining != 0:  # pragma: no cover - guarded by available_total
            raise AssertionError('issue allocation did not cover the quantity')

        self.event_records.append(event)
        self.physical_trace.append({
            'event': event_id, 'seq': event['seq'], 'type': 'issue',
            'sku': event['sku'], 'currency': event['currency'],
            'quantity': quantity, 'allocation': allocation,
        })
        self.entries.append({
            'entry': event_id, 'seq': event['seq'], 'type': 'issue',
            'currency': event['currency'],
            'debit': total_amount, 'credit': total_amount, 'lines': lines,
        })

    def _event_return(self, event):
        event_id = event['id']
        if event_id in self.batches or event_id in self.issues:
            _fail('duplicate event id: ' + event_id)
        issue = self.issues.get(event['issue'])
        if issue is None:
            _fail('event %s returns unknown or future issue %s'
                  % (event_id, event['issue']))
        quantity = event['quantity']
        held_now = intervals.total_length(
            [(s['start'], s['end']) for s in issue['segments']])
        if quantity > held_now:
            _fail('event %s returns %d units but issue %s only still holds %d'
                  % (event_id, quantity, issue['id'], held_now))

        # Undo the issue's not-yet-returned units in reverse allocation order;
        # each restored fragment goes back to its original batch and index,
        # where it again ages by original receipt for later FIFO issues.
        # Segments stay labeled with their receipt because unit indices
        # restart at 0 inside every batch.
        labeled = [(s['receipt'], s['start'], s['end'])
                   for s in issue['segments']]
        removed, kept = intervals.take_tail_labeled(labeled, quantity)
        kept_segments = [{'receipt': label, 'start': start, 'end': end}
                         for label, start, end in kept]
        restored = []
        lines = []
        total_amount = 0
        line_index = 0
        for label, start, end in removed:
            batch = self.batches[label]
            # Cost at the *current* batch cost at return time.
            amount = prefix_cost(batch['total_cost'], batch['quantity'],
                                 start, end)
            restored.append({'receipt': label, 'start': start,
                             'end': end})
            self._add_attribution({
                'event': event_id, 'type': 'return', 'kind': 'flow',
                'owner': INVENTORY, 'issue': issue['id'],
                'receipt': label, 'start': start, 'end': end,
                'currency': batch['currency'], 'cost_basis': amount,
                'amount': amount, 'signed_amount': -amount,
            })
            if amount > 0:
                attribution_index = len(self.attributions) - 1
                lines.append({'line': line_index,
                              'account': batch['inventory_account'],
                              'side': 'debit', 'amount': amount,
                              'attribution': attribution_index})
                self._emit_line(batch['currency'],
                                batch['inventory_account'], 'debit', amount,
                                event_id, line_index)
                line_index += 1
                lines.append({'line': line_index,
                              'account': issue['expense_account'],
                              'side': 'credit', 'amount': amount,
                              'attribution': attribution_index})
                self._emit_line(batch['currency'],
                                issue['expense_account'], 'credit', amount,
                                event_id, line_index)
                line_index += 1
                total_amount += amount
            batch['available'] = intervals.restore(
                batch['available'], [(start, end)])

        issue['segments'] = kept_segments
        issue['returned_quantity'] += quantity
        if not issue['segments']:
            touched_batches = {r['receipt'] for r in restored}
            for batch_id in touched_batches:
                self.batches[batch_id]['holders'].discard(issue['id'])

        self.event_records.append(event)
        self.physical_trace.append({
            'event': event_id, 'seq': event['seq'], 'type': 'return',
            'issue': issue['id'], 'quantity': quantity,
            'allocation': restored,
        })
        self.entries.append({
            'entry': event_id, 'seq': event['seq'], 'type': 'return',
            'currency': issue['currency'],
            'debit': total_amount, 'credit': total_amount, 'lines': lines,
        })

    def _event_reprice(self, event):
        event_id = event['id']
        if event_id in self.batches or event_id in self.issues:
            _fail('duplicate event id: ' + event_id)
        batch = self.batches.get(event['receipt'])
        if batch is None:
            _fail('event %s reprices unknown or future receipt %s'
                  % (event_id, event['receipt']))
        old_cost = batch['total_cost']
        new_cost = event['total_cost']
        quantity = batch['quantity']

        # Physical allocation is untouched; revalue along the current owner
        # partition. Flatten every owner's segments and walk them in original
        # unit-index order so the revaluation trace reads like the batch's
        # physical units, regardless of owner kind. Each non-zero difference is
        # one sourced attribution with its own pair of posting lines, never
        # netted across owners/issues.
        owner_rows = self._batch_owners(batch)
        unit_rows = []
        for kind, holder_id, segments in owner_rows:
            if kind == INVENTORY:
                owner_account = batch['inventory_account']
                owner_label = INVENTORY
            else:
                owner_account = self.issues[holder_id]['expense_account']
                owner_label = holder_id
            for start, end in segments:
                unit_rows.append((start, end, kind, owner_label,
                                  owner_account))
        unit_rows.sort(key=lambda row: (row[0], row[1]))
        allocations = []
        lines = []
        line_index = 0
        for start, end, kind, owner_label, owner_account in unit_rows:
            seg_old = prefix_cost(old_cost, quantity, start, end)
            seg_new = prefix_cost(new_cost, quantity, start, end)
            seg_diff = seg_new - seg_old
            allocations.append({
                'owner_kind': kind,
                'owner': owner_label,
                'receipt': batch['id'], 'start': start, 'end': end,
                'old_cost': seg_old, 'new_cost': seg_new,
                'difference': seg_diff, 'account': owner_account,
            })
            if seg_diff != 0:
                self._add_attribution({
                    'event': event_id, 'type': 'reprice',
                    'kind': 'revaluation', 'owner': owner_label,
                    'receipt': batch['id'], 'start': start, 'end': end,
                    'currency': batch['currency'],
                    'cost_basis': seg_old, 'amount': abs(seg_diff),
                    'signed_amount': seg_diff,
                })
                attribution_index = len(self.attributions) - 1
                if seg_diff > 0:
                    # Positive revision: debit the current owner, credit the
                    # batch payable.
                    lines.append({'line': line_index,
                                  'account': owner_account,
                                  'side': 'debit',
                                  'amount': seg_diff,
                                  'attribution': attribution_index})
                    self._emit_line(batch['currency'], owner_account,
                                    'debit', seg_diff, event_id,
                                    line_index)
                    line_index += 1
                    lines.append({'line': line_index,
                                  'account': batch['payable_account'],
                                  'side': 'credit',
                                  'amount': seg_diff,
                                  'attribution': attribution_index})
                    self._emit_line(batch['currency'],
                                    batch['payable_account'], 'credit',
                                    seg_diff, event_id, line_index)
                    line_index += 1
                else:
                    # Negative revision reverses the direction: debit payable,
                    # credit the current owner.
                    magnitude = -seg_diff
                    lines.append({'line': line_index,
                                  'account': batch['payable_account'],
                                  'side': 'debit',
                                  'amount': magnitude,
                                  'attribution': attribution_index})
                    self._emit_line(batch['currency'],
                                    batch['payable_account'], 'debit',
                                    magnitude, event_id, line_index)
                    line_index += 1
                    lines.append({'line': line_index,
                                  'account': owner_account,
                                  'side': 'credit',
                                  'amount': magnitude,
                                  'attribution': attribution_index})
                    self._emit_line(batch['currency'], owner_account,
                                    'credit', magnitude, event_id,
                                    line_index)
                    line_index += 1

        difference_sum = sum(row['difference'] for row in allocations)
        if difference_sum != new_cost - old_cost:  # pragma: no cover
            raise AssertionError(
                'reprice differences do not sum to the batch cost change')
        batch['total_cost'] = new_cost

        # Every attribution posts a balanced pair, so debit and credit totals
        # are the sums of the actual line amounts on each side (both sides can
        # be positive inside one reprice when owners move in both directions).
        entry_debit = sum(line['amount'] for line in lines
                          if line['side'] == 'debit')
        entry_credit = sum(line['amount'] for line in lines
                           if line['side'] == 'credit')

        self.event_records.append(event)
        self.revaluations.append({
            'event': event_id, 'seq': event['seq'], 'type': 'reprice',
            'receipt': batch['id'], 'currency': batch['currency'],
            'old_total_cost': old_cost, 'new_total_cost': new_cost,
            'total_change': new_cost - old_cost,
            'allocations': allocations,
        })
        self.entries.append({
            'entry': event_id, 'seq': event['seq'], 'type': 'reprice',
            'currency': batch['currency'],
            'debit': entry_debit, 'credit': entry_credit,
            'lines': lines,
        })

    # -- finalisation ------------------------------------------------------

    def final_positions(self):
        positions = {'receipts': [], 'issues': []}
        for batch_id in self.batch_order:
            batch = self.batches[batch_id]
            self._assert_batch_partition(batch)
            positions['receipts'].append({
                'receipt': batch_id, 'seq': batch['seq'],
                'sku': batch['sku'], 'currency': batch['currency'],
                'quantity': batch['quantity'],
                'total_cost': batch['total_cost'],
                'inventory_account': batch['inventory_account'],
                'payable_account': batch['payable_account'],
                'available_intervals': [
                    {'receipt': batch_id, 'start': start, 'end': end}
                    for start, end in batch['available']],
                'available_quantity': intervals.total_length(
                    batch['available']),
                'inventory_cost': segments_cost(
                    batch['total_cost'], batch['quantity'],
                    batch['available']),
            })
        for issue_id in sorted(self.issues):
            issue = self.issues[issue_id]
            unreturned = [{'receipt': seg['receipt'], 'start': seg['start'],
                           'end': seg['end']} for seg in issue['segments']]
            cost = 0
            for seg in issue['segments']:
                batch = self.batches[seg['receipt']]
                cost += prefix_cost(batch['total_cost'], batch['quantity'],
                                    seg['start'], seg['end'])
            positions['issues'].append({
                'issue': issue_id, 'seq': issue['seq'],
                'sku': issue['sku'], 'currency': issue['currency'],
                'quantity': issue['quantity'],
                'returned_quantity': issue['returned_quantity'],
                'unreturned_quantity': intervals.total_length(
                    [(s['start'], s['end']) for s in issue['segments']]),
                'expense_account': issue['expense_account'],
                'unreturned_intervals': unreturned,
                'unreturned_cost': cost,
            })
        return positions

    def trial_balance(self):
        """Reuse the existing ledger balance semantics over posted lines.

        Empty per-currency ledgers are seeded too, so every currency present in
        the request shows every defined account, including untouched zero
        accounts, exactly like the v1 trial balance.
        """
        entries_for_ledger = []
        grouped = {}
        for entry_id, currency, line in self.posting_lines:
            grouped.setdefault((entry_id, currency), []).append(line)
        for (entry_id, currency), lines in grouped.items():
            entries_for_ledger.append((entry_id, currency, lines))
        # Ensure currencies with no non-zero lines still appear.
        seen_currencies = {currency for _id, currency in grouped}
        for currency in self.currencies:
            if currency not in seen_currencies:
                entries_for_ledger.append(('__seed__' + currency, currency, []))
        return balance(self.accounts, entries_for_ledger)

    def reconciliations(self, positions, trial):
        trial_lookup = {
            block['currency']: {row['account']: row
                                for row in block['accounts']}
            for block in trial['currencies']}

        def row(currency, account):
            return trial_lookup[currency][account]

        # 1. Per-batch: current total cost = inventory valuation + every issue's
        #    unreturned cost on units sourced from this batch.
        batch_checks = []
        for position in positions['receipts']:
            batch = self.batches[position['receipt']]
            issue_cost = 0
            for issue_id in sorted(batch['holders']):
                issue = self.issues[issue_id]
                for seg in issue['segments']:
                    if seg['receipt'] == batch['id']:
                        issue_cost += prefix_cost(
                            batch['total_cost'], batch['quantity'],
                            seg['start'], seg['end'])
            total = position['inventory_cost'] + issue_cost
            batch_checks.append({
                'receipt': batch['id'], 'currency': batch['currency'],
                'total_cost': batch['total_cost'],
                'inventory_cost': position['inventory_cost'],
                'issued_unreturned_cost': issue_cost,
                'balanced': total == batch['total_cost'],
            })

        # 2. Inventory account trial balance = sum of valuations mapped to it
        #    within the same currency.
        inventory_checks = []
        valuation_by_key = {}
        for position in positions['receipts']:
            key = (position['currency'], position['inventory_account'])
            valuation_by_key[key] = valuation_by_key.get(key, 0) \
                + position['inventory_cost']
        for currency in self.currencies:
            for account in sorted(self.inventory_accounts):
                expected = valuation_by_key.get((currency, account), 0)
                actual = row(currency, account)['net_debit']
                inventory_checks.append({
                    'currency': currency, 'account': account,
                    'expected_debit': expected,
                    'trial_net_debit': actual,
                    'balanced': expected == actual,
                })

        # 3. Expense account balance = sum of its issues' unreturned costs.
        expense_checks = []
        expense_by_key = {}
        for position in positions['issues']:
            key = (position['currency'], position['expense_account'])
            expense_by_key[key] = expense_by_key.get(key, 0) \
                + position['unreturned_cost']
        expense_accounts = sorted(
            {issue['expense_account'] for issue in self.issues.values()})
        for currency in self.currencies:
            for account in expense_accounts:
                expected = expense_by_key.get((currency, account), 0)
                actual = row(currency, account)['net_debit']
                expense_checks.append({
                    'currency': currency, 'account': account,
                    'expected_debit': expected,
                    'trial_net_debit': actual,
                    'balanced': expected == actual,
                })

        # 4. Payable account: balance sheet identity per currency.
        payable_checks = []
        payable_by_key = {}
        for position in positions['receipts']:
            key = (position['currency'], position['payable_account'])
            payable_by_key[key] = payable_by_key.get(key, 0) \
                + position['total_cost']
        for currency in self.currencies:
            for account in sorted(self.payable_accounts):
                expected_credit = payable_by_key.get((currency, account), 0)
                actual_credit = row(currency, account)['net_credit']
                payable_checks.append({
                    'currency': currency, 'account': account,
                    'expected_credit': expected_credit,
                    'trial_net_credit': actual_credit,
                    'balanced': expected_credit == actual_credit,
                })

        all_checks = batch_checks + inventory_checks + expense_checks \
            + payable_checks
        return {
            'batch_cost_partitions': batch_checks,
            'inventory_valuations': inventory_checks,
            'expense_balances': expense_checks,
            'payable_balances': payable_checks,
            'all_balanced': all(check['balanced'] for check in all_checks),
        }


def inventory_cost(data, source_sha256=None):
    """Validate the request, replay every event and build the full report.

    ``data`` is the already-parsed strict JSON object and ``source_sha256`` is
    the hex digest of its exact source bytes (``None`` when called without a
    file). Returns a plain JSON-serialisable dict; raises an
    :class:`InventoryValidationError` subclass on any malformed input.
    """
    validated = validate_request(data)
    engine = _Engine(validated)
    for event in validated['events']:
        engine.replay(event)

    positions = engine.final_positions()
    trial = engine.trial_balance()
    reconciliations = engine.reconciliations(positions, trial)
    if not reconciliations['all_balanced']:  # pragma: no cover - invariant
        raise AssertionError('inventory-cost reconciliation failed')

    statistics = {
        'event_count': len(validated['events']),
        'stock_count': len(validated['stocks']),
        'receipt_count': engine.counters['receipt'],
        'issue_count': engine.counters['issue'],
        'return_count': engine.counters['return'],
        'reprice_count': engine.counters['reprice'],
        'attribution_count': len(engine.attributions),
        'posting_line_count': len(engine.posting_lines),
        'currencies': engine.currencies,
    }
    return {
        'schema_version': 1,
        'source_sha256': source_sha256,
        'events': engine.event_records,
        'physical_trace': engine.physical_trace,
        'cost_attributions': engine.attributions,
        'entries': engine.entries,
        'revaluations': engine.revaluations,
        'final_positions': positions,
        'trial_balance': trial,
        'reconciliations': reconciliations,
        'statistics': statistics,
    }


__all__ = ['inventory_cost', 'validate_request', 'prefix_cost',
           'INVENTORY', 'ISSUE_HOLDER']
