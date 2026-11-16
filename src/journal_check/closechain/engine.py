"""Pure close-chain computation: validation, carry-forward and period closes.

Everything in this module works on plain Python objects and integers; no file
is touched, so the whole algorithm is directly testable. Only the Python
standard library is used.

Balance convention
------------------
Every amount is a signed integer with debit positive and credit negative.
The sign is *never* inferred from ``normal_balance``: actual journal side
directions decide signs, so an abnormal (opposite-direction) balance is
handled naturally. Currencies are always computed independently; balances of
different currencies are never netted against each other.

Determinism
-----------
All iteration orders are canonical (sorted ids), so results do not depend on
the order in which the caller lists entries, roles, incoming rows or check
items.
"""
import hashlib
import json

from ..common import ident
from ..journal import parse_journal
from .errors import (
    CloseChainError,
    ConflictError,
    LimitError,
    ValidationError,
)

MAX_PERIODS = 60
MAX_WEIGHT = 10 ** 6
MAX_CHECK_AMOUNT = 10 ** 30

PERIOD_FIELDS = ('id', 'journal', 'roles', 'retained', 'incoming',
                 'opening_checks')
TOP_LEVEL_FIELDS = ('chain_id', 'expected_head', 'start', 'periods')
ROLE_KINDS = ('carry', 'result')


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------

def canonical_bytes(obj):
    """Stable serialization used both for storage and for SHA256 hashing."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _is_int(value):
    # bool is a subclass of int and is explicitly forbidden everywhere.
    return type(value) is int


def _require_exact(obj, fields, what):
    if isinstance(fields, str):
        fields = tuple(fields.split())
    if not isinstance(obj, dict) or set(obj) != set(fields):
        raise ValidationError(
            '%s must be an object with exactly fields: %s'
            % (what, ' '.join(fields)))


def _validate_identifier(value, what):
    try:
        return ident(value)
    except ValueError as exc:
        raise ValidationError(str(exc) + ' for ' + what) from None


def _validate_generation_id(value, what):
    if not isinstance(value, str) or len(value) != 64 or \
            any(c not in '0123456789abcdef' for c in value):
        raise ValidationError(what + ' must be a 64 character lowercase hex id')
    return value


# ---------------------------------------------------------------------------
# Request-level validation
# ---------------------------------------------------------------------------

def validate_request(request):
    """Validate the top-level request shape; returns normalized scalars."""
    _require_exact(request, TOP_LEVEL_FIELDS, 'request')
    chain_id = _validate_identifier(request['chain_id'], 'chain_id')
    expected_head = request['expected_head']
    if expected_head is not None:
        _validate_generation_id(expected_head, 'expected_head')
    start = request['start']
    if not _is_int(start) or start < 1:
        raise ValidationError('start must be a positive 1-based integer')
    periods = request['periods']
    if not isinstance(periods, list) or not periods:
        raise ValidationError('periods must be a non-empty replacement fragment')
    if len(periods) > MAX_PERIODS:
        raise LimitError('a chain holds at most %d periods' % MAX_PERIODS)
    return chain_id, expected_head, start, periods


# ---------------------------------------------------------------------------
# Single-period parsing (cross-period-independent checks only)
# ---------------------------------------------------------------------------

def parse_period(raw):
    """Parse and validate one period source into a normalized structure.

    Cross-period checks (incoming references, opening-check currency set) are
    applied later, when the surrounding chain is known.
    """
    _require_exact(raw, PERIOD_FIELDS, 'period')
    period_id = _validate_identifier(raw['id'], 'period id')
    accounts, entries = parse_journal(raw['journal'])

    roles_raw = raw['roles']
    if not isinstance(roles_raw, list) or len(roles_raw) != len(accounts):
        raise ValidationError(
            'period %s: roles must list every defined account exactly once'
            % period_id)
    roles = {}
    for item in roles_raw:
        _require_exact(item, 'account kind', 'role')
        account = _validate_identifier(item['account'], 'role account')
        kind = item['kind']
        if not isinstance(kind, str) or kind not in ROLE_KINDS:
            raise ValidationError(
                'period %s: role kind must be carry or result' % period_id)
        if account not in accounts:
            raise ValidationError(
                'period %s: role references unknown account %s'
                % (period_id, account))
        if account in roles:
            raise ValidationError(
                'period %s: account %s has more than one role'
                % (period_id, account))
        roles[account] = kind
    if set(roles) != set(accounts):
        raise ValidationError(
            'period %s: roles must cover every defined account exactly once'
            % period_id)

    retained = raw['retained']
    retained = _validate_identifier(retained, 'retained') if retained is not \
        None else None
    if retained not in accounts:
        raise ValidationError(
            'period %s: retained references unknown account %s'
            % (period_id, retained))
    if roles[retained] != 'carry' or accounts[retained] != 'credit':
        raise ValidationError(
            'period %s: retained must be a carry account with normal=credit'
            % period_id)

    incoming = _parse_incoming(period_id, raw['incoming'], accounts, roles)
    checks = _parse_opening_checks(period_id, raw['opening_checks'], accounts,
                                   roles)
    return {
        'id': period_id,
        'journal': raw['journal'],
        'accounts': dict(accounts),
        'entries': list(entries),
        'roles': dict(roles),
        'retained': retained,
        'incoming': incoming,
        'opening_checks': checks,
    }


def _parse_incoming(period_id, raw, accounts, roles):
    # Only structural, current-period checks live here. Whether a source
    # account existed as a carry account in the *previous* period can only be
    # decided once the surrounding chain is known.
    if not isinstance(raw, list):
        raise ValidationError('period %s: incoming must be an array' % period_id)
    incoming = []
    seen_sources = set()
    for row in raw:
        _require_exact(row, 'account targets', 'incoming row')
        source = _validate_identifier(row['account'], 'incoming source account')
        if source in seen_sources:
            raise ValidationError(
                'period %s: incoming source %s listed more than once'
                % (period_id, source))
        seen_sources.add(source)
        targets_raw = row['targets']
        if not isinstance(targets_raw, list) or not targets_raw:
            raise ValidationError(
                'period %s: incoming targets for %s must be non-empty'
                % (period_id, source))
        targets = []
        seen_targets = set()
        for target in targets_raw:
            _require_exact(target, 'account weight', 'incoming target')
            target_id = _validate_identifier(target['account'],
                                             'incoming target account')
            if target_id not in accounts:
                raise ValidationError(
                    'period %s: incoming references unknown target account %s'
                    % (period_id, target_id))
            if roles[target_id] != 'carry':
                raise ValidationError(
                    'period %s: incoming target %s must be a carry account'
                    % (period_id, target_id))
            weight = target['weight']
            if not _is_int(weight) or not 1 <= weight <= MAX_WEIGHT:
                raise ValidationError(
                    'period %s: incoming weight must be an integer in 1..10^6'
                    % period_id)
            if target_id in seen_targets:
                raise ValidationError(
                    'period %s: duplicate incoming target %s in one source row'
                    % (period_id, target_id))
            seen_targets.add(target_id)
            targets.append({'account': target_id, 'weight': weight})
        incoming.append({'account': source,
                         'targets': sorted(targets,
                                           key=lambda t: t['account'])})
    incoming.sort(key=lambda r: r['account'])
    return incoming


def _parse_opening_checks(period_id, raw, accounts, roles):
    if not isinstance(raw, list):
        raise ValidationError(
            'period %s: opening_checks must be an array' % period_id)
    carry_accounts = {a for a, kind in roles.items() if kind == 'carry'}
    checks = []
    seen = set()
    for item in raw:
        _require_exact(item, 'currency account amount', 'opening check')
        currency = _validate_identifier(item['currency'], 'check currency')
        account = _validate_identifier(item['account'], 'check account')
        amount = item['amount']
        if not _is_int(amount) or abs(amount) > MAX_CHECK_AMOUNT:
            raise ValidationError(
                'period %s: check amount must be a signed integer with '
                'abs(value) <= 10^30 (booleans are forbidden)' % period_id)
        if account not in accounts or account not in carry_accounts:
            raise ValidationError(
                'period %s: opening check account %s must be a carry account'
                % (period_id, account))
        key = (currency, account)
        if key in seen:
            raise ValidationError(
                'period %s: duplicate opening check for currency %s account %s'
                % (period_id, currency, account))
        seen.add(key)
        checks.append({'currency': currency, 'account': account,
                       'amount': amount})
    checks.sort(key=lambda c: (c['currency'], c['account']))
    return checks


# ---------------------------------------------------------------------------
# Largest-remainder allocation (exact integer arithmetic only)
# ---------------------------------------------------------------------------

def allocate(amount, targets):
    """Split the signed integer ``amount`` across weighted targets.

    ``targets`` is a list of ``(account, weight)`` pairs. The absolute source
    amount is distributed proportionally to the integer weights: every target
    first receives its floored integer share, then the remaining units are
    handed out one at a time in descending remainder order, ties broken by
    ascending target id. The original sign is restored afterwards. No float
    is used and no per-unit loop is performed, so 10^12 amounts (and larger)
    are cheap. Returns ``(allocations, remainder_basis)`` where allocations
    maps account id to the signed integer share and remainder_basis lists the
    exact rational remainders behind the tie-break.
    """
    if not targets:
        raise ValidationError('incoming target list must be non-empty')
    sign = -1 if amount < 0 else 1
    magnitude = abs(amount)
    total_weight = sum(weight for _, weight in targets)
    floors = []
    remainders = []
    for account, weight in targets:
        product = magnitude * weight
        floor = product // total_weight
        # Exact rational remainder: numerator / total_weight, kept as a pair.
        remainder_num = product - floor * total_weight
        floors.append((account, floor))
        remainders.append((account, remainder_num))
    left = magnitude - sum(floor for _, floor in floors)
    order = sorted(remainders, key=lambda pair: (-pair[1], pair[0]))
    extra = {account: 0 for account, _ in floors}
    for account, remainder_num in order:
        if left == 0:
            break
        extra[account] += 1
        left -= 1
    if left != 0:  # pragma: no cover - arithmetic invariant
        raise CloseChainError('largest-remainder allocation lost units')
    allocations = {}
    basis = []
    for (account, floor), (_, remainder_num) in zip(floors, remainders):
        share = sign * (floor + extra[account])
        allocations[account] = share
        basis.append({
            'account': account,
            'weight': dict(targets)[account],
            'floor_share': floor,
            'remainder_numerator': remainder_num,
            'remainder_denominator': total_weight,
            'extra_unit': extra[account],
            'allocated_magnitude': floor + extra[account],
        })
    basis.sort(key=lambda b: b['account'])
    return allocations, basis


# ---------------------------------------------------------------------------
# Chain assembly and cross-period validation
# ---------------------------------------------------------------------------

def assemble_chain(head_periods, fragment, start):
    """Return the full candidate chain as normalized period sources.

    ``head_periods`` are the normalized sources of the current head (empty for
    a brand new store); ``fragment`` is the validated replacement fragment and
    ``start`` its 1-based insertion position. Existing positions covered by the
    fragment keep their period id; positions past the chain tail append
    contiguously and the old suffix is never implicitly deleted.
    """
    parsed_fragment = [parse_period(raw) for raw in fragment]
    candidate = list(head_periods)
    if start > len(candidate) + 1:
        raise ValidationError(
            'start %d leaves a gap; chain currently has %d periods'
            % (start, len(candidate)))
    for offset, period in enumerate(parsed_fragment):
        position = start + offset
        if position <= len(candidate):
            if candidate[position - 1]['id'] != period['id']:
                raise ConflictError(
                    'period id at position %d must stay %s when overwriting '
                    'an existing position (got %s)'
                    % (position, candidate[position - 1]['id'], period['id']))
            candidate[position - 1] = period
        else:
            if position != len(candidate) + 1:  # pragma: no cover - guarded above
                raise ValidationError('replacement fragment is not contiguous')
            candidate.append(period)
    if len(candidate) > MAX_PERIODS:
        raise LimitError('a chain holds at most %d periods' % MAX_PERIODS)
    ids = [period['id'] for period in candidate]
    if len(set(ids)) != len(ids):
        raise ValidationError('period ids must be unique across the whole chain')
    return candidate


def _validate_incoming_chain(period, index, prior):
    """Apply cross-period incoming rules for the period at chain index."""
    incoming = period['incoming']
    if index == 0:
        if incoming:
            raise ValidationError(
                'period %s: the first period must have an empty incoming list'
                % period['id'])
        return
    prior_carry = {a for a, kind in prior['roles'].items() if kind == 'carry'}
    sources = {row['account'] for row in incoming}
    if sources != prior_carry:
        missing = sorted(prior_carry - sources)
        extra = sorted(sources - prior_carry)
        detail = []
        if missing:
            detail.append('missing sources %s' % missing)
        if extra:
            detail.append('unknown or non-carry sources %s' % extra)
        raise ValidationError(
            'period %s: incoming must list every prior carry account exactly '
            'once (%s)' % (period['id'], '; '.join(detail)))
    current_carry = {a for a, kind in period['roles'].items()
                     if kind == 'carry'}
    for row in incoming:
        for target in row['targets']:
            if target['account'] not in current_carry:
                raise ValidationError(
                    'period %s: incoming target %s is not a current carry '
                    'account' % (period['id'], target['account']))


# ---------------------------------------------------------------------------
# Period computation
# ---------------------------------------------------------------------------

def _journal_movements(period):
    """Signed debit/credit movement totals per currency and account."""
    movements = {}
    currencies = set()
    for _entry_id, currency, lines in period['entries']:
        currencies.add(currency)
        bucket = movements.setdefault(
            currency, {a: {'debit': 0, 'credit': 0}
                       for a in period['accounts']})
        for account, side, amount in lines:
            bucket[account][side] += amount
    return movements, currencies


def _apply_incoming(period, prior_close, currencies):
    """Build this period's opening balances from the prior carry balances."""
    opening = {currency: {a: 0 for a in period['accounts']}
               for currency in currencies}
    rows = []
    for row in period['incoming']:
        source = row['account']
        per_currency = []
        for currency in sorted(currencies):
            source_amount = prior_close[currency].get(source, 0)
            targets = [(t['account'], t['weight']) for t in row['targets']]
            allocations, basis = allocate(source_amount, targets)
            conserved = sum(allocations.values()) == source_amount
            for target, share in allocations.items():
                opening[currency][target] += share
            per_currency.append({
                'currency': currency,
                'source_amount': source_amount,
                'allocations': [
                    {'account': t['account'], 'weight': t['weight'],
                     'amount': allocations[t['account']]}
                    for t in row['targets']
                ],
                'remainder_basis': basis,
                'conserved': conserved,
            })
        rows.append({'account': source, 'currencies': per_currency})
    rows.sort(key=lambda r: r['account'])
    return opening, rows


def _assert_conservation(label, currency, balances, movements, opening,
                         period_id):
    """Per-currency debit/credit conservation, never across currencies.

    Signed balance totals must equal signed opening totals plus signed
    movement totals; the retained adjustment only moves sums between accounts
    and cannot change the currency total either.
    """
    signed_movement = sum(
        movements[a]['debit'] - movements[a]['credit'] for a in balances)
    if sum(balances.values()) != sum(opening.values()) + signed_movement:
        raise CloseChainError(
            'period %s currency %s: %s conservation broken; cross-currency '
            'netting is never allowed' % (period_id, currency, label))


def compute_period(period, index, prior_result):
    """Compute one period's full close result.

    ``prior_result`` is the previous period's result (``None`` for the first
    period). Returns the result envelope stored alongside the period source.
    """
    movements, journal_currencies = _journal_movements(period)
    if index == 0:
        inherited_currencies = set()
    else:
        inherited_currencies = set(prior_result['currency_order'])
    currencies = inherited_currencies | journal_currencies

    if index == 0:
        opening = {currency: {a: 0 for a in period['accounts']}
                   for currency in currencies}
        incoming_report = []
    else:
        prior_close = {
            block['currency']: {
                row['account']: row['closing_after'] for row in block['accounts']
            }
            for block in prior_result['currencies']
        }
        # A currency introduced by this period's journal has no prior block;
        # every carried source therefore opens at zero for that currency.
        for currency in currencies:
            prior_close.setdefault(currency, {})
        opening, incoming_report = _apply_incoming(
            period, prior_close, currencies)

    check_violation = _verify_checks(period, currencies, opening)
    if check_violation is not None:
        raise ValidationError(check_violation)

    result_accounts = sorted(
        a for a, kind in period['roles'].items() if kind == 'result')
    retained = period['retained']
    currency_blocks = []
    for currency in sorted(currencies):
        movement = movements.get(
            currency, {a: {'debit': 0, 'credit': 0}
                       for a in period['accounts']})
        account_rows = []
        pre_closing = {}
        for account in sorted(period['accounts']):
            move = movement[account]
            opening_value = opening[currency][account]
            pre = opening_value + move['debit'] - move['credit']
            pre_closing[account] = pre
            account_rows.append({
                'account': account,
                'kind': period['roles'][account],
                'opening': opening_value,
                'movement_debit': move['debit'],
                'movement_credit': move['credit'],
                'closing_before': pre,
            })
        _assert_conservation('pre-adjustment', currency, pre_closing,
                             movement, opening[currency], period['id'])

        clearings = []
        result_sum = 0
        for account in result_accounts:
            clearing = pre_closing[account]
            result_sum += clearing
            clearings.append({'account': account,
                              'closing_before': pre_closing[account],
                              'cleared_amount': clearing,
                              'closing_after': 0})
        post_closing = dict(pre_closing)
        for account in result_accounts:
            post_closing[account] = 0
        retained_before = pre_closing[retained]
        retained_transfer = result_sum
        post_closing[retained] = retained_before + retained_transfer
        for row in account_rows:
            account = row['account']
            row['closing_after'] = post_closing[account]
        _assert_conservation('post-adjustment', currency, post_closing,
                             movement, opening[currency], period['id'])

        currency_blocks.append({
            'currency': currency,
            'accounts': account_rows,
            'result_clearings': clearings,
            'retained_transfer': {
                'account': retained,
                'closing_before': retained_before,
                'transfer_amount': retained_transfer,
                'closing_after': post_closing[retained],
            },
        })

    return {
        'period': period['id'],
        'currency_order': sorted(currencies),
        'accounts': sorted(period['accounts']),
        'currencies': currency_blocks,
        'incoming': incoming_report,
        'opening_checks': list(period['opening_checks']),
    }


def _verify_checks(period, currencies, opening):
    expected_keys = {
        (currency, account)
        for currency in currencies
        for account, kind in period['roles'].items()
        if kind == 'carry'
    }
    actual_checks = {(c['currency'], c['account']): c['amount']
                     for c in period['opening_checks']}
    actual_keys = set(actual_checks)
    if actual_keys != expected_keys:
        duplicate = len(actual_keys) != len(period['opening_checks'])
        if duplicate:
            return ('period %s: opening_checks contains a duplicate '
                    'currency/account key' % period['id'])
        first = sorted(expected_keys ^ actual_keys)[0]
        if first in expected_keys:
            return ('period %s: opening_checks is missing the required '
                    '(possibly zero) key currency=%s account=%s'
                    % (period['id'], first[0], first[1]))
        return ('period %s: opening_checks contains unexpected key '
                'currency=%s account=%s' % (period['id'], first[0], first[1]))
    for currency, account in sorted(expected_keys):
        expected = opening[currency][account]
        given = actual_checks[(currency, account)]
        if given != expected:
            return ('period %s: opening check mismatch at currency=%s '
                    'account=%s: expected %d but control figure is %d'
                    % (period['id'], currency, account, expected, given))
    return None


def compute_chain(periods):
    """Compute every period in order; returns the list of result envelopes."""
    results = []
    prior = None
    for index, period in enumerate(periods):
        _validate_incoming_chain(period, index,
                                 periods[index - 1] if index else None)
        result = compute_period(period, index, prior)
        results.append(result)
        prior = result
    return results


# ---------------------------------------------------------------------------
# Full candidate preparation used by the store
# ---------------------------------------------------------------------------

def prepare_candidate(head_periods, request):
    """Validate a request against the current head and build the candidate.

    Returns ``(chain_id, expected_head, candidate_sources, candidate_results)``
    without performing any I/O.
    """
    chain_id, expected_head, start, fragment = validate_request(request)
    candidate_sources = assemble_chain(head_periods, fragment, start)
    candidate_results = compute_chain(candidate_sources)
    return chain_id, expected_head, candidate_sources, candidate_results


def generation_id(chain_id, parent, sources, request_sha256):
    """Derive the deterministic immutable generation id."""
    payload = {
        'chain_id': chain_id,
        'parent': parent,
        'request_sha256': request_sha256,
        'periods': [
            {'id': source['id'], 'source': canonical_bytes(source).hex()}
            for source in sources
        ],
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


__all__ = [
    'MAX_PERIODS', 'MAX_WEIGHT', 'MAX_CHECK_AMOUNT',
    'canonical_bytes', 'validate_request', 'parse_period', 'allocate',
    'assemble_chain', 'compute_period', 'compute_chain',
    'prepare_candidate', 'generation_id',
]
