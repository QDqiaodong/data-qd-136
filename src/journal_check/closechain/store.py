"""File-backed immutable generation store for the close-chain workbench.

Layout (an implementation detail; tests must not depend on it)::

    <store>/
        head.json                     # current head pointer (atomically replaced)
        head.lock                     # cross-process fcntl lock
        tmp-*.staging/                # unpublished, garbage-collected residue
        gen-<generation_id>/
            meta.json                 # chain id, parent, request hash, file hashes
            period-<position>-<id>.source.json
            period-<position>-<id>.result.json

Publication protocol
--------------------
All validation and computation happen in memory before anything is written.
A new generation is then staged completely in its own directory. Only after
every source/result file and the meta file are durable does the head pointer
switch via one atomic ``os.replace``; that replace is the single publication
point. A crash before it leaves the old chain fully readable (staging
residue is never linked from the head and is swept on the next locked open);
a crash after it can only expose the complete new chain. Cleanup never
deletes a file referenced by a published generation.
"""
import datetime
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager

from . import engine
from .errors import (
    ConflictError,
    CorruptionError,
    NotFoundError,
    ValidationError,
)

SCHEMA_VERSION = 1
_HEAD_NAME = 'head.json'
_LOCK_NAME = 'head.lock'
_GEN_PREFIX = 'gen-'
_STAGING_PREFIX = 'tmp-'
_STAGING_SUFFIX = '.staging'

# Fault injection points, each triggered at most once per process when the
# named environment variable is set to any non-empty value.
FAULT_SOURCE_WRITE = 'JOURNAL_CHECK_FAULT_SOURCE_WRITE'
FAULT_RESULT_WRITE = 'JOURNAL_CHECK_FAULT_RESULT_WRITE'
FAULT_BEFORE_HEAD_SWITCH = 'JOURNAL_CHECK_FAULT_BEFORE_HEAD_SWITCH'
FAULT_AFTER_HEAD_SWITCH = 'JOURNAL_CHECK_FAULT_AFTER_HEAD_SWITCH'
_FAULT_USED = set()


def _fault(point):
    if os.environ.get(point) and point not in _FAULT_USED:
        _FAULT_USED.add(point)
        raise OSError('injected fault at ' + point)


def _sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class _StoreLock:
    """Whole-store exclusive (writers) or shared (readers) fcntl lock."""

    def __init__(self, path, exclusive):
        self._path = path
        self._exclusive = exclusive
        self._stream = None

    def __enter__(self):
        import fcntl
        self._stream = open(self._path, 'a+', encoding='utf-8')
        mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        fcntl.flock(self._stream.fileno(), mode)
        return self

    def __exit__(self, exc_type, exc, tb):
        import fcntl
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


# ---------------------------------------------------------------------------
# Low-level file helpers
# ---------------------------------------------------------------------------

def _atomic_write(path, blob):
    dest_dir = os.path.dirname(path)
    fd, temp_name = tempfile.mkstemp(prefix='.write-', dir=dest_dir)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _read_json_file(path):
    try:
        with open(path, 'rb') as stream:
            blob = stream.read()
    except OSError as exc:
        raise CorruptionError('cannot read stored file %s: %s'
                              % (path, exc)) from None
    try:
        return json.loads(blob), blob
    except ValueError:
        raise CorruptionError('stored file is not valid JSON: %s' % path) \
            from None


def _read_head(store_dir):
    path = os.path.join(store_dir, _HEAD_NAME)
    if not os.path.exists(path):
        return None
    data, blob = _read_json_file(path)
    if not isinstance(data, dict) or set(data) != {
            'schema_version', 'chain_id', 'generation_id', 'parent',
            'request_sha256', 'period_count'}:
        raise CorruptionError('head pointer has an unexpected shape')
    if data['schema_version'] != SCHEMA_VERSION:
        raise CorruptionError(
            'unsupported store schema version: %s' % data['schema_version'])
    if not os.path.isdir(os.path.join(
            store_dir, _GEN_PREFIX + data['generation_id'])):
        raise CorruptionError(
            'head points at a missing generation: ' + data['generation_id'])
    return data


def _generation_dir(store_dir, generation_id):
    return os.path.join(store_dir, _GEN_PREFIX + generation_id)


def _read_meta(store_dir, generation_id):
    gen_dir = _generation_dir(store_dir, generation_id)
    meta_path = os.path.join(gen_dir, 'meta.json')
    if not os.path.isdir(gen_dir):
        raise NotFoundError('generation not found: ' + generation_id)
    meta, _ = _read_json_file(meta_path)
    if not isinstance(meta, dict) or set(meta) != {
            'schema_version', 'chain_id', 'generation_id', 'parent',
            'request_sha256', 'created_at', 'periods'}:
        raise CorruptionError('generation meta has an unexpected shape: '
                              + generation_id)
    if meta['generation_id'] != generation_id or \
            meta['schema_version'] != SCHEMA_VERSION:
        raise CorruptionError('generation meta is internally inconsistent')
    return meta


def _sweep_staging(store_dir):
    """Remove unpublished staging residue; published files are never touched."""
    for name in os.listdir(store_dir):
        if name.startswith(_STAGING_PREFIX) and name.endswith(_STAGING_SUFFIX):
            _remove_tree(os.path.join(store_dir, name))


def _remove_tree(path):
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(path)


def _reachable_generations(store_dir, head):
    """Walk the parent chain from the head; every other generation is orphan."""
    reachable = set()
    current = head['generation_id'] if head else None
    while current is not None:
        if current in reachable:
            raise CorruptionError('generation parent chain contains a cycle')
        reachable.add(current)
        meta = _read_meta(store_dir, current)
        current = meta['parent']
    return reachable


def _sweep_orphan_generations(store_dir, head):
    """Remove generations no head can reach.

    The parent chain of the current head is real history and is never
    deleted. A generation directory left by a crash between the directory
    rename and the head switch is unreachable and is safe to remove; an
    unreadable meta means the directory was never completed.
    """
    keep = _reachable_generations(store_dir, head)
    for name in os.listdir(store_dir):
        if not name.startswith(_GEN_PREFIX):
            continue
        generation_id = name[len(_GEN_PREFIX):]
        if generation_id in keep:
            continue
        try:
            _read_meta(store_dir, generation_id)
        except (CorruptionError, NotFoundError):
            pass
        _remove_tree(os.path.join(store_dir, name))


# ---------------------------------------------------------------------------
# Store handle
# ---------------------------------------------------------------------------

class Store:
    """Locked entry points used by the public :func:`apply`/:func:`show`."""

    def __init__(self, path):
        self.path = os.path.abspath(str(path))
        os.makedirs(self.path, exist_ok=True)
        self._lock_path = os.path.join(self.path, _LOCK_NAME)

    @contextmanager
    def _locked(self, exclusive):
        with _StoreLock(self._lock_path, exclusive):
            if exclusive:
                # Writers own the only exclusive lock, so residue from a
                # crashed earlier publish cannot be mid-creation here.
                head_for_sweep = _read_head(self.path)
                _sweep_staging(self.path)
                _sweep_orphan_generations(self.path, head_for_sweep)
            yield

    # -- reading -----------------------------------------------------------

    def head(self):
        with self._locked(exclusive=False):
            return _read_head(self.path)

    def show(self, generation_id, period_id):
        with self._locked(exclusive=False):
            # Pinned first: the whole read belongs to this one generation.
            head = _read_head(self.path)
            if generation_id is None:
                if head is None:
                    raise NotFoundError('store has no published generation')
                generation_id = head['generation_id']
            if head is not None and generation_id == head['generation_id']:
                pass
            meta = _read_meta(self.path, generation_id)
            match = [entry for entry in meta['periods']
                     if entry['period_id'] == period_id]
            if not match:
                raise NotFoundError(
                    'period %s not found in generation %s'
                    % (period_id, generation_id))
            entry = match[0]
            gen_dir = _generation_dir(self.path, generation_id)
            # Only the two files of the requested period are opened; the
            # bodies of every other period file stay unread.
            source, source_blob = _read_json_file(
                os.path.join(gen_dir, entry['source_file']))
            result, result_blob = _read_json_file(
                os.path.join(gen_dir, entry['result_file']))
            if _sha256_bytes(source_blob) != entry['source_sha256']:
                raise CorruptionError(
                    'source file hash mismatch for period ' + period_id)
            if _sha256_bytes(result_blob) != entry['result_sha256']:
                raise CorruptionError(
                    'result file hash mismatch for period ' + period_id)
            if result.get('period') != period_id or \
                    source.get('id') != period_id:
                raise CorruptionError(
                    'stored period content does not match its meta entry')
            return {
                'chain_id': meta['chain_id'],
                'generation_id': generation_id,
                'parent': meta['parent'],
                'request_sha256': meta['request_sha256'],
                'position': entry['position'],
                'period_id': period_id,
                'source': source,
                'source_sha256': entry['source_sha256'],
                'result': result,
                'result_sha256': entry['result_sha256'],
            }

    # -- writing -----------------------------------------------------------

    def apply(self, request, request_sha256):
        with self._locked(exclusive=True):
            head = _read_head(self.path)
            chain_id, expected_head, start, _fragment = \
                engine.validate_request(request)
            if head is None:
                if expected_head is not None:
                    raise ConflictError(
                        'expected_head must be null when creating a new chain')
                if start != 1:
                    raise ValidationError(
                        'a new chain must start at position 1')
                parent = None
                head_sources = []
            else:
                if expected_head is None:
                    raise ConflictError(
                        'expected_head is required once the chain exists')
                if expected_head != head['generation_id']:
                    raise ConflictError(
                        'expected_head %s does not match current head %s; '
                        'the request is not silently rebased'
                        % (expected_head, head['generation_id']))
                if chain_id != head['chain_id']:
                    raise ConflictError(
                        'chain_id %s does not match the stored chain %s'
                        % (chain_id, head['chain_id']))
                parent = head['generation_id']
                head_sources = self._load_head_sources(head)

            candidate = engine.prepare_candidate(head_sources, request)
            cand_chain_id, _cand_head, sources, results = candidate
            if cand_chain_id != chain_id:  # pragma: no cover - same input
                raise ValidationError('chain id changed during preparation')
            new_generation_id = engine.generation_id(
                chain_id, parent, sources, request_sha256)

            # A crash between the generation-directory rename and the head
            # switch can leave an identical unpublished directory; it is
            # unreachable from the head and is replaced by a fresh stage.
            if os.path.exists(_generation_dir(self.path, new_generation_id)):
                _remove_tree(_generation_dir(self.path, new_generation_id))

            head_before = head['generation_id'] if head else None
            self._publish(new_generation_id, chain_id, parent,
                          request_sha256, sources, results, head_before)
            new_head = _read_head(self.path)
            return self._receipt(new_head, period_count=len(sources))

    # -- internals ---------------------------------------------------------

    def _load_head_sources(self, head):
        """Load every period source of the current head, verifying hashes."""
        meta = _read_meta(self.path, head['generation_id'])
        gen_dir = _generation_dir(self.path, head['generation_id'])
        sources = []
        for entry in meta['periods']:
            source, blob = _read_json_file(
                os.path.join(gen_dir, entry['source_file']))
            if _sha256_bytes(blob) != entry['source_sha256']:
                raise CorruptionError(
                    'source file hash mismatch for period '
                    + entry['period_id'])
            sources.append(source)
        return sources

    def _publish(self, generation_id, chain_id, parent, request_sha256,
                 sources, results, head_before):
        staging = tempfile.mkdtemp(prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX,
                                   dir=self.path)
        try:
            period_entries = []
            for position, (source, result) in enumerate(
                    zip(sources, results), start=1):
                period_id = source['id']
                source_blob = engine.canonical_bytes(source)
                result_blob = engine.canonical_bytes(result)
                source_name = 'period-%03d-%s.source.json' % (
                    position, period_id)
                result_name = 'period-%03d-%s.result.json' % (
                    position, period_id)
                _fault(FAULT_SOURCE_WRITE)
                _atomic_write(os.path.join(staging, source_name), source_blob)
                _fault(FAULT_RESULT_WRITE)
                _atomic_write(os.path.join(staging, result_name), result_blob)
                period_entries.append({
                    'position': position,
                    'period_id': period_id,
                    'source_file': source_name,
                    'source_sha256': _sha256_bytes(source_blob),
                    'result_file': result_name,
                    'result_sha256': _sha256_bytes(result_blob),
                })
            meta = {
                'schema_version': SCHEMA_VERSION,
                'chain_id': chain_id,
                'generation_id': generation_id,
                'parent': parent,
                'request_sha256': request_sha256,
                'created_at': datetime.datetime.now(
                    datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'periods': period_entries,
            }
            _atomic_write(os.path.join(staging, 'meta.json'),
                          engine.canonical_bytes(meta))
            final_dir = _generation_dir(self.path, generation_id)
            os.rename(staging, final_dir)
            staging = None

            # Re-read the head right at the publication point: even though the
            # store lock is held, this makes the no-implicit-rebase rule
            # explicit and survives any future lock-layout change.
            current = _read_head(self.path)
            current_id = current['generation_id'] if current else None
            if current_id != head_before:
                _remove_tree(final_dir)
                raise ConflictError(
                    'head changed to %s while computing; the stale candidate '
                    'is rejected without rebasing' % current_id)
            _fault(FAULT_BEFORE_HEAD_SWITCH)
            head_blob = engine.canonical_bytes({
                'schema_version': SCHEMA_VERSION,
                'chain_id': chain_id,
                'generation_id': generation_id,
                'parent': parent,
                'request_sha256': request_sha256,
                'period_count': len(sources),
            })
            _atomic_write(os.path.join(self.path, _HEAD_NAME + '.new'),
                          head_blob)
            os.replace(os.path.join(self.path, _HEAD_NAME + '.new'),
                       os.path.join(self.path, _HEAD_NAME))
            _fault(FAULT_AFTER_HEAD_SWITCH)
        finally:
            if staging is not None and os.path.exists(staging):
                _remove_tree(staging)
        # The old generation stays on disk as history; only generations
        # unreachable from any head (none here) would ever be swept.
        _sweep_staging(self.path)

    @staticmethod
    def _receipt(head, period_count):
        return {
            'chain_id': head['chain_id'],
            'generation_id': head['generation_id'],
            'parent': head['parent'],
            'request_sha256': head['request_sha256'],
            'head_generation_id': head['generation_id'],
            'period_count': period_count,
        }


# ---------------------------------------------------------------------------
# Public convenience interface
# ---------------------------------------------------------------------------

def open_store(path):
    """Open (or create) a close-chain store directory."""
    return Store(path)


def apply(store, request, request_sha256):
    """Validate, compute and atomically publish one candidate generation."""
    return Store(store).apply(request, request_sha256) if not \
        isinstance(store, Store) else store.apply(request, request_sha256)


def show(store, generation_id, period_id):
    """Read one historical period with full hash verification."""
    return Store(store).show(generation_id, period_id) if not \
        isinstance(store, Store) else store.show(generation_id, period_id)


__all__ = ['Store', 'open_store', 'apply', 'show',
           'FAULT_SOURCE_WRITE', 'FAULT_RESULT_WRITE',
           'FAULT_BEFORE_HEAD_SWITCH', 'FAULT_AFTER_HEAD_SWITCH']
