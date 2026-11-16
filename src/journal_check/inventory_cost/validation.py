"""Strict request validation for the inventory-cost workbench.

The shape, identifier, field, bound and reference rules live here so the
engine can assume well-formed records. Two kinds of checks are deliberately
split:

* structural validation (this module): exact field sets, identifiers, integer
  bounds with booleans explicitly rejected, duplicate ids/seq, account and
  pool definitions and the disjoint inventory/payable account rule;
* sequential legality (engine): future references, issuing from an empty pool,
  cross-pool references, over-returns and insufficient available units. These
  need the physical state built by processing events in ``seq`` order.

Every failure raises :class:`InventoryValidationError`, a :class:`ValueError`
subclass; any single illegal event rejects the whole request.
"""
from ..common import ident, rows
from .errors import InventoryValidationError

STOCK_LIMIT = 1000
EVENT_LIMIT = 5000
ACCOUNT_LIMIT = 1000
QUANTITY_MAX = 10 ** 12
COST_MAX = 10 ** 18
SEQ_MAX = 10 ** 9

TOP_FIELDS = ('accounts', 'stocks', 'events')
STOCK_FIELDS = ('sku', 'currency', 'inventory_account', 'payable_account')
COMMON_EVENT_FIELDS = ('id', 'seq', 'type')
EVENT_FIELDS = {
    'receipt': ('id', 'seq', 'type', 'sku', 'currency', 'quantity',
                'total_cost'),
    'issue': ('id', 'seq', 'type', 'sku', 'currency', 'quantity',
              'expense_account'),
    'return': ('id', 'seq', 'type', 'issue', 'quantity'),
    'reprice': ('id', 'seq', 'type', 'receipt', 'total_cost'),
}
EVENT_TYPES = tuple(EVENT_FIELDS)


def _fail(message):
    raise InventoryValidationError(message)


def is_int(value):
    """``bool`` is an ``int`` subclass and is explicitly forbidden everywhere."""
    return type(value) is int


def _bounded_int(value, minimum, maximum, what):
    if not is_int(value) or not minimum <= value <= maximum:
        _fail(what + ' must be an integer in %d..%d (booleans are forbidden)'
              % (minimum, maximum))
    return value


def _identifier(value, what):
    try:
        return ident(value)
    except ValueError as exc:
        _fail(str(exc) + ' for ' + what)


def validate_accounts(raw_accounts):
    """Validate account definitions using the existing v1 account rules.

    Mirrors :func:`journal_check.journal.parse_journal`'s account loop exactly:
    1..1000 unique ids, each with ``normal`` in ``debit``/``credit``.
    """
    accounts = rows(raw_accounts, ACCOUNT_LIMIT)
    if not isinstance(accounts, list) or not accounts:
        _fail('accounts required')
    normal_by_account = {}
    for raw in accounts:
        if not isinstance(raw, dict) or set(raw) != {'id', 'normal'}:
            _fail('each account must have exactly id and normal')
        account_id = _identifier(raw['id'], 'account id')
        normal = _identifier(raw['normal'], 'account normal')
        if account_id in normal_by_account or normal not in ('debit',
                                                              'credit'):
            _fail('duplicate account or invalid normal side: ' + account_id)
        normal_by_account[account_id] = normal
    return normal_by_account


def validate_stocks(raw_stocks, accounts):
    """Validate the stock pool definitions and return normalised records.

    An empty list is legal (mirroring v1's empty-entries case); with no pools
    every later event necessarily fails its pool reference in the engine.
    """
    stocks = rows(raw_stocks, STOCK_LIMIT)
    if not isinstance(stocks, list):
        _fail('stocks must be an array')
    normalised = []
    seen_pools = set()
    inventory_accounts = set()
    payable_accounts = set()
    for raw in stocks:
        if not isinstance(raw, dict) or set(raw) != set(STOCK_FIELDS):
            _fail('each stock must have exactly fields: '
                  + ' '.join(STOCK_FIELDS))
        sku = _identifier(raw['sku'], 'stock sku')
        currency = _identifier(raw['currency'], 'stock currency')
        inventory_account = _identifier(raw['inventory_account'],
                                        'inventory_account')
        payable_account = _identifier(raw['payable_account'],
                                      'payable_account')
        if inventory_account not in accounts:
            _fail('inventory_account must be a defined account: '
                  + inventory_account)
        if payable_account not in accounts:
            _fail('payable_account must be a defined account: '
                  + payable_account)
        pool = (sku, currency)
        if pool in seen_pools:
            _fail('duplicate stock pool (sku, currency): %s/%s' % pool)
        seen_pools.add(pool)
        inventory_accounts.add(inventory_account)
        payable_accounts.add(payable_account)
        normalised.append({
            'sku': sku, 'currency': currency,
            'inventory_account': inventory_account,
            'payable_account': payable_account,
        })
    # The two account families must be disjoint; within either family several
    # pools may legitimately share one account.
    overlap = inventory_accounts & payable_accounts
    if overlap:
        _fail('an account cannot be both an inventory and a payable account: '
              + ', '.join(sorted(overlap)))
    return normalised, inventory_accounts, payable_accounts


def validate_events(raw_events):
    """Validate event field sets, ids, seq, types and numeric bounds."""
    events = rows(raw_events, EVENT_LIMIT)
    if not isinstance(events, list):
        _fail('events must be an array')
    normalised = []
    seen_event_ids = set()
    seen_seq = set()
    for raw in events:
        if not isinstance(raw, dict):
            _fail('each event must be an object')
        if not set(raw) >= set(COMMON_EVENT_FIELDS):
            _fail('each event must have exactly id, seq and type-specific '
                  'fields')
        event_id = _identifier(raw['id'], 'event id')
        if event_id in seen_event_ids:
            _fail('duplicate event id: ' + event_id)
        seen_event_ids.add(event_id)
        seq = _bounded_int(raw['seq'], 0, SEQ_MAX, 'seq')
        if seq in seen_seq:
            _fail('duplicate event seq: ' + str(seq))
        seen_seq.add(seq)
        event_type = _identifier(raw['type'], 'event type')
        if event_type not in EVENT_FIELDS:
            _fail('unknown event type: ' + event_type)
        if set(raw) != set(EVENT_FIELDS[event_type]):
            _fail('event %s must have exactly fields: %s'
                  % (event_id, ' '.join(EVENT_FIELDS[event_type])))
        record = {'id': event_id, 'seq': seq, 'type': event_type}
        if event_type == 'receipt':
            record['sku'] = _identifier(raw['sku'], 'receipt sku')
            record['currency'] = _identifier(raw['currency'],
                                             'receipt currency')
            record['quantity'] = _bounded_int(
                raw['quantity'], 1, QUANTITY_MAX, 'quantity')
            record['total_cost'] = _bounded_int(
                raw['total_cost'], 0, COST_MAX, 'total_cost')
        elif event_type == 'issue':
            record['sku'] = _identifier(raw['sku'], 'issue sku')
            record['currency'] = _identifier(raw['currency'],
                                             'issue currency')
            record['quantity'] = _bounded_int(
                raw['quantity'], 1, QUANTITY_MAX, 'quantity')
            record['expense_account'] = _identifier(
                raw['expense_account'], 'expense_account')
        elif event_type == 'return':
            record['issue'] = _identifier(raw['issue'], 'return issue')
            record['quantity'] = _bounded_int(
                raw['quantity'], 1, QUANTITY_MAX, 'quantity')
        else:  # reprice
            record['receipt'] = _identifier(raw['receipt'],
                                            'reprice receipt')
            record['total_cost'] = _bounded_int(
                raw['total_cost'], 0, COST_MAX, 'total_cost')
        normalised.append(record)
    # Processing order is seq order, never the input arrangement.
    normalised.sort(key=lambda event: event['seq'])
    return normalised


def validate_request(data):
    """Validate the full request and return a normalised, seq-sorted structure.

    Referential and physical legality that requires event-order state (pool
    existence at issue time, future references, over-returns, insufficient
    stock, expense-account family rules) is enforced by the engine while it
    replays the seq-sorted events.
    """
    if not isinstance(data, dict) or set(data) != set(TOP_FIELDS):
        _fail('expected object fields: ' + ' '.join(TOP_FIELDS))
    accounts = validate_accounts(data['accounts'])
    stocks, inventory_accounts, payable_accounts = validate_stocks(
        data['stocks'], accounts)
    events = validate_events(data['events'])
    return {
        'accounts': accounts,
        'stocks': stocks,
        'events': events,
        'inventory_accounts': inventory_accounts,
        'payable_accounts': payable_accounts,
    }


__all__ = ['validate_request', 'validate_accounts', 'validate_stocks',
           'validate_events', 'is_int', 'STOCK_LIMIT', 'EVENT_LIMIT',
           'ACCOUNT_LIMIT', 'QUANTITY_MAX', 'COST_MAX', 'SEQ_MAX',
           'EVENT_TYPES']
