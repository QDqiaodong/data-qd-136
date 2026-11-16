from .journal import parse_journal
from .ledger import balance

def analyze(data):
    """Trial balance v1: exact integer journals, currencies kept separate."""
    accounts, entries = parse_journal(data)
    return balance(accounts, entries)
