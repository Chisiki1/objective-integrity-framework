"""Lossless, source-bound master retrieval cache; never a semantic authority.

build writes only an explicitly named cache. query is read-only and checks the
current source against the indexed bytes. Pages preserve every matched segment.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 'master-retrieval-index-v1'
ID = re.compile(r'(?<![A-Z0-9])(?:GPM|PM|G|P|GK|GC|PK|U|KCM|POLICY|SKILL|MGSKILL)[A-Z0-9]*(?:-[A-Z0-9]+)+')
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*$')
CEILING = 'exact source bytes, complete indexed occurrences and matching-page coverage only; relevance, semantic disposition, sanitization and current operational truth unproven'


def sha(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def safe_output(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('reparse output is outside cache contract')
    return path


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


def scan(raw):
    # Byte spans partition the entire source, including preamble and line ends.
    lines = raw.splitlines(keepends=True)
    sections, occurrences, start, offset = [], [], 0, 0
    title, control, level, start_line = 'PREAMBLE', False, 0, 1
    control_roots = []
    for number, line in enumerate(lines, 1):
        text = line.decode('utf8')
        match = HEADING.match(text.rstrip('\r\n'))
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


def build(sources, cache):
    cache = safe_output(cache)
    cache.mkdir(parents=True, exist_ok=True)
    resolved = [Path(p).resolve(strict=True) for p in sources]
    if len(set(resolved)) != len(resolved) or not resolved:
        raise ValueError('sources must be nonempty and distinct')
    masters = []
    for source in resolved:
        if cache == source or source in cache.parents or cache in source.parents:
            raise ValueError('source and cache must not contain one another')
        raw = source.read_bytes()
        raw.decode('utf8')
        digest = sha(raw)
        backing = safe_output(cache / 'backing' / (digest + '.txt'))
        if backing.exists() and backing.read_bytes() != raw:
            raise ValueError('immutable backing conflict')
        if not backing.exists():
            atomic(backing, raw)
        sections, occurrences = scan(raw)
        masters.append({'path': str(source), 'sha256': digest, 'bytes': len(raw),
                        'backing': str(backing), 'sections': sections,
                        'id_occurrences': occurrences})
    result = {'schema': SCHEMA, 'masters': masters, 'proof_ceiling': CEILING}
    digest = sha(canonical(result))
    target = safe_output(cache / ('index-' + digest + '.json'))
    raw = canonical(result)
    if target.exists() and target.read_bytes() != raw:
        raise ValueError('immutable index conflict')
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
    raw_index = Path(index_path).read_bytes()
    idx = json.loads(raw_index.decode('utf8'))
    if idx.get('schema') != SCHEMA:
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
    chunks = []
    for master in idx['masters']:
        raw = Path(master['backing']).read_bytes()
        if sha(raw) != master['sha256'] or len(raw) != master['bytes']:
            raise ValueError('backing identity mismatch')
        if sha(Path(master['path']).read_bytes()) != master['sha256']:
            raise ValueError('STALE_SOURCE: rebuild dependent index')
        sections, occurrences = scan(raw)
        if sections != master['sections'] or occurrences != master['id_occurrences']:
            raise ValueError('index coverage mismatch')
        for sec in sections:
            text = raw[sec['start']:sec['end']].decode('utf8')
            if not ((controls and sec['current_control_region']) or
                    any(term.casefold() in text.casefold() for term in terms)):
                continue
            # Segment only for transport; every selected character remains paged.
            for pos in range(0, len(text), page_chars):
                chunks.append({'source': master['path'], 'source_sha256': master['sha256'],
                               'section_id': sec['section_id'], 'line': sec['line'],
                               'section_sha256': sec['sha256'], 'character_offset': pos,
                               'text': text[pos:pos + page_chars]})
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
    q.add_argument('--page-chars', type=int, default=12000)
    args = p.parse_args()
    try:
        result = build(args.master, args.cache) if args.kind == 'build' else query(args.index, args.term, args.cursor, args.page_chars, args.controls)
        print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc), 'proof_ceiling': CEILING}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
