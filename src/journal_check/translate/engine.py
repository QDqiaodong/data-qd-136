"""Quote-evidence driven multi-currency translation engine.

The engine consumes one strict request of the shape

``{journal, target, rounding_account, entry_dates, quotes}``

and produces a single-currency trial balance together with the full quote
evidence behind every number. It is pure apart from no I/O at all: callers
handle the strict 8 MB JSON read and the atomic report publication.

Exact arithmetic
----------------
Every rate and every translated amount is a :class:`fractions.Fraction`;
there is no float anywhere. Rounding to the target minimal currency unit uses
round-half-to-even (banker's rounding) on the exact rational amount. Original
journal lines are translated and rounded individually and are never merged,
because merging first would change the rounding outcome.

Pending entries
---------------
Only entries whose currency is ``ready`` on their own date enter the target
ledger. Entries in an ``inconsistent`` or ``missing`` component go to the
pending list with their evidence; none of their lines or balancing
differences leak into the trial balance. Pending is an ordinary business
result (exit 0); malformed input raises :class:`TranslationValidationError`.
"""
import datetime
import re
from fractions import Fraction

from ..common import ident, rows
from ..journal import parse_journal
from . import rates
from .errors import TranslationValidationError

QUOTE_LIMIT = 2000
CURRENCY_LIMIT = 60
DATE_LIMIT = 366
QUOTE_FIELDS = ('id', 'base', 'quote', 'numerator', 'denominator',
                'valid_from', 'valid_to')
DATE_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
TOP_FIELDS = ('journal', 'target', 'rounding_account', 'entry_dates', 'quotes')


# ---------------------------------------------------------------------------
# Low-level validation helpers
# ---------------------------------------------------------------------------

def _is_int(value):
    # bool is a subclass of int and is explicitly forbidden everywhere.
    return type(value) is int


def _fail(message):
    raise TranslationValidationError(message)


def _validate_identifier(value, what):
    try:
        return ident(value)
    except ValueError as exc:
        _fail(str(exc) + ' for ' + what)


def _parse_date(value, what):
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        _fail(what + ' must be a real Gregorian YYYY-MM-DD date')
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        _fail(what + ' must be a real Gregorian YYYY-MM-DD date')


def _fraction_pair(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_request(data):
    """Validate and normalise the full translation request.

    The embedded journal first passes the original :func:`parse_journal`
    checks unchanged (original amounts, debit/credit balance and the duplicate
    account-line rules all keep their v1 semantics). Returns a normalised
    structure; generated zero amounts are never fed back into the journal
    validator.
    """
    if not isinstance(data, dict) or set(data) != set(TOP_FIELDS):
        _fail('expected object fields: ' + ' '.join(TOP_FIELDS))

    accounts, entries = parse_journal(data['journal'])
    entry_ids = [entry_id for entry_id, _currency, _lines in entries]
    currencies = {currency for _eid, currency, _lines in entries}

    target = _validate_identifier(data['target'], 'target')
    rounding_account = _validate_identifier(
        data['rounding_account'], 'rounding_account')
    if rounding_account not in accounts:
        _fail('rounding_account must be an account defined in the journal: '
              + rounding_account)

    entry_dates = _validate_entry_dates(data['entry_dates'], set(entry_ids))
    quotes = _validate_quotes(data['quotes'])

    # Every currency appearing in the journal, the target or the quotes
    # counts toward the global bound; bridge currencies used only inside
    # quote paths are legitimate even without journal entries.
    quote_currencies = {ccy for quote in quotes
                        for ccy in (quote['base'], quote['quote'])}
    all_currencies = currencies | {target} | quote_currencies
    if len(all_currencies) > CURRENCY_LIMIT:
        _fail('at most %d distinct currencies may appear' % CURRENCY_LIMIT)
    if len(set(entry_dates.values())) > DATE_LIMIT:
        _fail('at most %d distinct entry dates' % DATE_LIMIT)

    return {
        'accounts': dict(accounts),
        'entries': entries,
        'target': target,
        'rounding_account': rounding_account,
        'entry_dates': entry_dates,
        'quotes': quotes,
        'currencies': all_currencies,
    }


def _validate_entry_dates(raw, entry_ids):
    if not isinstance(raw, list) or len(raw) != len(entry_ids):
        _fail('entry_dates must contain exactly one date per journal entry')
    mapping = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {'entry', 'date'}:
            _fail('each entry_dates item must have exactly entry and date')
        entry_id = _validate_identifier(item['entry'], 'entry_dates entry')
        if entry_id not in entry_ids:
            _fail('entry_dates references unknown entry: ' + entry_id)
        if entry_id in mapping:
            _fail('duplicate entry_dates row for entry: ' + entry_id)
        day = _parse_date(item['date'], 'entry_dates date')
        mapping[entry_id] = day
    if set(mapping) != entry_ids:
        _fail('entry_dates must cover every journal entry exactly once')
    return mapping


def _validate_quotes(raw):
    quotes = rows(raw, QUOTE_LIMIT)
    seen_ids = set()
    normalised = []
    for quote in quotes:
        if not isinstance(quote, dict) or set(quote) != set(QUOTE_FIELDS):
            _fail('each quote must have exactly fields: ' + ' '.join(QUOTE_FIELDS))
        quote_id = _validate_identifier(quote['id'], 'quote id')
        if quote_id in seen_ids:
            _fail('duplicate quote id: ' + quote_id)
        seen_ids.add(quote_id)
        base = _validate_identifier(quote['base'], 'quote base')
        quoted = _validate_identifier(quote['quote'], 'quote quote')
        if base == quoted:
            _fail('quote %s base and quote currency must differ' % quote_id)
        numerator = quote['numerator']
        denominator = quote['denominator']
        if not _is_int(numerator) or not _is_int(denominator) or \
                not 1 <= numerator <= 10 ** 9 or \
                not 1 <= denominator <= 10 ** 9:
            _fail('quote %s numerator and denominator must be integers in '
                  '1..10^9 (booleans are forbidden)' % quote_id)
        valid_from = _parse_date(quote['valid_from'], 'quote valid_from')
        valid_to = _parse_date(quote['valid_to'], 'quote valid_to')
        if not valid_from < valid_to:
            _fail('quote %s validity range must be a non-empty half-open '
                  'interval [valid_from, valid_to)' % quote_id)
        normalised.append({
            'id': quote_id, 'base': base, 'quote': quoted,
            'numerator': numerator, 'denominator': denominator,
            'valid_from': valid_from, 'valid_to': valid_to,
        })
    return normalised


# ---------------------------------------------------------------------------
# Exact rounding
# ---------------------------------------------------------------------------

def round_half_even(value):
    """Round an exact :class:`Fraction` to an integer, half to even.

    Negative values round on their magnitude (the half case moves toward the
    even integer), matching standard banker's rounding. No float is involved.
    """
    floor = value.numerator // value.denominator
    remainder = value.numerator - floor * value.denominator
    doubled = 2 * remainder
    if doubled < value.denominator:
        return floor
    if doubled > value.denominator:
        return floor + 1
    return floor if floor % 2 == 0 else floor + 1


# ---------------------------------------------------------------------------
# Per-day quote resolution
# ---------------------------------------------------------------------------

def _active_quotes(quotes, day):
    return [quote for quote in quotes
            if quote['valid_from'] <= day < quote['valid_to']]


def resolve_quote_days(validated):
    """Resolve the quote graph independently for every distinct entry date."""
    quotes = validated['quotes']
    target = validated['target']
    entry_currency = {entry_id: currency
                      for entry_id, currency, _lines in validated['entries']}
    days = {}
    for entry_id, day in validated['entry_dates'].items():
        bucket = days.setdefault(day, set())
        bucket.add(entry_currency[entry_id])
    resolution = {}
    for day in sorted(days):
        active = _active_quotes(quotes, day)
        # Quote-only bridge currencies belong to the day's graph and must be
        # reported as well: their component may be inconsistent even though no
        # journal entry is denominated in them directly.
        active_currencies = {ccy for quote in active
                             for ccy in (quote['base'], quote['quote'])}
        resolution[day] = {
            'date': day.isoformat(),
            'active_quote_ids': sorted(quote['id'] for quote in active),
            'currencies': rates.resolve_day(
                active, target, days[day] | active_currencies),
        }
    return resolution


# ---------------------------------------------------------------------------
# Translation accounting
# ---------------------------------------------------------------------------

def _translate_entry(entry, day_record, target, rounding_account):
    """Translate one ready entry, keeping every original line separate.

    Duplicate account lines are deliberately not merged beforehand: each line
    is rounded on its own exact rational amount, so a line that rounds to zero
    and a repeated account line both keep an individual zero-based trace. The
    balancing difference, if any, becomes a single separately marked rounding
    line on ``rounding_account`` and is never merged with an original line.
    """
    entry_id, currency, lines = entry
    status = day_record['currencies'][currency]
    if status['state'] != rates.READY:  # pragma: no cover - caller guards
        _fail('entry %s is not ready on its date' % entry_id)
    rate = Fraction(status['rate']['numerator'], status['rate']['denominator'])
    evidence = {'state': rates.READY, 'rate': status['rate'],
                'path': status['path']}

    line_traces = []
    debit_sum = 0
    credit_sum = 0
    for line_idx, (account, side, original_amount) in enumerate(lines):
        exact = Fraction(original_amount) * rate
        rounded = round_half_even(exact)
        if side == 'debit':
            debit_sum += rounded
        else:
            credit_sum += rounded
        line_traces.append({
            'entry': entry_id,
            'line': line_idx,
            'account': account,
            'side': side,
            'original_currency': currency,
            'original_amount': original_amount,
            'rate_evidence': evidence,
            'exact_amount': _fraction_pair(exact),
            'rounded_amount': rounded,
            'kind': 'original',
        })

    difference = debit_sum - credit_sum
    appended = None
    if difference > 0:
        appended = {'entry': entry_id, 'account': rounding_account,
                    'side': 'credit', 'amount': difference,
                    'kind': 'rounding', 'line': len(lines)}
    elif difference < 0:
        appended = {'entry': entry_id, 'account': rounding_account,
                    'side': 'debit', 'amount': -difference,
                    'kind': 'rounding', 'line': len(lines)}
    return line_traces, appended


def _trial_balance(accounts, posted_lines, rounding_lines):
    """Aggregate translated non-zero original lines and rounding lines."""
    totals = {account: {'debit': 0, 'credit': 0} for account in accounts}
    for trace in posted_lines:
        amount = trace['rounded_amount']
        if amount == 0:
            continue  # zero-rounded originals stay in the trace, not the TB
        totals[trace['account']][trace['side']] += amount
    for line in rounding_lines:
        totals[line['account']][line['side']] += line['amount']

    rows_out = []
    total_debits = 0
    total_credits = 0
    for account in sorted(accounts):
        debit = totals[account]['debit']
        credit = totals[account]['credit']
        net = debit - credit
        normal = accounts[account]
        rows_out.append({
            'account': account,
            'debit': debit,
            'credit': credit,
            'net_debit': max(net, 0),
            'net_credit': max(-net, 0),
            'normal_balance': net if normal == 'debit' else -net,
        })
        total_debits += debit
        total_credits += credit
    if total_debits != total_credits:  # pragma: no cover - invariant
        raise AssertionError('translated trial balance is not balanced')
    return {
        'currency': None,  # filled by the caller with the target currency
        'accounts': rows_out,
        'total_debits': total_debits,
        'total_credits': total_credits,
        'net_debits': sum(row['net_debit'] for row in rows_out),
        'net_credits': sum(row['net_credit'] for row in rows_out),
    }


def translate(data, source_sha256=None):
    """Validate the request and build the full translation report.

    ``data`` is the already-parsed strict JSON object and ``source_sha256`` is
    the hex digest of its exact source bytes (``None`` when called without a
    file). Returns a plain JSON-serialisable dict; raises a
    :class:`TranslationValidationError` subclass on any malformed input.
    """
    validated = validate_request(data)
    target = validated['target']
    rounding_account = validated['rounding_account']
    accounts = validated['accounts']
    day_records = resolve_quote_days(validated)

    entry_currency = {entry_id: currency
                      for entry_id, currency, _lines in validated['entries']}
    qualifications = []
    posted_lines = []
    rounding_lines = []
    entry_balances = []
    pending = []
    counts_by_currency = {}
    counts_by_state = {rates.READY: 0, rates.MISSING: 0,
                       rates.INCONSISTENT: 0}

    for entry in validated['entries']:
        entry_id, currency, lines = entry
        day = validated['entry_dates'][entry_id]
        day_record = day_records[day]
        status = day_record['currencies'][currency]
        state = status['state']
        counts_by_state[state] += 1
        bucket = counts_by_currency.setdefault(
            currency, {rates.READY: 0, rates.MISSING: 0,
                       rates.INCONSISTENT: 0})
        bucket[state] += 1

        qualification = {'entry': entry_id, 'date': day.isoformat(),
                         'original_currency': currency, 'state': state}
        if state == rates.READY:
            qualification['rate'] = status['rate']
        qualifications.append(qualification)

        if state != rates.READY:
            reason = {'state': state,
                      'date': day.isoformat(),
                      'currency': currency}
            if state == rates.INCONSISTENT:
                reason['witness'] = status['witness']
            else:
                reason['reachable_currencies'] = \
                    status['reachable_currencies']
            pending.append({'entry': entry_id, 'date': day.isoformat(),
                            'original_currency': currency, 'reason': reason,
                            'line_count': len(lines)})
            continue

        line_traces, appended = _translate_entry(
            entry, day_record, target, rounding_account)
        posted_lines.extend(line_traces)
        if appended is not None:
            rounding_lines.append(appended)
        entry_debits = sum(trace['rounded_amount']
                           for trace in line_traces
                           if trace['side'] == 'debit')
        entry_credits = sum(trace['rounded_amount']
                            for trace in line_traces
                            if trace['side'] == 'credit')
        if appended is not None:
            if appended['side'] == 'debit':
                entry_debits += appended['amount']
            else:
                entry_credits += appended['amount']
        if entry_debits != entry_credits:  # pragma: no cover - invariant
            raise AssertionError('translated entry %s does not balance'
                                 % entry_id)
        entry_balances.append({'entry': entry_id, 'date': day.isoformat(),
                               'debit': entry_debits, 'credit': entry_credits,
                               'balanced': True})

    trial = _trial_balance(accounts, posted_lines, rounding_lines)
    trial['currency'] = target

    quote_resolution = [
        {'date': day_records[day]['date'],
         'active_quote_ids': day_records[day]['active_quote_ids'],
         'currencies': [day_records[day]['currencies'][ccy]
                        for ccy in sorted(day_records[day]['currencies'])]}
        for day in sorted(day_records)
    ]

    # Canonical entry order makes every report independent of the input
    # arrangement. Lines within an entry keep their zero-based line order;
    # rounding lines sort by (entry, line) alongside the origin they belong to.
    qualifications.sort(key=lambda row: row['entry'])
    pending.sort(key=lambda row: row['entry'])
    entry_balances.sort(key=lambda row: row['entry'])
    posted_lines.sort(key=lambda trace: (trace['entry'], trace['line'],
                                         trace['account'], trace['side']))
    rounding_lines.sort(key=lambda row: (row['entry'], row['line'],
                                         row['account'], row['side']))

    posted_entry_ids = {row['entry'] for row in entry_balances}
    pending_entry_ids = {row['entry'] for row in pending}
    all_entry_ids = set(entry_currency)
    if posted_entry_ids | pending_entry_ids != all_entry_ids or \
            posted_entry_ids & pending_entry_ids:
        raise AssertionError('every source entry must appear exactly once')

    return {
        'schema_version': 1,
        'source_sha256': source_sha256,
        'target_currency': target,
        'rounding_account': rounding_account,
        'quote_resolution': quote_resolution,
        'entry_qualification': qualifications,
        'line_trace': posted_lines,
        'rounding_lines': rounding_lines,
        'entry_balances': entry_balances,
        'pending_entries': pending,
        'trial_balance': trial,
        'statistics': {
            'entry_count': len(validated['entries']),
            'posted_count': len(entry_balances),
            'pending_count': len(pending),
            'by_state': counts_by_state,
            'by_original_currency': [
                {'currency': currency, **counts_by_currency[currency]}
                for currency in sorted(counts_by_currency)
            ],
        },
    }


__all__ = ['translate', 'validate_request', 'resolve_quote_days',
           'round_half_even', 'QUOTE_LIMIT', 'CURRENCY_LIMIT', 'DATE_LIMIT',
           'TranslationValidationError']
