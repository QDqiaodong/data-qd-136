"""Shared strict parser for the v1 journal snapshot shape.

The rules are identical to the original :func:`journal_check.model.analyze`
contract; ``analyze`` keeps its exact v1 behaviour and this module only makes
the same parsing reusable for the open-items workbench.
"""
from .common import require, ident, number, rows

ACCOUNT_LIMIT = 1000
ENTRY_LIMIT = 5000
LINE_LIMIT = 100
AMOUNT_MIN = 1
AMOUNT_MAX = 10 ** 12


def parse_journal(data):
    """Validate the ``accounts``/``entries`` snapshot.

    Returns ``(accounts, entries)`` where ``accounts`` maps account id to its
    normal side (``debit``/``credit``) and each entry is the tuple
    ``(entry_id, currency, lines)`` with ``lines`` a list of
    ``(account, side, amount)`` tuples.
    """
    require(data, 'accounts entries')
    accounts = {}
    entries = []
    seen = set()
    for account in rows(data['accounts'], ACCOUNT_LIMIT):
        require(account, 'id normal')
        aid = ident(account['id'])
        normal = ident(account['normal'])
        if aid in accounts or normal not in ('debit', 'credit'):
            raise ValueError('duplicate account or invalid normal side')
        accounts[aid] = normal
    if not accounts:
        raise ValueError('accounts required')
    for entry in rows(data['entries'], ENTRY_LIMIT):
        require(entry, 'id currency lines')
        eid = ident(entry['id'])
        currency = ident(entry['currency'])
        if eid in seen:
            raise ValueError('duplicate entry')
        seen.add(eid)
        lines = []
        for line in rows(entry['lines'], LINE_LIMIT):
            require(line, 'account side amount')
            account = ident(line['account'])
            side = ident(line['side'])
            amount = number(line['amount'], AMOUNT_MIN, AMOUNT_MAX)
            if account not in accounts or side not in ('debit', 'credit'):
                raise ValueError('unknown account or invalid side')
            lines.append((account, side, amount))
        if len(lines) < 2 or sum(v if s == 'debit' else -v for a, s, v in lines) != 0:
            raise ValueError('unbalanced journal')
        entries.append((eid, currency, lines))
    return accounts, entries
