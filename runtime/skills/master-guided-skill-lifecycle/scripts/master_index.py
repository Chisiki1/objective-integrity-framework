"""Lossless, source-bound master retrieval cache; never a semantic authority.

build writes only an explicitly named cache. query is read-only and checks the
current source against the indexed bytes. Pages preserve every matched segment.
"""
from __future__ import annotations
import argparse
import base64
import copy
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SCHEMA = 'master-retrieval-index-v1'
ARCHIVE_SCHEMA = 'master-retrieval-index-v2'
ID = re.compile(r'(?<![A-Z0-9])(?:GPM|PM|G|P|GK|GC|PK|U|KCM|POLICY|SKILL|MGSKILL)[A-Z0-9]*(?:-[A-Z0-9]+)+')
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*$')
HISTORY_MARKER = re.compile(r'^<!-- master-history-v1 (\{.*\}) -->$')
CEILING = 'exact source bytes, complete indexed occurrences and matching-page coverage only; relevance, semantic disposition, sanitization and current operational truth unproven'


def sha(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON field: ' + key)
            result[key] = value
        return result

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('non-finite JSON value')
        if isinstance(value, (list, dict)):
            for item in value.values() if isinstance(value, dict) else value:
                finite(item)

    def constant(value):
        raise ValueError('non-finite JSON constant: ' + value)

    result = json.loads(raw.decode('utf8'), object_pairs_hook=pairs, parse_constant=constant)
    finite(result)
    return result


def _hash(value):
    if type(value) is not str or re.fullmatch(r'[A-Fa-f0-9]{64}', value) is None:
        raise ValueError('explicit full SHA256 required')
    return value.upper()


def plain_path(value, *, existing=True):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('explicit absolute path without parent traversal required')
    if os.name == 'nt' and (str(path).startswith(('\\\\?\\', '\\\\.\\'))
            or ':' in str(path)[len(path.drive):]):
        raise ValueError('device paths and alternate streams are unsupported')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if existing:
                raise ValueError('missing source/backing: ' + str(part))
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked/reparse path is outside archive contract')
    if existing and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('plain regular source/backing required')
    return path


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_plain(value):
    path = plain_path(value)
    before = path.lstat()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _stamp(opened) != _stamp(before):
            raise ValueError('source identity changed before read')
        raw = stream.read()
        after_handle = os.fstat(stream.fileno())
        if _stamp(opened) != _stamp(after_handle) or opened.st_ctime_ns != after_handle.st_ctime_ns:
            raise ValueError('source changed during read')
    plain_path(path)
    after = path.lstat()
    if _stamp(before) != _stamp(after) or before.st_ctime_ns != after.st_ctime_ns:
        raise ValueError('source identity changed after read')
    raw.decode('utf8')
    return raw


def safe_output(path):
    # Retain the existing relative-cache convenience, never resolve links away.
    return plain_path(Path(path).absolute(), existing=False)


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.index-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def scan(raw, *, markdown_fences=True):
    # Byte spans partition the entire source, including preamble and line ends.
    lines = raw.splitlines(keepends=True)
    sections, occurrences, start, offset = [], [], 0, 0
    title, control, level, start_line = 'PREAMBLE', False, 0, 1
    control_roots = []
    fence = None
    for number, line in enumerate(lines, 1):
        text = line.decode('utf8')
        content = text.rstrip('\r\n').lstrip('\ufeff') if number == 1 and markdown_fences else text.rstrip('\r\n')
        fence_match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', content)
        in_fence = fence is not None
        if fence_match:
            marker, tail = fence_match.groups()
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not tail.strip():
                fence = None
            in_fence = True
        match = None if in_fence and markdown_fences else HEADING.match(content)
        if match:
            if offset > start:
                sections.append({'start': start, 'end': offset, 'line': start_line,
                                 'title': title, 'current_control_region': control, 'level': level})
            level = len(match[1])
            title = match[2]
            # Explicit control roots can occur at any supported heading depth.
            # A sibling/ancestor closes nested roots; enclosing control survives.
            control_roots = [depth for depth in control_roots if depth < level]
            if 'CURRENT CONTROL' in title.upper():
                control_roots.append(level)
            control = bool(control_roots)
            start, start_line = offset, number
        for item in ID.finditer(text):
            occurrences.append({'id': item[0], 'line': number, 'column': item.start() + 1,
                                'heading': bool(match)})
        offset += len(line)
    if offset > start:
        sections.append({'start': start, 'end': offset, 'line': start_line,
                         'title': title, 'current_control_region': control, 'level': level})
    for number, section in enumerate(sections):
        section['section_id'] = number
        section['sha256'] = sha(raw[section['start']:section['end']])
    return sections, occurrences


@lru_cache(maxsize=16)
def _scan_cached(raw):
    # Bounded in-memory acceleration only. Query still hashes every source and
    # cache backing each call; no stat-only freshness or persistent read receipt.
    return scan(raw)


@lru_cache(maxsize=32)
def _matching_segments(raw, terms, controls, page_chars, legacy=False):
    sections = scan(raw, markdown_fences=False)[0] if legacy else _scan_cached(raw)[0]
    chunks = []
    for section in sections:
        text = raw[section['start']:section['end']].decode('utf8')
        if not ((controls and section['current_control_region'])
                or any(term in text.casefold() for term in terms)):
            continue
        for position in range(0, len(text), page_chars):
            chunks.append({'section_id': section['section_id'], 'line': section['line'],
                'section_sha256': section['sha256'], 'character_offset': position,
                'text': text[position:position + page_chars]})
    return chunks


def history_markers(raw):
    result, offset = [], 0
    for number, line in enumerate(raw.splitlines(keepends=True), 1):
        text = line.decode('utf8').rstrip('\r\n')
        if re.search(r'<!--\s*master-history-v1\b', text):
            match = HISTORY_MARKER.fullmatch(text)
            if match is None:
                raise ValueError('malformed single-line master-history-v1 marker')
            ref = strict_json(match[1].encode('utf8'))
            if type(ref) is not dict or set(ref) != {'path', 'sha256', 'bytes'}:
                raise ValueError('history marker fields differ')
            if type(ref['path']) is not str:
                raise ValueError('history path must be text')
            ref['path'] = str(plain_path(ref['path']))
            ref['sha256'] = _hash(ref['sha256'])
            if type(ref['bytes']) is not int or ref['bytes'] < 0:
                raise ValueError('history byte size must be a nonnegative integer')
            result.append({**ref, 'line': number, 'start': offset, 'end': offset + len(line)})
        offset += len(line)
    return result


def load_sources(sources):
    """Pure exact live/history graph loader, shared by retrieval and inventory.

    Repeated references to the SAME lexical identity are deduplicated while all
    parent marker edges remain. Different aliases of one physical file, cycles,
    role conflicts and stale/missing references are errors. raw is bytes here,
    not part of the public JSON index. This is not an atomic filesystem lease.
    """
    roots = [str(plain_path(Path(source).absolute())) for source in sources]
    keys = [os.path.normcase(path) for path in roots]
    if not roots or len(keys) != len(set(keys)):
        raise ValueError('sources must be nonempty and distinct')
    nodes, edges, visiting = {}, [], set()
    identities = {}

    def visit(path, role, expected=None):
        key = os.path.normcase(path)
        if key in visiting:
            raise ValueError('history cycle')
        if role == 'history' and key in keys:
            raise ValueError('history aliases a live source role')
        if key in nodes:
            node = nodes[key]
            if expected and (node['sha256'], node['bytes']) != (expected['sha256'], expected['bytes']):
                raise ValueError('conflicting repeated history identity')
            return
        raw = read_plain(path)
        digest = sha(raw)
        if expected and (digest != expected['sha256'] or len(raw) != expected['bytes']):
            raise ValueError('history hash/size mismatch: ' + path)
        info = Path(path).lstat()
        physical = info.st_dev, info.st_ino
        if physical in identities and identities[physical] != key:
            raise ValueError('source/history physical alias')
        identities[physical] = key
        sections, occurrences = _scan_cached(raw)
        nodes[key] = {'path': path, 'sha256': digest, 'bytes': len(raw), 'role': role,
            'raw': raw, 'sections': copy.deepcopy(sections), 'id_occurrences': copy.deepcopy(occurrences)}
        visiting.add(key)
        for ref in history_markers(raw):
            edges.append({'parent_path': path, 'parent_sha256': digest, **ref})
            visit(ref['path'], 'history', ref)
        visiting.remove(key)

    try:
        for root in roots:
            visit(root, 'live')
    except RecursionError as error:
        raise ValueError('history graph exceeds interpreter traversal depth') from error
    for node in nodes.values():
        if sha(read_plain(node['path'])) != node['sha256']:
            raise ValueError('source changed while resolving history graph')
        node['parent_refs'] = [copy.deepcopy(edge) for edge in edges if edge['path'] == node['path']]
    return {'roots': roots, 'sources': list(nodes.values()), 'history_edges': edges,
        'proof_ceiling': CEILING}


def _separate_output(output, sources):
    for source in sources:
        path = Path(source['path'])
        if output == path or path in output.parents or output in path.parents:
            raise ValueError('source/history and cache/output must not contain one another')
        if output.exists() and os.path.samefile(output, path):
            raise ValueError('source/history aliases cache/output')


def build(sources, cache):
    cache = safe_output(cache)
    graph = load_sources(sources)
    _separate_output(cache, graph['sources'])
    archive = bool(graph['history_edges'])
    masters = []
    outputs = []
    for source in graph['sources']:
        raw, digest = source['raw'], source['sha256']
        backing = safe_output(cache / 'backing' / (digest + '.txt'))
        _separate_output(backing, graph['sources'])
        if backing.exists() and read_plain(backing) != raw:
            raise ValueError('immutable backing conflict')
        outputs.append((backing, raw))
        master = {key: source[key] for key in ('path', 'sha256', 'bytes', 'sections', 'id_occurrences')}
        master['backing'] = str(backing)
        if archive:
            master.update(role=source['role'], parent_refs=source['parent_refs'])
        masters.append(master)
    result = {'schema': ARCHIVE_SCHEMA if archive else SCHEMA, 'masters': masters, 'proof_ceiling': CEILING}
    if archive:
        result.update(roots=graph['roots'], history_edges=graph['history_edges'])
    digest = sha(canonical(result))
    target = safe_output(cache / ('index-' + digest + '.json'))
    _separate_output(target, graph['sources'])
    raw = canonical(result)
    if target.exists() and target.read_bytes() != raw:
        raise ValueError('immutable index conflict')
    for backing, backing_raw in outputs:
        if not backing.exists():
            atomic(backing, backing_raw)
    if not target.exists():
        atomic(target, raw)
    return {'index': str(target), 'sha256': digest,
            'master_count': len(masters), 'indexed_bytes': sum(m['bytes'] for m in masters),
            'occurrences': sum(len(m['id_occurrences']) for m in masters),
            'proof_ceiling': CEILING}


def query(index_path, terms, cursor=None, page_chars=12000, controls=False):
    if not isinstance(page_chars, int) or isinstance(page_chars, bool) or not 256 <= page_chars <= 50000:
        raise ValueError('page_chars outside256..50000')
    if not terms and not controls:
        raise ValueError('explicit query terms or controls required')
    if any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError('empty query term')
    if type(controls) is not bool:
        raise ValueError('controls must be boolean')
    raw_index = read_plain(Path(index_path).absolute())
    idx = strict_json(raw_index)
    if idx.get('schema') not in {SCHEMA, ARCHIVE_SCHEMA}:
        raise ValueError('unsupported index schema')
    digest = sha(canonical(idx))
    expected_name = 'index-' + digest + '.json'
    if Path(index_path).name != expected_name:
        raise ValueError('index identity mismatch')
    binding = sha(canonical([digest, sorted(set(t.casefold() for t in terms)), controls, page_chars]))
    offset = 0
    if cursor is not None:
        token, numeric = cursor.rsplit(':', 1)
        if token != binding or not numeric.isdecimal():
            raise ValueError('cursor/query identity mismatch')
        offset = int(numeric)
    archive = idx['schema'] == ARCHIVE_SCHEMA
    roots = idx['roots'] if archive else [master['path'] for master in idx['masters']]
    # All hashes remain checked on every call/page. Cache only source-derived
    # scans, not freshness claims. Missing history fails the dependent query.
    try:
        graph = load_sources(roots)
    except ValueError as error:
        raise ValueError('STALE_SOURCE or invalid history: ' + str(error)) from error
    if not archive and graph['history_edges']:
        raise ValueError('v1 index does not cover archived history; rebuild dependent index')
    if archive and graph['history_edges'] != idx['history_edges']:
        raise ValueError('STALE_SOURCE: history graph changed')
    if [source['path'] for source in graph['sources']] != [master['path'] for master in idx['masters']]:
        raise ValueError('index source coverage mismatch')
    chunks = []
    for master, source in zip(idx['masters'], graph['sources']):
        raw = read_plain(master['backing'])
        if sha(raw) != master['sha256'] or len(raw) != master['bytes']:
            raise ValueError('backing identity mismatch')
        if source['sha256'] != master['sha256']:
            raise ValueError('STALE_SOURCE: rebuild dependent index')
        if archive and (source['role'] != master['role'] or source['parent_refs'] != master['parent_refs']):
            raise ValueError('index live/history role or lineage mismatch')
        _separate_output(Path(master['backing']), graph['sources'])
        sections, occurrences = source['sections'], source['id_occurrences']
        legacy_scan = False
        if not archive and (sections != master['sections'] or occurrences != master['id_occurrences']):
            sections, occurrences = scan(raw, markdown_fences=False)
            legacy_scan = True
        if sections != master['sections'] or occurrences != master['id_occurrences']:
            raise ValueError('index coverage mismatch')
        for segment in _matching_segments(raw, tuple(sorted(set(term.casefold() for term in terms))),
                controls and source['role'] == 'live', page_chars, legacy_scan):
            chunks.append({'source': master['path'], 'source_sha256': master['sha256'],
                'source_role': source['role'], 'parent_refs': source['parent_refs'], **segment})
    if offset > len(chunks):
        raise ValueError('cursor past result frontier')
    page, used = [], 0
    for chunk in chunks[offset:]:
        size = len(chunk['text'])
        if page and used + size > page_chars:
            break
        page.append(chunk)
        used += size
    next_offset = offset + len(page)
    return {'index_sha256': digest, 'query_binding': binding, 'items': page,
            'total_segments': len(chunks), 'returned_segments': len(page),
            'remaining_segments': len(chunks) - next_offset,
            'next_cursor': binding + ':' + str(next_offset) if next_offset < len(chunks) else None,
            'complete_for_explicit_query': next_offset == len(chunks),
            'all_knowledge_semantics_accounted': False, 'proof_ceiling': CEILING}



def _query_options(terms, controls, page_chars):
    if type(page_chars) is not int or not 256 <= page_chars <= 50000:
        raise ValueError('page_chars outside256..50000')
    if type(controls) is not bool:
        raise ValueError('controls must be boolean')
    if not isinstance(terms, (list, tuple)) or any(type(t) is not str or not t.strip() for t in terms):
        raise ValueError('query terms must be an array of nonempty strings')
    if not terms and not controls:
        raise ValueError('explicit query terms or controls required')
    return tuple(sorted(set(t.casefold() for t in terms)))


def _validated_query_sources(index_path):
    """The compact reader retains all legacy source/coverage/lineage checks.

    No mtime-only shortcut: each invocation/page reloads the live history graph
    and hashes every exact cache backing. The legacy query API is unchanged.
    """
    idx = strict_json(read_plain(Path(index_path).absolute()))
    if type(idx) is not dict or idx.get('schema') not in {SCHEMA, ARCHIVE_SCHEMA}:
        raise ValueError('unsupported index schema')
    digest = sha(canonical(idx))
    if Path(index_path).name != 'index-' + digest + '.json':
        raise ValueError('index identity mismatch')
    masters = idx.get('masters')
    if type(masters) is not list or not masters or any(type(m) is not dict for m in masters):
        raise ValueError('malformed index masters')
    for master in masters:
        if (any(type(master.get(k)) is not str for k in ('path', 'backing', 'sha256'))
                or type(master.get('bytes')) is not int or master['bytes'] < 0
                or any(type(master.get(k)) is not list for k in ('sections', 'id_occurrences'))):
            raise ValueError('malformed index source')
        _hash(master['sha256'])
    archive = idx['schema'] == ARCHIVE_SCHEMA
    roots = idx.get('roots') if archive else [m['path'] for m in masters]
    if type(roots) is not list or not roots or any(type(r) is not str for r in roots):
        raise ValueError('malformed index roots')
    if archive and type(idx.get('history_edges')) is not list:
        raise ValueError('malformed index history edges')
    try:
        graph = load_sources(roots)
    except ValueError as error:
        raise ValueError('STALE_SOURCE or invalid history: ' + str(error)) from error
    if not archive and graph['history_edges']:
        raise ValueError('v1 index does not cover archived history; rebuild dependent index')
    if archive and canonical(graph['history_edges']) != canonical(idx['history_edges']):
        raise ValueError('STALE_SOURCE: history graph changed')
    if [s['path'] for s in graph['sources']] != [m['path'] for m in masters]:
        raise ValueError('index source coverage mismatch')
    validated = []
    for master, source in zip(masters, graph['sources']):
        raw = read_plain(master['backing'])
        if sha(raw) != master['sha256'] or len(raw) != master['bytes']:
            raise ValueError('backing identity mismatch')
        if source['sha256'] != master['sha256']:
            raise ValueError('STALE_SOURCE: rebuild dependent index')
        if archive and (source['role'] != master.get('role')
                        or source['parent_refs'] != master.get('parent_refs')):
            raise ValueError('index live/history role or lineage mismatch')
        _separate_output(Path(master['backing']), graph['sources'])
        sections, occurrences = source['sections'], source['id_occurrences']
        if not archive and (canonical(sections) != canonical(master['sections'])
                            or canonical(occurrences) != canonical(master['id_occurrences'])):
            sections, occurrences = scan(raw, markdown_fences=False)
        if (canonical(sections) != canonical(master['sections'])
                or canonical(occurrences) != canonical(master['id_occurrences'])):
            raise ValueError('index coverage mismatch')
        validated.append((source, raw, sections))
    return digest, graph, validated


def _compact_data(index_path, terms, controls):
    digest, graph, sources = _validated_query_sources(index_path)
    binding = sha(canonical(['master-query-compact-v1', digest, terms, controls]))
    # Raw bytes are the grouping key, not a hash alone, title, ID, normalized
    # line ending, fuzzy match or inferred semantic equivalence.
    by_bytes, ids = {}, {}
    for source_number, (source, raw, sections) in enumerate(sources):
        for section in sections:
            content = raw[section['start']:section['end']]
            text = content.decode('utf8')
            selected = ((controls and source['role'] == 'live' and section['current_control_region'])
                        or any(term in text.casefold() for term in terms))
            group = by_bytes.get(content)
            if group is None:
                group_id = sha(content)
                if group_id in ids and ids[group_id] != content:
                    raise ValueError('section hash collision; exact groups cannot share an ID')
                ids[group_id] = content
                group = {'group_id': group_id, 'text': text, 'bytes': len(content),
                         'origins': [], 'selected': False}
                by_bytes[content] = group
            group['selected'] |= selected
            group['origins'].append({'source': source_number, 'section_id': section['section_id'],
                'line': section['line'], 'byte_start': section['start'], 'byte_end': section['end'],
                'current_control_region': section['current_control_region'],
                'matched_by_query': bool(selected)})
    # Include every exact origin of a selected group. A controls-only match can
    # therefore disclose historical copies as origins, never as live controls.
    groups = [group for group in by_bytes.values() if group['selected']]
    return digest, binding, graph, groups


def serialize_page(page):
    """Exact compact CLI wire bytes, including one LF; budget uses this form.

    ASCII escaping makes wire bytes == serialized characters on every host.
    A caller that pretty-prints the returned dict must budget its own formatter.
    """
    return (json.dumps(page, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode('ascii')


def _position(cursor, prefix, binding, count):
    if cursor is None:
        return (0,) * count
    if type(cursor) is not str:
        raise ValueError('cursor must be text')
    parts = cursor.split(':')
    if len(parts) != count + 2 or parts[:2] != [prefix, binding]:
        raise ValueError('cursor/query/mode identity mismatch')
    numbers = parts[2:]
    if any(len(n) > 20 or re.fullmatch(r'0|[1-9][0-9]*', n) is None for n in numbers):
        raise ValueError('cursor position is not canonical')
    return tuple(map(int, numbers))


def _fit_text(text, offset, budget, make_page):
    """Fit the complete envelope; never return a successful zero-progress page."""
    remaining = len(text) - offset
    # Completion can remove a cursor and shrink metadata, so test that boundary
    # separately before the monotone search over strictly partial fragments.
    if remaining <= budget:
        whole = make_page(text[offset:], remaining)
        if len(serialize_page(whole)) <= budget:
            return whole, remaining
    low, high, best = 1, min(budget, remaining - 1), None
    while low <= high:
        middle = (low + high) // 2
        page = make_page(text[offset:offset + middle], middle)
        if len(serialize_page(page)) <= budget:
            best = (page, middle)
            low = middle + 1
        else:
            high = middle - 1
    return best if best is not None else (None, 0)


def query_compact(index_path, terms, cursor=None, page_chars=12000, controls=False):
    """Page unique exact section text; query() remains the legacy Python API.

    C1 cursors bind the exact validated index and normalized explicit query.
    page_chars budgets serialize_page(result), including metadata and LF; it
    may change between pages. Reassemble text per group_id/character_offset.
    origins_complete is false: use query_origins for complete provenance.
    """
    terms = _query_options(terms, controls, page_chars)
    digest, binding, graph, groups = _compact_data(index_path, terms, controls)
    number, offset = _position(cursor, 'C1', binding, 2)
    if number > len(groups) or (number == len(groups) and offset != 0):
        raise ValueError('cursor past result frontier')
    if number < len(groups) and offset >= len(groups[number]['text']):
        raise ValueError('cursor past group text frontier')
    items = []

    def envelope(values, next_number, next_offset):
        complete = next_number == len(groups)
        return {'schema': 'master-query-compact-v1', 'index_sha256': digest,
                'query_binding': binding, 'items': values, 'total_groups': len(groups),
                'remaining_groups': len(groups) - next_number,
                'next_cursor': None if complete else 'C1:%s:%d:%d' % (binding, next_number, next_offset),
                'complete_for_explicit_query': complete, 'origins_complete': False,
                'all_knowledge_semantics_accounted': False, 'proof_ceiling': CEILING}

    result = envelope(items, number, offset)
    while number < len(groups):
        group = groups[number]
        text = group['text']

        def candidate(fragment, length):
            end = offset + length
            finished = end == len(text)
            item = {'group_id': group['group_id'], 'section_bytes': group['bytes'],
                    'section_characters': len(text), 'character_offset': offset, 'text': fragment,
                    'section_complete': finished, 'origin_count': len(group['origins']),
                    'matching_origin_count': sum(o['matched_by_query'] for o in group['origins'])}
            return envelope(items + [item], number + 1 if finished else number, 0 if finished else end)

        fitted, consumed = _fit_text(text, offset, page_chars, candidate)
        if fitted is None:
            if items:
                break
            minimum = len(serialize_page(candidate(text[offset:offset + 1], 1)))
            raise ValueError('page_chars too small for progress; next fragment requires at least %d serialized characters' % minimum)
        result = fitted
        items = result['items']
        offset += consumed
        if offset != len(text):
            break
        number += 1
        offset = 0
    if len(serialize_page(result)) > page_chars:
        raise ValueError('page_chars too small for the complete empty-result envelope')
    return result


def _origin_document(graph, group):
    # Sources and graph edges occur once in this document, not parent_refs
    # repeated on every text fragment or every section occurrence.
    positions = {source['path']: i for i, source in enumerate(graph['sources'])}
    included = {origin['source'] for origin in group['origins']}
    incoming = {}
    for edge in graph['history_edges']:
        incoming.setdefault(positions[edge['path']], []).append(positions[edge['parent_path']])
    pending = list(included)
    while pending:
        child = pending.pop()
        for parent in incoming.get(child, []):
            if parent not in included:
                included.add(parent)
                pending.append(parent)
    sources = [{'source': i, **{k: s[k] for k in ('path', 'sha256', 'bytes', 'role')}}
               for i, s in enumerate(graph['sources']) if i in included]
    edges = [{'parent': positions[e['parent_path']], 'source': positions[e['path']],
              'marker_line': e['line'], 'marker_start': e['start'], 'marker_end': e['end']}
             for e in graph['history_edges'] if positions[e['path']] in included]
    return {'schema': 'master-query-origin-document-v1', 'group_id': group['group_id'],
            'origins': group['origins'], 'sources': sources, 'history_edges': edges}


def query_origins(index_path, terms, group_id, cursor=None, page_chars=12000, controls=False):
    """Independently page exact provenance for one selected group.

    Concatenate origin_json at character_offset, then JSON-decode once complete.
    Its document includes exact source identities/roles, every occurrence and
    the relevant ancestor graph with no duplicated per-occurrence parent_refs.
    Arbitrarily long paths are paginated as data, never unbounded metadata.
    O1 cursors cannot be used for text, a different group, query or index.
    """
    terms = _query_options(terms, controls, page_chars)
    group_id = _hash(group_id)
    digest, binding, graph, groups = _compact_data(index_path, terms, controls)
    group = next((g for g in groups if g['group_id'] == group_id), None)
    if group is None:
        raise ValueError('group is not in the explicit query')
    origin_binding = sha(canonical([binding, group_id]))
    offset, = _position(cursor, 'O1', origin_binding, 1)
    document = canonical(_origin_document(graph, group)).decode('utf8')
    if offset >= len(document):
        raise ValueError('cursor past origin document frontier')

    def candidate(fragment, consumed):
        end = offset + consumed
        complete = end == len(document)
        return {'schema': 'master-query-origins-v1', 'index_sha256': digest,
                'query_binding': binding, 'group_id': group_id, 'origin_count': len(group['origins']),
                'character_offset': offset, 'total_characters': len(document), 'origin_json': fragment,
                'next_cursor': None if complete else 'O1:%s:%d' % (origin_binding, end),
                'origins_complete': complete, 'content_included': False, 'proof_ceiling': CEILING}

    page, consumed = _fit_text(document, offset, page_chars, candidate)
    if page is None:
        minimum = len(serialize_page(candidate(document[offset:offset + 1], 1)))
        raise ValueError('page_chars too small for progress; next origin fragment requires at least %d serialized characters' % minimum)
    return page


def _compact_error(error, budget):
    # An oversized missing path or malformed input must not defeat the output
    # bound. Preserve the exact message hash/length and an explicitly partial
    # excerpt; this error never claims a complete successful retrieval.
    message = str(error)
    digest = sha(message.encode('utf8', errors='surrogatepass'))
    def candidate(fragment, count):
        return {'status': 'ERROR', 'error': fragment, 'error_sha256': digest,
                'error_characters': len(message), 'error_complete': count == len(message)}
    page, _ = _fit_text(message, 0, budget, candidate) if message else (candidate('', 0), 0)
    return page if page is not None else {'status': 'ERROR', 'error_sha256': digest}



def propose_organization(master_path, history_path, section_ids, expected_sha256):
    """Pure byte proposal for explicitly owner-selected complete heading bodies.

    The complete old master, including preamble/unselected bytes and any earlier
    pointers, becomes immutable backing. The owner stages/verifies it FIRST and
    CAS-installs replacement SECOND. This function grants neither operation.
    """
    master = plain_path(master_path)
    graph = load_sources([master])
    root = graph['sources'][0]
    if root['sha256'] != _hash(expected_sha256):
        raise ValueError('STALE_SOURCE: organization precondition differs')
    history = plain_path(history_path, existing=False)
    _separate_output(history, graph['sources'])
    raw = root['raw']
    if history.exists() and read_plain(history) != raw:
        raise ValueError('owned history destination already contains different bytes')
    if type(section_ids) is not list or not section_ids or any(type(item) is not int for item in section_ids):
        raise ValueError('explicit nonempty integer section IDs required')
    if len(section_ids) != len(set(section_ids)):
        raise ValueError('duplicate selected section ID')
    sections = {section['section_id']: section for section in root['sections']}
    if not set(section_ids) <= set(sections):
        raise ValueError('unknown selected section ID')
    selected = set(section_ids)
    markers = history_markers(raw)
    for identity in selected:
        section = sections[identity]
        if section['level'] == 0 or section['current_control_region']:
            raise ValueError('preamble/current-control removal is refused')
        if any(section['start'] <= marker['start'] < section['end'] for marker in markers):
            raise ValueError('history pointer section removal is refused')
        # Removing a heading while retaining its children would silently change
        # their hierarchy. A parent may only leave with its complete subtree.
        for child in root['sections'][identity + 1:]:
            if child['level'] <= section['level']:
                break
            if child['section_id'] not in selected:
                raise ValueError('selected parent requires its complete heading subtree')
    retained = b''.join(raw[section['start']:section['end']]
        for section in root['sections'] if section['section_id'] not in selected)
    ref = {'path': str(history), 'sha256': root['sha256'], 'bytes': len(raw)}
    # Append outside any retained section without changing a single retained
    # byte. Any added separator belongs to the replacement delta, not history.
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    separator = b'' if not retained or retained.endswith((b'\n', b'\r')) else newline
    marker = b'<!-- master-history-v1 ' + canonical(ref) + b' -->' + newline
    replacement = retained + separator + marker
    after_sections, after_occurrences = scan(replacement)
    before_ids = sorted({item['id'] for node in graph['sources'] for item in node['id_occurrences']})
    current_after_ids = sorted({item['id'] for item in after_occurrences})
    reachable_after_ids = sorted(set(before_ids) | set(current_after_ids))
    # Repeat exact precondition after constructing bytes; no future CAS lease.
    if read_plain(master) != raw:
        raise ValueError('STALE_SOURCE: organization source changed during proposal')
    return {'schema': 'master-organization-proposal-v1',
        'master_ref': {key: root[key] for key in ('path', 'sha256', 'bytes')},
        'history_ref': ref, 'replacement_sha256': sha(replacement), 'replacement_bytes': len(replacement),
        'replacement_base64': base64.b64encode(replacement).decode('ascii'),
        'backing_base64': base64.b64encode(raw).decode('ascii'),
        'selected_section_ids': sorted(selected), 'before_ids': before_ids,
        'after_current_ids': current_after_ids, 'after_reachable_ids': reachable_after_ids,
        'missing_reachable_ids': sorted(set(before_ids) - set(reachable_after_ids)),
        'before_sections': root['sections'], 'after_sections': after_sections,
        'retained_spans': [{'start': section['start'], 'end': section['end'], 'sha256': section['sha256']}
            for section in root['sections'] if section['section_id'] not in selected],
        'history_edges_before': graph['history_edges'],
        'archive_reachability': {'planned_new_parent': str(master), 'planned_history_ref': ref,
            'already_validated_backing_refs': [{key: node[key] for key in ('path', 'sha256', 'bytes')}
                for node in graph['sources'] if node['role'] == 'history'],
            'publication_observed': False},
        'writes_performed': False, 'permission_granted': False, 'proof_ceiling': CEILING}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='kind', required=True)
    b = sub.add_parser('build')
    b.add_argument('--master', action='append', required=True)
    b.add_argument('--cache', required=True)
    q = sub.add_parser('query')
    q.add_argument('--index', required=True)
    q.add_argument('--term', action='append', default=[])
    q.add_argument('--controls', action='store_true')
    q.add_argument('--cursor')
    q.add_argument('--page-chars', type=int, default=12000, help='compact serialized output budget, including metadata and LF')
    q.add_argument('--legacy-output', action='store_true', help='retain the legacy repeated-segment CLI output and text-only budget')
    q.add_argument('--origins', metavar='GROUP_ID', help='separate paged provenance for an exact compact-query group')
    organize = sub.add_parser('propose-organization')
    organize.add_argument('--master', required=True)
    organize.add_argument('--history', required=True)
    organize.add_argument('--section-id', action='append', required=True, type=int)
    organize.add_argument('--expected-sha256', required=True)
    args = p.parse_args()
    try:
        if args.kind == 'build':
            result = build(args.master, args.cache)
        elif args.kind == 'query':
            if args.legacy_output:
                if args.origins is not None:
                    raise ValueError('--origins is not a legacy-output mode')
                result = query(args.index, args.term, args.cursor, args.page_chars, args.controls)
            elif args.origins is not None:
                result = query_origins(args.index, args.term, args.origins, args.cursor, args.page_chars, args.controls)
            else:
                result = query_compact(args.index, args.term, args.cursor, args.page_chars, args.controls)
        else:
            result = propose_organization(args.master, args.history, args.section_id, args.expected_sha256)
        if args.kind == 'query' and not args.legacy_output:
            sys.stdout.buffer.write(serialize_page(result))
        else:
            print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if args.kind == 'query' and not args.legacy_output:
            budget = args.page_chars if 256 <= args.page_chars <= 50000 else 12000
            sys.stdout.buffer.write(serialize_page(_compact_error(exc, budget)))
        else:
            print(json.dumps({'status': 'ERROR', 'error': str(exc), 'proof_ceiling': CEILING}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
