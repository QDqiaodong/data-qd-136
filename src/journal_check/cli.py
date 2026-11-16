"""Command line entry point.

The original v1 invocation stays unchanged::

    journal-check --source <json> --output <report.json>

The open-items workbench is an independent subcommand group::

    journal-check open-items ingest  --db ledger.sqlite --source batch.json
    journal-check open-items status  --db ledger.sqlite [--output report.json]
    journal-check open-items plan    --db ledger.sqlite --request plan.json
    journal-check open-items commit  --db ledger.sqlite --plan-id <id>

The close-chain workbench is another independent subcommand group::

    journal-check close-chain apply --store chain-dir --source request.json
    journal-check close-chain show  --store chain-dir [--generation id] --period id

Every workbench failure raises one of the documented exceptions, all of which
are ``ValueError`` subclasses; the CLI prints the message to stderr, exits 2
and leaves any existing output file untouched.
"""
import argparse
import json
import sys
from pathlib import Path

from .closechain.errors import CloseChainError
from .closechain.store import Store as CloseChainStore
from .common import read, save
from .model import analyze
from .openitems import workbench
from .openitems.errors import OpenItemError
from .openitems.storage import connect as open_items_connect

DESCRIPTION = 'Exact minor-unit journals and currency-separated trial balances'
_FAILURE_TYPES = (ValueError, TypeError, KeyError, OSError, OverflowError,
                  RecursionError, OpenItemError, CloseChainError)


def run(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest, 'result': analyze(data)}


def _emit(report, output):
    if output is not None:
        save(output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))


# ---------------------------------------------------------------------------
# Open-items subcommand group
# ---------------------------------------------------------------------------

def _open_items_parser():
    parser = argparse.ArgumentParser(
        prog='journal-check open-items',
        description='Local open-item write-off workbench')
    actions = parser.add_subparsers(dest='action', required=True)

    ingest = actions.add_parser('ingest', help='validate and import one batch')
    ingest.add_argument('--db', type=Path, required=True)
    ingest.add_argument('--source', type=Path, required=True,
                        help='batch JSON, subject to the strict 8 MB limit')

    status = actions.add_parser('status', help='open items and gross unsettled amounts')
    status.add_argument('--db', type=Path, required=True)
    status.add_argument('--output', type=Path)

    plan = actions.add_parser('plan', help='plan settlements from a request file')
    plan.add_argument('--db', type=Path, required=True)
    plan.add_argument('--request', type=Path, required=True,
                      help='JSON file with exactly {"seeds": [...], "rules": [...]}')
    plan.add_argument('--output', type=Path)

    commit = actions.add_parser('commit', help='commit a stored plan by plan_id')
    commit.add_argument('--db', type=Path, required=True)
    commit.add_argument('--plan-id', required=True)
    commit.add_argument('--output', type=Path)
    return parser


def _run_open_items(argv):
    args = _open_items_parser().parse_args(argv)
    conn = open_items_connect(args.db)
    try:
        if args.action == 'ingest':
            data, digest = read(args.source)
            report = workbench.ingest(conn, data, digest)
        elif args.action == 'status':
            report = workbench.status(conn)
        elif args.action == 'plan':
            request = json.loads(Path(args.request).read_text(encoding='utf-8'))
            if not isinstance(request, dict) or set(request) != {'seeds', 'rules'}:
                raise ValueError('request file must contain exactly seeds and rules')
            report = workbench.plan(conn, request['seeds'], request['rules'])
        else:  # commit
            report = workbench.commit(conn, args.plan_id)
    finally:
        conn.close()
    _emit(report, getattr(args, 'output', None))


# ---------------------------------------------------------------------------
# Close-chain subcommand group
# ---------------------------------------------------------------------------

def _close_chain_parser():
    parser = argparse.ArgumentParser(
        prog='journal-check close-chain',
        description='Local immutable period close-chain workbench')
    actions = parser.add_subparsers(dest='action', required=True)

    apply = actions.add_parser(
        'apply', help='validate a request and publish one immutable generation')
    apply.add_argument('--store', type=Path, required=True,
                       help='chain store directory (created when absent)')
    apply.add_argument('--source', type=Path, required=True,
                       help='request JSON, subject to the strict 8 MB limit')
    apply.add_argument('--output', type=Path)

    show = actions.add_parser(
        'show', help='read one historical period with hash verification')
    show.add_argument('--store', type=Path, required=True)
    show.add_argument('--generation', default=None,
                       help='generation id; defaults to the current head')
    show.add_argument('--period', required=True, help='period id')
    show.add_argument('--output', type=Path)
    return parser


def _run_close_chain(argv):
    args = _close_chain_parser().parse_args(argv)
    store = CloseChainStore(args.store)
    if args.action == 'apply':
        request, request_sha256 = read(args.source)
        report = store.apply(request, request_sha256)
    else:  # show
        report = store.show(args.generation, args.period)
    _emit(report, getattr(args, 'output', None))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == 'open-items':
            _run_open_items(argv[1:])
            return 0
        if argv and argv[0] == 'close-chain':
            _run_close_chain(argv[1:])
            return 0
    except _FAILURE_TYPES as error:
        print(str(error), file=sys.stderr)
        return 2

    # Legacy v1 command: exact original parser, flags and report behaviour.
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except _FAILURE_TYPES as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
