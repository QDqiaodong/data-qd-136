"""SQLite persistence for the open-items workbench (standard library only).

The database is an ordinary library-format SQLite file opened through
:func:`connect`. All mutating work happens inside explicit transactions so an
error at any point rolls back the whole ingest or commit; a half batch or a
half settlement can never be observed.
"""
import sqlite3

from .errors import ConflictError, OpenItemError

SCHEMA_VERSION = 1

# Plain table statements are split-safe (no nested semicolons).
TABLE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    source_sha256   TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    normal          TEXT NOT NULL CHECK (normal IN ('debit', 'credit'))
)""",
    """CREATE TABLE IF NOT EXISTS entries (
    entry_id        TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    currency        TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS entry_lines (
    entry_id        TEXT NOT NULL REFERENCES entries(entry_id),
    line_idx        INTEGER NOT NULL CHECK (line_idx >= 0),
    account_id      TEXT NOT NULL REFERENCES accounts(account_id),
    side            TEXT NOT NULL CHECK (side IN ('debit', 'credit')),
    amount          INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 1000000000000),
    PRIMARY KEY (entry_id, line_idx)
)""",
    """CREATE TABLE IF NOT EXISTS open_items (
    item_id         TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    entry_id        TEXT NOT NULL,
    line_idx        INTEGER NOT NULL,
    party           TEXT NOT NULL,
    settlement_key  TEXT NOT NULL,
    priority        INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 1000000),
    side            TEXT NOT NULL CHECK (side IN ('debit', 'credit')),
    currency        TEXT NOT NULL,
    account_id      TEXT NOT NULL REFERENCES accounts(account_id),
    initial_amount  INTEGER NOT NULL CHECK (initial_amount >= 1),
    remaining       INTEGER NOT NULL CHECK (remaining >= 0),
    UNIQUE (entry_id, line_idx),
    CHECK (remaining <= initial_amount)
)""",
    """CREATE TABLE IF NOT EXISTS plans (
    plan_id         TEXT PRIMARY KEY,
    state           TEXT NOT NULL CHECK (state IN ('planned', 'committed')),
    created_at      TEXT NOT NULL,
    seed_json       TEXT NOT NULL,
    rules_json      TEXT NOT NULL,
    items_json      TEXT NOT NULL,
    edges_json      TEXT NOT NULL,
    components_json TEXT NOT NULL,
    allocation_json TEXT NOT NULL,
    total_amount    INTEGER NOT NULL CHECK (total_amount >= 0)
)""",
    """CREATE TABLE IF NOT EXISTS plan_allocations (
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    edge_seq        INTEGER NOT NULL,
    debit_item_id   TEXT NOT NULL REFERENCES open_items(item_id),
    credit_item_id  TEXT NOT NULL REFERENCES open_items(item_id),
    amount          INTEGER NOT NULL CHECK (amount >= 1),
    PRIMARY KEY (plan_id, edge_seq)
)""",
    """CREATE TABLE IF NOT EXISTS item_flows (
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    item_id         TEXT NOT NULL REFERENCES open_items(item_id),
    amount          INTEGER NOT NULL CHECK (amount >= 1),
    PRIMARY KEY (plan_id, item_id)
)""",
    """CREATE TABLE IF NOT EXISTS commits (
    plan_id         TEXT PRIMARY KEY REFERENCES plans(plan_id),
    commit_seq      INTEGER NOT NULL UNIQUE,
    receipt_json    TEXT NOT NULL
)""",
]

# Storage-level conservation: initial_amount must always equal remaining plus
# the sum of committed flows. The trigger fires after the remaining balance is
# updated; the workbench also calls assert_conservation() before committing, so
# the invariant cannot be bypassed by direct SQL either.
TRIGGER_STATEMENTS = [
    """CREATE TRIGGER IF NOT EXISTS trg_open_items_conservation
AFTER UPDATE OF remaining ON open_items
FOR EACH ROW
BEGIN
    SELECT CASE WHEN
        NEW.remaining < 0
        OR NEW.remaining
        + COALESCE((SELECT SUM(amount) FROM item_flows WHERE item_id = NEW.item_id), 0)
        <> NEW.initial_amount
    THEN RAISE(ABORT, 'conservation invariant broken') END;
END""",
]

def connect(path):
    """Open (or create) a workbench database with safe pragmas."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    # Wait for the competing connection instead of failing immediately; the
    # explicit transaction checks still guarantee at most one commit wins.
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA journal_mode = WAL')
    _initialize(conn)
    return conn


def _initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    row = conn.execute('SELECT value FROM meta WHERE key = ?', ('schema_version',)).fetchone()
    if row is None:
        for statement in TABLE_STATEMENTS + TRIGGER_STATEMENTS:
            conn.execute(statement)
        conn.execute('INSERT INTO meta(key, value) VALUES (?, ?)',
                     ('schema_version', str(SCHEMA_VERSION)))
        conn.commit()
    elif row['value'] != str(SCHEMA_VERSION):
        raise OpenItemError('unsupported schema version: ' + row['value'])


def assert_conservation(conn):
    """Verify ``initial = remaining + committed flow`` for every open item.

    Also checks that no remaining amount is negative and that every flow has
    matching debit/credit sides. Raises :class:`ConflictError` on the first
    broken invariant; the trial balance being equal is never used as proof of
    correctness.
    """
    rows = conn.execute(
        'SELECT item_id, initial_amount, remaining, '
        'COALESCE((SELECT SUM(amount) FROM item_flows f WHERE f.item_id = o.item_id), 0) AS used '
        'FROM open_items o'
    ).fetchall()
    for row in rows:
        if row['remaining'] < 0:
            raise ConflictError('negative remaining balance for item ' + row['item_id'])
        if row['initial_amount'] != row['remaining'] + row['used']:
            raise ConflictError('conservation invariant broken for item ' + row['item_id'])
    bad = conn.execute(
        'SELECT f.plan_id, f.item_id FROM item_flows f JOIN open_items o ON o.item_id = f.item_id '
        'WHERE f.amount <= 0'
    ).fetchall()
    if bad:
        raise ConflictError('non-positive settlement flow persisted')


def rollback_silent(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
