from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import base64
import hashlib
import json
import os
from uuid import uuid4

from .capabilities import atomic_json, confined, process_lock, relative_path
from .models import (Learning, LearningContractError, OperationResult, PolicyError,
                     APPLICATION_FIELDS, APPLICATION_EVIDENCE_FIELDS,
                     application_evidence_value, now, validate_learning_updates,
                     idea_deferral, validate_optional_deferral)
from .store import Store, digest, redact
from .updates import ordinary_use_result_observed


def _text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _hash(value, label):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdefABCDEF' for c in value):
        raise PolicyError(label + ' must be an exact SHA256')
    return value.lower()


def _pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise PolicyError('Evidence needs a nonempty JSON pointer into the actual result')
    value = document
    try:
        for part in pointer[1:].split('/'):
            key = part.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise PolicyError('Evidence pointer is absent from the actual result') from error
    if value is None or value == '' or value == [] or value == {}:
        raise PolicyError('Evidence pointer has no observed content')
    return value


def _time(value):
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            raise ValueError('timezone missing')
        return instant
    except (TypeError, ValueError, AttributeError) as error:
        raise PolicyError('Evidence needs an observed timezone-aware timestamp') from error


class Knowledge:
    """Source-bound observations; structural evidence is not semantic truth."""

    def __init__(self, store: Store, runtime_evidence=None):
        self.store = store
        self.runtime_evidence = runtime_evidence
        self.optional_deferral_enabled = lambda: False
        self.root = store.data_dir / 'knowledge'
        self.root.mkdir(exist_ok=True)
        self.startup_projection_status = {'state': 'projected'}
        try:
            self._render_skills()
        except (OSError, PolicyError, ValueError) as error:
            # Keep the controller available to inspect/review a repair. This is
            # not a clean-start claim; ordinary learning records its own outcome.
            self.startup_projection_status = {'state': 'pending', 'effect': 'unknown',
                'first_fault': {'type': type(error).__name__, 'message': str(error)}}

    def _task(self, task):
        return self.store.get_task(task['id'] if isinstance(task, dict) else task)

    def idea_disposition(self, task, idea):
        """Persist a reviewed choice, never infer optionality from an open ID."""
        body = idea.model_dump() if hasattr(idea, 'model_dump') else idea
        deferred = idea_deferral(body)
        if deferred is None:
            return {'status': 'rejected' if body['disposition'] == 'reject' else 'pending'}
        if not self.optional_deferral_enabled():
            raise PolicyError('Optional deferral requires the approved scoped-delivery policy')
        return {'status': 'deferred', 'deferral': deferred,
                'resolution_reason': deferred['reason'], 'next_trigger': deferred['next_trigger'],
                'deferral_source_hash': task['source_hash'],
                'deferral_acceptance_hash': digest(task['acceptance']),
                'deferral_policy_hash': task.get('policy_hash')}

    def _primary_id(self, task):
        seen = set()
        while task.get('parent_id'):
            if task['id'] in seen:
                raise PolicyError('Task ownership cycle')
            seen.add(task['id'])
            task = self.store.get_task(task['parent_id'])
        return task['id']

    def _owner(self, skill):
        if skill.get('owner_task_id'):
            return skill['owner_task_id']
        # Legacy rows need source-backed ownership, never a caller-supplied guess.
        sources = skill.get('sources', [])
        origin = self.store.record_get('episode', sources[0]) if sources else None
        if not origin:
            raise PolicyError('Legacy Skill ownership is unresolved')
        # The original schema kept the creating episode first and appended reuse
        # and merge lineage. Other actors' later use does not transfer ownership.
        return origin['task_id']

    def _visible(self, task, skill):
        if skill.get('visibility') == 'shared':
            return True
        owner = self._owner(skill)
        return owner == task['id'] or (
            not skill.get('visibility') and not self.store.get_task(owner).get('parent_id'))

    def _writable(self, task, skill):
        return self._owner(skill) == task['id'] or (
            not task.get('parent_id') and self._visible(task, skill))

    def _episode_read_basis(self, task, episode):
        if episode['task_id'] == task['id']:
            return [{'kind': 'owned-episode', 'task_id': task['id']}]
        if episode.get('visibility') in {'private', 'task'}:
            return []
        owner = self._task(episode['task_id'])
        # A child's raw work is private until its exact source is included in a
        # parent-reviewed shared integration. A guessed episode ID grants nothing.
        if owner.get('parent_id'):
            grant = self.store.record_get('episode_access', episode['id'])
            if not grant or grant.get('episode_sha256') != digest(episode):
                return []
            candidate = self.store.record_get('knowledge_candidate', grant.get('candidate_id'))
            if (not candidate or candidate['status'] != 'integrated' or
                    candidate['hash'] != grant.get('candidate_hash') or
                    digest(candidate['payload']) != candidate['hash'] or
                    candidate['payload']['source_task_id'] != owner['id'] or
                    candidate['payload']['source_episode_id'] != episode['id'] or
                    candidate['payload']['source_episode_sha256'] != digest(episode) or
                    candidate['payload']['primary_task_id'] != self._primary_id(owner) or
                    candidate['integration_operation_id'] != grant.get('operation_id') or
                    grant.get('parent_task_id') != self._primary_id(owner)):
                return []
        basis = []
        for skill in self.store.records('skill') + self.store.records('skill_history'):
            if episode['id'] in skill.get('sources', []) and self._visible(task, skill):
                basis.append({'kind': 'visible-skill-source', 'skill_id': skill['id'], 'skill_hash': skill['hash']})
        for family in self.store.records('global_family'):
            if episode['id'] in family.get('episode_refs', []):
                basis.append({'kind': 'shared-family-source', 'family_id': family['id'], 'family_sha256': digest(family)})
        return basis

    def describe_episode(self, task, episode_id, expected_hash=None):
        """Read exact stored experience, not a summary or an arbitrary task row."""
        task = self._task(task)
        episode = self.store.record_get('episode', episode_id)
        if not episode:
            raise PolicyError('Knowledge episode is unavailable')
        basis = self._episode_read_basis(task, episode)
        if not basis:
            raise PolicyError('Knowledge episode is outside the visible source scope')
        actual = digest(episode)
        if expected_hash is not None and _hash(expected_hash, 'Episode expected hash') != actual:
            raise PolicyError('Knowledge episode changed since source selection')
        return {'episode_id': episode_id, 'episode_sha256': actual,
                'source_task_id': episode['task_id'], 'episode': deepcopy(episode),
                'operation_sha256': digest(episode['operation']),
                'result_sha256': digest(episode['result']),
                'learning_sha256': digest(episode['learning']), 'read_basis': basis}

    def _deferred_source(self, idea):
        episode = self.store.record_get('episode', idea.get('source_episode'))
        return {'idea_id': idea['id'], 'source_hash': digest(idea),
                'source_task_id': idea['task_id'], 'source_episode_id': idea.get('source_episode'),
                'source_episode_sha256': digest(episode) if episode else None,
                'source_operation_id': idea.get('source_operation_id'),
                'source_objective': (episode or {}).get('source_objective'),
                'proposal': idea['proposal'], 'applicability': idea.get('applicability'),
                'next_trigger': idea.get('next_trigger'), 'status': idea['status'],
                'source_state': 'observed' if episode else 'unavailable',
                'idea': deepcopy(idea)}

    def deferred_for_planning(self, task):
        """Expose all visible carryovers; the reviewed planner decides relevance.

        Legacy deferred rows are read through the same derived index without
        rewriting their source. Keyword filtering must not silently lose an idea.
        """
        task = self._task(task)
        candidates = []
        for idea in self.store.records('idea'):
            if (idea.get('status') != 'deferred' or (idea.get('target') != 'task'
                    and not (self.optional_deferral_enabled() and idea.get('deferral')))):
                continue
            if (self.optional_deferral_enabled() and idea['task_id'] == task['id']
                    and idea.get('deferral') and task.get('policy_hash')
                    and idea.get('deferral_policy_hash') == task['policy_hash']
                    and idea.get('deferral_source_hash') == task['source_hash']
                    and idea.get('deferral_acceptance_hash') == digest(task['acceptance'])):
                # Reuse this task's already governed disposition. It remains in
                # the original journal/index; changed conditions or another
                # task make it a candidate again, without reopening it here.
                continue
            if idea['task_id'] != task['id']:
                owner = self._task(idea['task_id'])
                if idea.get('visibility') in {'private', 'task'} or owner.get('parent_id'):
                    continue
                episode = self.store.record_get('episode', idea.get('source_episode'))
                if episode and not self._episode_read_basis(task, episode):
                    continue
            candidates.append(self._deferred_source(idea))
        candidates.sort(key=lambda item: item['idea_id'])
        context = {key: task.get(key) for key in ('id', 'parent_id', 'objective', 'acceptance')}
        payload = {'task_id': task['id'], 'task_context_sha256': digest(context), 'candidates': candidates}
        return dict(payload, index_hash=digest(payload),
                    instruction='Account for every candidate by source_hash and reasoned include/exclude. '
                    'Include requires a concrete planned_application. Original task ownership is unchanged.')

    def record_deferred_selection(self, task, operation_id, decisions, expected_index_hash):
        task = self._task(task)
        expected_index_hash = _hash(expected_index_hash, 'Deferred index hash')
        key = task['id'] + ':' + operation_id
        request_hash = digest({'decisions': decisions, 'expected_index_hash': expected_index_hash})

        def commit():
            prior = self.store.record_get('deferred_consideration', key)
            if prior:
                if prior['input_hash'] != request_hash:
                    raise PolicyError('Deferred consideration changed after the reviewed plan')
                return prior['outcome']
            described = self.deferred_for_planning(task)
            if described['index_hash'] != expected_index_hash:
                raise PolicyError('Deferred sources changed; review the new planning inventory')
            entries = {d.get('idea_id'): d for d in decisions}
            targets = {c['idea_id']: c for c in described['candidates']}
            if len(entries) != len(decisions) or set(entries) != set(targets):
                raise PolicyError('Plan must consider every visible deferred source exactly once')
            for identity, item in entries.items():
                if set(item) != {'idea_id', 'source_hash', 'disposition', 'reason', 'planned_application'}:
                    raise PolicyError('Deferred consideration schema is incomplete')
                if _hash(item['source_hash'], 'Deferred source hash') != targets[identity]['source_hash']:
                    raise PolicyError('Deferred consideration refers to different source bytes')
                if item['disposition'] not in {'include', 'exclude'} or not str(item['reason']).strip():
                    raise PolicyError('Deferred consideration needs reasoned include/exclude')
                if item['disposition'] == 'include' and not str(item['planned_application']).strip():
                    raise PolicyError('Included carryover needs a concrete next-task application')
            row = self._reviewed_row(task['id'], operation_id)
            plan = row['operation'].get('args', {}).get('plan', {})
            if row['status'] != 'executing' or row['operation']['kind'] != 'plan_task':
                raise PolicyError('Deferred consideration requires the reviewed executing plan')
            if plan.get('deferred_index_hash') != expected_index_hash or plan.get('deferred_considerations') != decisions:
                raise PolicyError('Deferred consideration differs from the frozen plan')
            outcome = {'task_id': task['id'], 'operation_id': operation_id, 'index_hash': expected_index_hash,
                       'sources': described['candidates'], 'decisions': deepcopy(decisions),
                       'included_idea_ids': [i for i, d in entries.items() if d['disposition'] == 'include'],
                       'source_ownership_unchanged': True, 'actual_use_not_inferred': True}
            self.store.record('deferred_consideration', key,
                              {'id': key, 'input_hash': request_hash, 'outcome': outcome})
            return outcome
        return self.store._transaction(commit)

    def retrieve(self, task, operation=None):
        task = self._task(task)
        skills = [s for s in self.store.records('skill')
                  if s['status'] != 'retired' and self._visible(task, s)]
        episodes = [e for e in self.store.records('episode') if e['task_id'] == task['id']]
        deferred = self.deferred_for_planning(task)
        candidates = [self._describe_record(c) for c in self.store.records('knowledge_candidate')
                      if c['payload']['source_task_id'] == task['id'] or
                      (not task.get('parent_id') and c['payload']['primary_task_id'] == task['id'])]
        families = []
        for family in self.store.records('global_family'):
            visible = [identity for identity in family.get('episode_refs', [])
                       if (episode := self.store.record_get('episode', identity)) and self._episode_read_basis(task, episode)]
            if visible:
                families.append(dict(family, episode_refs=visible,
                                     skill_refs=[identity for identity in family.get('skill_refs', [])
                                                 if (skill := self.store.record_get('skill', identity)) and self._visible(task, skill)]))
        return {
            'global': families,
            'project': [{'id': e['id'], 'task_id': e['task_id'], 'operation_id': e['operation_id'],
                         'outcome': e['result']['status'], 'summary': e['learning']['outcome_summary'],
                         'source_ref': e['id']} for e in episodes],
            'skills': skills, 'deferred_candidates': deferred['candidates'],
            'deferred_planning': deferred, 'candidates': candidates,
            'source_read': 'knowledge_read {episode_id,expected_hash} returns the exact visible original episode; source_ref is an episode ID, not an operation ID.',
            'selection_rule': 'Choose by source, scope and preconditions. Selection is not use. '
                              'Selected entries need id/hash and exact procedure_clause/procedure_sha256. '
                              'Learning.applications binds reviewed actual result evidence. '
                              'Existing Skill mutations need expected_hash; merge needs merge_hashes.'
        }

    _INDEX_KINDS = ('skill', 'idea', 'episode', 'family', 'candidate', 'deferred')

    def _index_values(self, task, kind):
        """Complete visible values; paging never changes source or write authority."""
        if kind == 'skill':
            return [s for s in self.store.records('skill') if self._visible(task, s)]
        if kind == 'idea':
            return [i for i in self.store.records('idea') if i['task_id'] == task['id']]
        if kind == 'episode':
            return [e for e in self.store.records('episode') if self._episode_read_basis(task, e)]
        if kind == 'candidate':
            return [self._describe_record(c) for c in self.store.records('knowledge_candidate')
                    if c['payload']['source_task_id'] == task['id'] or
                    (not task.get('parent_id') and c['payload']['primary_task_id'] == task['id'])]
        if kind == 'deferred':
            return self.deferred_for_planning(task)['candidates']
        if kind == 'family':
            # The shared view has always filtered foreign private references.
            # Hash and explicitly label that view, never claim it is the raw row.
            values = []
            for family in self.store.records('global_family'):
                episodes = [identity for identity in family.get('episode_refs', [])
                            if (episode := self.store.record_get('episode', identity)) and
                            self._episode_read_basis(task, episode)]
                if episodes:
                    skills = [identity for identity in family.get('skill_refs', [])
                              if (skill := self.store.record_get('skill', identity)) and self._visible(task, skill)]
                    values.append(dict(family, episode_refs=episodes, skill_refs=skills))
            return values
        raise PolicyError('Unknown knowledge index kind; use ' + ', '.join(self._INDEX_KINDS))

    @staticmethod
    def _source_identity(kind, value):
        return value['candidate_id' if kind == 'candidate' else 'idea_id' if kind == 'deferred' else 'id']

    def _index_data(self, task, kind, query):
        if not isinstance(query, str) or len(query) > 2048:
            raise PolicyError('Knowledge lookup query must be text of at most 2048 characters')
        values = sorted(self._index_values(task, kind), key=lambda v: self._source_identity(kind, v))
        version = digest({'task_id': task['id'], 'kind': kind,
                          'sources': [[self._source_identity(kind, v), digest(v)] for v in values]})
        terms = query.casefold().split()
        matched = [v for v in values if all(term in json.dumps(v, ensure_ascii=False).casefold() for term in terms)]
        return {'task_id': task['id'], 'kind': kind, 'query': query,
                'index_hash': version, 'total': len(values), 'matched': len(matched)}, matched

    @staticmethod
    def _cursor(header, offset):
        payload = {key: header[key] for key in ('task_id', 'kind', 'index_hash')}
        payload.update(schema='knowledge-cursor-v1', query_sha256=digest(header['query']), offset=offset)
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')

    @staticmethod
    def _cursor_offset(cursor, header):
        if cursor is None:
            return 0
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError('invalid cursor')
            value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
            if (not isinstance(value, dict) or value.get('schema') != 'knowledge-cursor-v1' or
                    value.get('task_id') != header['task_id'] or value.get('kind') != header['kind'] or
                    value.get('query_sha256') != digest(header['query'])):
                raise ValueError('cursor belongs to another lookup')
            if value.get('index_hash') != header['index_hash']:
                raise PolicyError('Knowledge index changed; restart lookup and reconcile already read source hashes')
            offset = value['offset']
            if type(offset) is not int or not 0 <= offset <= header['matched']:
                raise ValueError('cursor offset is out of range')
            return offset
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise PolicyError('Invalid knowledge cursor for this task/kind/query') from error

    def _index_item(self, kind, value):
        identity, source_hash = self._source_identity(kind, value), digest(value)
        summary = (value.get('title') or value.get('proposal') or value.get('summary') or
                   value.get('learning', {}).get('outcome_summary') or value.get('payload', {}).get('source_objective') or '')
        preview = str(summary)
        item = {'id': identity, 'source_sha256': source_hash, 'status': value.get('status'),
                'preview': preview[:240], 'preview_truncated': len(preview) > 240,
                'source_scope': 'visible-family-projection' if kind == 'family' else 'visible-source',
                'read_args': {'kind': kind, 'identity': identity, 'expected_hash': source_hash}}
        if kind == 'skill':
            item.update(hash=value['hash'], needs_cleanup=bool(value.get('needs_cleanup')),
                        uses_count=len(value.get('uses', [])))
        if kind == 'idea':
            item['target'] = value.get('target')
        if kind == 'candidate':
            item.update(candidate_hash=value['candidate_hash'], state_hash=value['state_hash'])
        if kind == 'deferred':
            item['source_hash'] = value['source_hash']
        return item

    def _index_page(self, header, values, offset, limit):
        items = [self._index_item(header['kind'], v) for v in values[offset:offset + limit]]
        end = offset + len(items)
        return dict(header, schema='knowledge-index-v1', offset=offset, returned=len(items), items=items,
                    next_cursor=self._cursor(header, end) if end < len(values) else None,
                    complete_index=offset == 0 and end == len(values), full_values_provided=False)

    def index(self, task, kind, query='', cursor=None, limit=20):
        """A bounded, source-version-bound lookup. An index is never a source read.

        `skill` includes retired current rows with their explicit status. The
        caller must select an active exact Skill and read its full procedure.
        A cursor is navigation only; every request recalculates actual visibility.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PolicyError('Knowledge index limit must be between 1 and 100')
        def collect():
            header, values = self._index_data(self._task(task), kind, query)
            return self._index_page(header, values, self._cursor_offset(cursor, header), limit)
        return self.store._transaction(collect)

    def read(self, task, kind, identity, expected_hash=None):
        """Return the complete selected visible value, without a length ceiling.

        `expected_hash` is the index's source_sha256, not the Skill procedure
        version hash. Family values retain the existing visibility projection;
        all episode/Skill/idea values are complete original stored records.
        """
        def collect():
            current = self._task(task)
            if kind in {'skill', 'idea', 'episode'}:
                value = self.store.record_get(kind, identity)
                if value is not None:
                    visible = (self._visible(current, value) if kind == 'skill' else
                               value['task_id'] == current['id'] if kind == 'idea' else
                               bool(self._episode_read_basis(current, value)))
                    if not visible:
                        value = None
            else:
                value = next((v for v in self._index_values(current, kind)
                              if self._source_identity(kind, v) == identity), None)
            if value is None:
                raise PolicyError('Knowledge source is unavailable or outside the visible source scope')
            actual = digest(value)
            if expected_hash is not None and _hash(expected_hash, 'Knowledge source hash') != actual:
                raise PolicyError('Knowledge source changed since selection; look up its current version')
            return {'schema': 'knowledge-read-v1', 'kind': kind, 'identity': identity,
                    'source_sha256': actual, 'value': deepcopy(value), 'complete_visible_value': True,
                    'source_scope': 'visible-family-projection' if kind == 'family' else 'visible-source',
                    'selection_or_use_not_inferred': True}
        return self.store._transaction(collect)

    def context(self, task, query='', max_chars=24000):
        """Compact navigation plus complete pending counts; raw history stays stored.

        Bounds the JSON representation using json.dumps(ensure_ascii=False).
        If even the required census/navigation cannot fit, report that explicitly.
        The full retrieve/snapshot and finish gate remain independent of this view.
        """
        if type(max_chars) is not int or max_chars < 1:
            raise PolicyError('Knowledge context needs a positive character budget')
        def collect():
            current = self._task(task)
            indexed = {kind: self._index_data(current, kind, query) for kind in self._INDEX_KINDS}
            own_ideas = [i for i in self.store.records('idea') if i['task_id'] == current['id']]
            pending_ideas = [i for i in own_ideas if i['status'] not in {'rejected', 'verified', 'deferred'}]
            pending = {'workflow_ideas': sum(i['target'] == 'workflow' for i in pending_ideas),
                       'task_ideas': sum(i['target'] == 'task' for i in pending_ideas),
                       'skills_cleanup': sum(current['id'] in s.get('touched_tasks', []) and s.get('needs_cleanup', False)
                           and self._writable(current, s) for s in self.store.records('skill')),
                       'pending_candidates': sum(c['status'] == 'pending' for c in self._index_values(current, 'candidate')),
                       'deferred_candidates': indexed['deferred'][0]['total'],
                       'projection_failures': len(self.pending_projection_failures(current))}
            result = {'schema': 'knowledge-context-v1', 'task_id': current['id'], 'projection_only': True,
                      'full_history_read': False, 'pending': pending,
                      'startup_projection_status': deepcopy(self.startup_projection_status),
                      'indices': {kind: self._index_page(header, values, 0, 3)
                                  for kind, (header, values) in indexed.items()},
                      'lookup': {'index': 'knowledge_index {kind,query,cursor,limit}; follow next_cursor until the required scope is read.',
                                 'read': 'knowledge_read with an item.read_args returns its complete visible value.',
                                 'selection': 'Read an active Skill in full before selecting its exact hash/procedure_clause/procedure_sha256. '
                                              'An index preview is not a read, application, review or verified effect.',
                                 'next_trigger': 'Retrieve original sources before dependent decisions; reconcile every pending item before finish. '
                                                 'A query miss or an omitted page does not discharge an obligation.'}}
            result['current_state_sha256'] = digest({'pending': pending,
                'indices': {kind: header['index_hash'] for kind, (header, _) in indexed.items()}})
            while len(json.dumps(result, ensure_ascii=False)) > max_chars:
                pages = [p for p in result['indices'].values() if p['items']]
                if not pages:
                    raise PolicyError('Knowledge context budget cannot fit required census/navigation; increase max_chars')
                page = max(pages, key=lambda p: len(json.dumps(p['items'], ensure_ascii=False)))
                header, values = indexed[page['kind']]
                result['indices'][page['kind']] = self._index_page(header, values, 0, len(page['items']) - 1)
            return result
        return self.store._transaction(collect)

    def _idea_time(self, idea):
        if idea.get('created_at'):
            return _time(idea['created_at'])
        episode = self.store.record_get('episode', idea.get('source_episode'))
        if (not episode or episode.get('id') != idea.get('source_episode') or episode.get('task_id') != idea.get('task_id') or
                (idea.get('source_operation_id') and episode.get('operation_id') != idea['source_operation_id'])):
            raise PolicyError('Legacy idea time needs its exact owned source episode')
        return _time(episode.get('created_at'))

    def _reviewed_row(self, task_id, operation_id, *, post=False):
        try:
            row = self.store.get_operation(operation_id)
        except KeyError as error:
            row=self._web_reviewed_row(task_id,operation_id,post=post)
            if row is None:
                raise PolicyError('Evidence operation is not in the controller journal') from error
        if row['task_id'] != task_id:
            raise PolicyError('Evidence operation is not owned by the task')
        stages = ['pre', 'post'] if post else ['pre']
        for stage in stages:
            bundle, review = row.get(stage + '_bundle'), row.get(stage + '_review')
            if not bundle or not review or review.get('bundle_hash') != digest(bundle):
                raise PolicyError('Evidence is missing its exact reviewed ' + stage + ' bundle')
            if (review.get('disposition') or {}).get('verdict') != 'proceed':
                raise PolicyError('Evidence was not accepted by the parent disposition')
            if bundle.get('operation') != row['operation']:
                raise PolicyError('Reviewed operation differs from the frozen operation')
            if stage == 'post' and bundle.get('result') != row.get('result'):
                raise PolicyError('Reviewed result differs from the actual journal result')
        return row

    def _web_reviewed_row(self,task_id,operation_id,*,post=False):
        """Adapt actual finite Web reviews, never fabricate an ordinary action.

        Only a retained pre-exchange selection plus the exact acquired result
        can establish Web Skill use. Legacy outcomes without that evidence may
        still be learned, but cannot claim a procedure was selected or applied.
        """
        before=self.store.record_get('web_work',operation_id+':before')
        after=self.store.record_get('web_work',operation_id+':after')
        if not before or not after:return None
        if (before.get('task_id')!=task_id or after.get('task_id')!=task_id or
            before.get('detail',{}).get('id')!=operation_id or after.get('detail',{}).get('id')!=operation_id or
            before.get('acquisition_operation')!=after.get('acquisition_operation') or
            before.get('selection')!=after.get('selection') or not before.get('selection_binding')):
            raise PolicyError('Web application lost its exact before/after source identity')
        operation=after['acquisition_operation'];result=after['acquisition_result']
        if result.get('data')!=after['detail'] or operation.get('id')!=operation_id:
            raise PolicyError('Web application result differs from actual acquisition')
        original=before.get('review_input')
        if (not original or digest(original)!=before.get('review_input_sha256') or
            original.get('operation')!=operation or original.get('acquisition')!=before['detail'] or
            original.get('pre',{}).get('skills')!=before['selection'] or
            not before.get('review') or before.get('disposition',{}).get('verdict')!='proceed'):
            raise PolicyError('Web procedure selection lacks its actual before review')
        pre_bundle={'operation':operation,'skills':before['selection'],'acquisition':before['detail'],
                    'review_input_sha256':before['review_input_sha256']}
        row={'task_id':task_id,'operation':operation,'result':result,'pre_bundle':pre_bundle,
             'pre_review':{'bundle_hash':digest(pre_bundle),'disposition':before['disposition']}}
        if post:
            attempts=after.get('learning_attempts',[])
            if not attempts:raise PolicyError('Web application has no actual Learning review')
            attempt=attempts[-1];review_input=attempt.get('review_input')
            if (not review_input or digest(review_input)!=attempt.get('review_input_sha256') or
                review_input.get('operation')!=operation or review_input.get('result')!=result or
                review_input.get('learning')!=attempt['learning'] or
                review_input.get('pre',{}).get('skills')!=before['selection'] or
                not attempt.get('review') or attempt.get('disposition',{}).get('verdict')!='proceed'):
                raise PolicyError('Web application lacks the exact accepted Learning input')
            row['post_bundle']={'operation':operation,'result':result,'learning':attempt['learning'],
                                'review_input_sha256':attempt['review_input_sha256']}
            row['post_review']={'bundle_hash':digest(row['post_bundle']),'disposition':attempt['disposition']}
        return row

    def _skill_version(self, identity, expected_hash):
        skill = self.store.record_get('skill', identity)
        if skill and skill.get('hash') == expected_hash:
            return skill
        history = self.store.record_get('skill_history', identity + ':' + expected_hash)
        if history and history.get('hash') == expected_hash:
            return history
        # Reachable old history remains valid even when it used UUID record keys.
        for old in self.store.records('skill_history'):
            if old.get('id') == identity and old.get('hash') == expected_hash:
                return old
        raise PolicyError('Exact selected Skill version is unavailable')

    def validate_learning_proposal(self, task, operation, learning, result, selected_skills):
        """Read-only admission of a proposal; final reviewed checks stay atomic.

        A focus page may name a subset of the original frozen selection. It may
        not add a selection, change the original result or assert unobserved use.
        """
        task = self._task(task)
        try:
            validate_learning_updates(learning.skill_updates)
            for idea in learning.ideas:
                self.idea_disposition(task, idea)
            if result.get('operation_id') != operation['id']:
                raise PolicyError('Learning result operation identity differs')
            for identity in selected_skills:
                skill = self.store.record_get('skill', identity)
                if not skill or not self._visible(task, skill):
                    raise PolicyError('Selected Skill is unavailable or outside task visibility')
            applications = self._applications(task, operation, learning, result, selected_skills, reviewed=False)
            self._validate_update_targets(task, learning, applications)
        except PolicyError as error:
            raise LearningContractError(str(error)) from error

    def _is_control_episode(self, episode, task, operation):
        """Prove positive controller provenance; a label alone grants no exclusion."""
        try:
            identity = episode['id']
            journal = self.store.record_get('judgment_result', identity)
            if not journal or journal.get('id') != identity or episode['task_id'] != task['id'] or episode['operation'] != operation:
                return False
            prefix = operation['id'] + ':'
            if not identity.startswith(prefix):
                return False
            phase, input_hash = identity[len(prefix):].rsplit(':', 1)
            expected_hash = digest({'operation': operation, 'phase': phase,
                'assessments': journal['assessments'], 'actual_result': journal['actual_result'],
                'review': journal['review'], 'policy_hash': journal['policy_hash'], 'source_hash': journal['source_hash']})
            if input_hash != expected_hash or journal.get('input_hash') != input_hash:
                return False
            actual = journal['actual_result']
            expected = {'id': identity, 'task_id': task['id'], 'operation_id': operation['id'],
                'created_at': episode['created_at'], 'operation': operation, 'source_objective': episode['source_objective'],
                'result': actual if 'status' in actual else {'status': 'succeeded', 'data': actual},
                'learning': {'outcome_summary': phase + ' actual reviewed outcome', 'classifications': ['organize'],
                    'ideas': [idea for a in journal['assessments'] for idea in a['assessment']['ideas']]},
                'classification_reason': 'Source-bound evaluation and control journaling for an already governed operation; no new procedure use/effect is inferred.'}
            if episode != expected:
                return False
            # Current records bind the entire episode. Legacy controller records
            # have the same recomputable input, result and exact episode shape.
            extra = {'task_id', 'operation_id', 'phase', 'episode_sha256'} & journal.keys()
            if extra and (extra != {'task_id', 'operation_id', 'phase', 'episode_sha256'} or
                    journal['task_id'] != task['id'] or journal['operation_id'] != operation['id'] or
                    journal['phase'] != phase or journal['episode_sha256'] != digest(episode)):
                return False
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def _application_episodes(self, task, operation):
        episodes = [e for e in self.store.records('episode')
                    if e.get('task_id') == task['id'] and e.get('operation_id') == operation['id']]
        controls = [e for e in episodes if self._is_control_episode(e, task, operation)]
        control_ids = {e['id'] for e in controls}
        return [e for e in episodes if e['id'] not in control_ids], controls

    def _require_clean_application(self, task, operation):
        """Caller holds the mutation transaction, so an orphan cannot race admission."""
        if self.store.record_get('knowledge_application', task['id'] + ':' + operation['id']):
            raise PolicyError('Knowledge already committed; preserve and recover its exact original input')
        semantic, _ = self._application_episodes(task, operation)
        if semantic:
            raise PolicyError('Knowledge episode exists without its atomic application marker; preserve the inconsistent original before further application')

    def application_state(self, task, operation, learning, result, selected_skills):
        """Observe the existing atomic effect boundary before ordinary resume.

        Every semantic change follows the episode insert; episode, semantic rows
        and application marker commit together. File projection starts only after
        that commit. Absence of both in a fresh completed transaction proves no
        committed semantic application, without guessing from an exception name.
        """
        task = self._task(task)
        identity = task['id'] + ':' + operation['id']
        original = {'operation': operation, 'learning': learning.model_dump() if learning is not None else None,
                    'result': result, 'selected': selected_skills}
        def inspect():
            application = self.store.record_get('knowledge_application', identity)
            episodes, controls = self._application_episodes(task, operation)
            state = 'committed' if application else 'unknown' if episodes else 'not_committed'
            return {'schema': 'knowledge-application-state-v1', 'task_id': task['id'],
                'operation_id': operation['id'], 'input_hash': digest(original), 'state': state,
                'application': {'id': identity, 'sha256': digest(application)} if application else None,
                'episodes': [{'id': e['id'], 'sha256': digest(e)} for e in episodes],
                'control_episodes': [{'id': e['id'], 'sha256': digest(e),
                    'judgment_result_sha256': digest(self.store.record_get('judgment_result', e['id']))} for e in controls],
                'basis': 'Semantic rows, episode and application marker share one SQLite transaction; projection is after commit. Only positively verified control journals are excluded. Existing commits still require exact original-input validation.'}
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Knowledge transaction is still active; its original failure needs transaction recovery before resume')
            return self.store._transaction(inspect)

    @staticmethod
    def record_only_candidate(learning):
        # This is only eligibility for exact readback, never proof of application.
        return (all(update.get('action') == 'use' for update in learning.skill_updates)
                and all(set(application) == set(APPLICATION_FIELDS) for application in learning.applications))

    def confirm_record_only_application(self, task, operation, learning, result, selected, outcome):
        """Read back accepted control storage; never certify a semantic effect."""
        if (not self.optional_deferral_enabled() or not self.record_only_candidate(learning)
                or result.get('status') not in {'succeeded', 'failed'} or result.get('effect') == 'unknown'
                or outcome.get('candidate_ids')
                or outcome.get('projection_status', {}).get('state') != 'projected'
                or outcome.get('projection_status', {}).get('effect') != 'confirmed'
                or self.pending_projection_failures(task)):
            return None
        identity = task['id'] + ':' + operation['id']
        expected_hash = digest({'operation': operation, 'learning': learning.model_dump(),
                                'result': result, 'selected': selected})
        application = self.store.record_get('knowledge_application', identity)
        episode = self.store.record_get('episode', outcome.get('episode_id'))
        applications = self._applications(task, operation, learning, result, selected)
        self._validate_update_targets(task, learning, applications)
        observed = sorted({item['skill_id'] for item in applications})
        transition = {'schema': 'knowledge-skill-transitions-v1', 'task_id': task['id'],
                      'operation_id': operation['id'], 'input_hash': expected_hash,
                      'episode_id': outcome.get('episode_id'),
                      'entries': outcome.get('skill_transitions', {}).get('entries')}
        projected = self.store.record_get('knowledge_projection_outcome', identity)
        if (not application or application.get('id') != identity or application.get('input_hash') != expected_hash
                or application.get('outcome') != outcome or not episode
                or episode.get('id') != outcome.get('episode_id') or episode.get('task_id') != task['id']
                or episode.get('operation_id') != operation['id'] or episode.get('operation') != operation
                or episode.get('result') != result or episode.get('learning') != learning.model_dump()
                or episode.get('selected_skills') != selected or episode.get('applications') != applications
                or outcome.get('skills') != observed or outcome.get('observed_application_ids') != observed
                or outcome.get('classifications') != learning.classifications
                or outcome.get('effect') != 'UNVERIFIED_UNTIL_REAL_COMPARISON'
                or outcome.get('skill_transitions') != transition
                or not projected or projected.get('input_hash') != expected_hash or projected.get('outcome') != outcome
                or projected.get('semantic_outcome') != {k:v for k,v in outcome.items() if k != 'projection_status'}):
            raise PolicyError('Control Learning storage differs from its exact atomic application')
        entries = transition['entries']
        if (not isinstance(entries, list) or len(entries) != len(observed)
                or {entry.get('skill_id') for entry in entries} != set(observed)):
            raise PolicyError('Control Learning storage lost its exact use transitions')
        for entry in entries:
            skill_id = entry['skill_id']
            before = self._skill_version(skill_id, entry['before']['hash'])
            after = self._skill_version(skill_id, entry['after']['hash'])
            if (not self._writable(task, before)
                    or any(record.get('hash') != digest({k:v for k,v in record.items() if k != 'hash'})
                           or entry[side] != {'hash': record['hash'], 'record_sha256': digest(record)}
                           for side,record in (('before',before),('after',after)))):
                raise PolicyError('Control Learning storage has an invalid original Skill version')
            expected = before
            for item in applications:
                if item['skill_id'] != skill_id:continue
                proof = self._application_proof(task, item, result, episode['id'])
                if self.store.record_get('skill_application', digest({'task': task['id'], 'application': item})) != proof:
                    raise PolicyError('Control Learning storage lost its bound application proof')
                expected = self._skill_value(self._use_value(task, expected, proof, episode['id']), expected)
            if after != expected:
                raise PolicyError('Control Learning storage changed more than the reviewed use record')
        if not task.get('parent_id'):
            family_key = digest({'kind': operation['kind'], 'classification': sorted(learning.classifications)})[:24]
            family = self.store.record_get('global_family', family_key)
            if (not family or episode['id'] not in family.get('episode_refs', [])
                    or not set(observed) <= set(family.get('skill_refs', []))):
                raise PolicyError('Control Learning storage lost its source family references')
        for idea in learning.ideas:
            body = idea.model_dump(); key = digest({'task': task['id'], 'idea': body})[:32]
            retained = self.store.record_get('idea', key)
            if (not retained or retained.get('task_id') != task['id']
                    or any(retained.get(k) != v for k,v in body.items() if k != 'id')):
                raise PolicyError('Control Learning storage lost an original Idea')
        return {'application_id': identity, 'application_input_hash': expected_hash,
                'episode_id': episode['id'], 'episode_sha256': digest(episode),
                'outcome_sha256': digest(outcome),
                'meaning': 'Exact committed storage of already reviewed Learning. No new judgment, Skill effect, review PASS, or task-completion claim.'}

    def _validate_update_targets(self, task, learning, applications):
        observed_ids = {application['skill_id'] for application in applications}
        for index, update in enumerate(learning.skill_updates):
            kind = update['action']
            if kind == 'create':
                continue
            if kind == 'use':
                if update['id'] not in observed_ids:
                    raise PolicyError(f'skill_updates[{index}].use requires its exact evidenced application; selection is not use')
                continue
            target = self.store.record_get('skill', update['id'])
            if not target or not self._visible(task, target):
                raise PolicyError(f'skill_updates[{index}] target is unavailable or outside task visibility')
            self._check_update_cas(update, target)
            for identity in update.get('merge_ids', []):
                merged = self.store.record_get('skill', identity)
                if not merged or not self._visible(task, merged):
                    raise PolicyError(f'skill_updates[{index}] merge source is unavailable or outside task visibility')

    def _applications(self, task, operation, learning, result, selected_skills, *, reviewed=True):
        if not learning.applications:
            return []
        row = self._reviewed_row(task['id'], operation['id'], post=reviewed)
        if row['operation'] != operation or row.get('result') != result:
            raise PolicyError('Skill application operation/result identity changed')
        if reviewed and row['post_bundle'].get('learning') != learning.model_dump():
            raise PolicyError('Skill application is not in the reviewed Learning')
        if result.get('operation_id') != operation['id'] or result.get('status') not in {'succeeded', 'failed'} or result.get('effect') == 'unknown':
            raise PolicyError('Unknown execution cannot establish actual Skill application')
        selected = {s['id']: s for s in row['pre_bundle']['skills']['selected']}
        if (set(selected) != set(selected_skills) if reviewed else not set(selected_skills) <= set(selected)):
            raise PolicyError('Selected Skill identity differs from the frozen selection')
        selected = {identity: selected[identity] for identity in selected_skills}
        applications = []
        seen = set()
        required = set(APPLICATION_FIELDS)
        for application in learning.applications:
            if not isinstance(application, dict) or set(application) != required:
                actual = set(application) if isinstance(application, dict) else set()
                raise PolicyError('Skill application needs the complete bound evidence schema: '
                    f'missing fields {sorted(required-actual)}; unsupported fields {sorted(actual-required)}')
            if not isinstance(application['skill_id'], str) or not application['skill_id'].strip():
                raise PolicyError('Skill application needs an exact skill_id string')
            item = deepcopy(application)
            identity = item['skill_id']
            version = _hash(item['skill_hash'], 'Skill version')
            clause = item['procedure_clause']
            clause_hash = _hash(item['procedure_sha256'], 'Procedure hash')
            if not isinstance(clause, str) or not clause.strip() or _text_hash(clause) != clause_hash:
                raise PolicyError('Procedure clause bytes changed')
            chosen = selected.get(identity)
            if not chosen or chosen.get('hash') != version or chosen.get('procedure_clause') != clause or chosen.get('procedure_sha256') != clause_hash:
                raise PolicyError('Procedure application was not selected before execution')
            skill = self._skill_version(identity, version)
            if skill['status'] == 'retired' or clause not in skill['content'] or not self._visible(task, skill):
                raise PolicyError('Procedure clause is not in the selected visible Skill')
            if item['operation_id'] != operation['id'] or _hash(item['operation_sha256'], 'Operation hash') != digest(operation) or _hash(item['result_sha256'], 'Result hash') != digest(result):
                raise PolicyError('Skill application does not match the actual operation/result')
            key = (identity, version, clause_hash)
            if key in seen:
                raise PolicyError('Duplicate Skill application')
            seen.add(key)
            if not isinstance(item['evidence'], list) or not item['evidence']:
                raise PolicyError('Actual application needs concrete result evidence')
            for evidence in item['evidence']:
                if (not isinstance(evidence, dict) or set(evidence) != set(APPLICATION_EVIDENCE_FIELDS)
                        or not isinstance(evidence['explanation'], str) or not evidence['explanation'].strip()):
                    raise PolicyError('Application evidence needs pointer, hash and explanation')
                pointer = evidence['pointer']
                if _hash(evidence['sha256'], 'Evidence hash') != digest(application_evidence_value(result, pointer)):
                    raise PolicyError('Application evidence bytes differ from the actual result')
            applications.append(item)
        return applications

    def apply(self, task, operation, learning: Learning, result, selected_skills):
        task = self._task(task)
        identity = task['id'] + ':' + operation['id']
        payload_hash = digest({'operation': operation, 'learning': learning.model_dump(),
                               'result': result, 'selected': selected_skills})

        def commit():
            previous = self.store.record_get('knowledge_application', identity)
            if previous:
                if previous['input_hash'] != payload_hash:
                    episode = self.store.record_get('episode', previous['outcome'].get('episode_id'))
                    legacy = digest({'learning': episode['learning'], 'result': episode['result'],
                                     'selected': episode['selected_skills']}) if episode else None
                    if not episode or previous['input_hash'] != legacy or episode['task_id'] != task['id'] or episode['operation'] != operation or episode['result'] != result or Learning.model_validate(episode['learning']).model_dump() != learning.model_dump() or episode['selected_skills'] != selected_skills:
                        raise PolicyError('Applied knowledge input changed; use a new reviewed update')
                    self.store.record('knowledge_application_history', identity + ':' + previous['input_hash'], previous)
                    previous = dict(previous, input_hash=payload_hash, migrated_from_input_hash=legacy)
                    self.store.record('knowledge_application', identity, previous)
                return previous['outcome']
            self._require_clean_application(task, operation)
            # Capture only existing sources this Learning can mutate. These
            # before/after records and the semantic application commit together;
            # later readback must not mistake unrelated changes for its Learning.
            touched = {identity for identity in selected_skills if isinstance(identity, str)}
            for item in learning.applications:
                if isinstance(item.get('skill_id'), str): touched.add(item['skill_id'])
            for item in learning.skill_updates:
                if isinstance(item.get('id'), str): touched.add(item['id'])
                merges = item.get('merge_ids', [])
                if isinstance(merges, list): touched.update(i for i in merges if isinstance(i, str))
            before = {i: self.store.record_get('skill', i) for i in sorted(touched)}
            outcome = self._apply(task, operation, learning, result, selected_skills)
            transitions = []
            for skill_id, source in before.items():
                actual = self.store.record_get('skill', skill_id)
                if source and actual and source != actual:
                    transitions.append({'skill_id': skill_id,
                                        'before': {'hash': source['hash'], 'record_sha256': digest(source)},
                                        'after': {'hash': actual['hash'], 'record_sha256': digest(actual)}})
            outcome['skill_transitions'] = {
                'schema': 'knowledge-skill-transitions-v1', 'task_id': task['id'],
                'operation_id': operation['id'], 'input_hash': payload_hash,
                'episode_id': outcome['episode_id'], 'entries': transitions}
            self.store.record('knowledge_application', identity,
                              {'id': identity, 'input_hash': payload_hash, 'outcome': outcome})
            return outcome

        outcome = self.store._transaction(commit)
        # A returned outcome is immutable. Repair is a separate reviewed operation;
        # retrying learning never replays either its semantic writes or renderer.
        if 'projection_status' in outcome:
            return outcome
        try:
            recovery = self.rebuild_projections()
            first_fault = None if recovery['repaired'] else {
                'type': 'ProjectionConflict', 'message': ', '.join(recovery['conflicts'])}
        except (OSError, PolicyError, ValueError) as error:
            first_fault = {'type': type(error).__name__, 'message': str(error)}
            projection_identity = getattr(error, 'projection_skill_id', None)
            recovery = {'repaired': False, 'conflicts': [projection_identity + ':publication-failed'] if projection_identity else [],
                        'scope_unknown': projection_identity is None,
                        'preimage_receipt': getattr(error, 'projection_receipt_ref', None)}
        projection = {'state': 'pending' if first_fault else 'projected',
                      'effect': 'unknown' if first_fault else 'confirmed',
                      'first_fault': first_fault, 'recovery': recovery}
        failure = None
        if first_fault:
            failure_id = digest({'task_id': task['id'], 'operation_id': operation['id'], 'stage': 'learning-projection'})
            affected = sorted({entry.split(':', 1)[0] for entry in recovery.get('conflicts', [])
                               if self.store.record_get('skill', entry.split(':', 1)[0])})
            if recovery.get('scope_unknown'):
                affected = [s['id'] for s in self.store.records('skill') if self._writable(task, s)]
            failure = {'id': failure_id, 'task_id': task['id'], 'operation_id': operation['id'],
                       'source_input_hash': payload_hash, 'skill_ids': affected, 'first_fault': first_fault,
                       'recovery': recovery, 'observed_at': now(), 'status': 'pending'}
            projection['failure_id'] = failure_id
        actual_outcome = dict(outcome, projection_status=projection)
        def record_observation():
            current = self.store.record_get('knowledge_application', identity)
            if not current or current['input_hash'] != payload_hash:
                raise PolicyError('Knowledge application identity changed before projection observation')
            if 'projection_status' in current['outcome']:
                return current['outcome']
            self.store.record('knowledge_projection_outcome', identity,
                              {'id': identity, 'input_hash': payload_hash, 'semantic_outcome': current['outcome'],
                               'outcome': actual_outcome})
            if failure:
                self.store.record('projection_failure', failure['id'], failure)
            current['outcome'] = actual_outcome
            self.store.record('knowledge_application', identity, current)
            return actual_outcome
        return self.store._transaction(record_observation)

    def _apply(self, task, operation, learning, result, selected_skills):
        validate_learning_updates(learning.skill_updates)
        for idea in learning.ideas:
            self.idea_disposition(task, idea)
        if not learning.classifications:
            raise PolicyError('Result must enter a real Skill lifecycle disposition')
        if result.get('operation_id') != operation['id']:
            raise PolicyError('Learning result belongs to a different operation')
        for identity in selected_skills:
            selected = self.store.record_get('skill', identity)
            if selected is None or not self._visible(task, selected):
                raise PolicyError('Selected Skill is missing or outside the task knowledge scope')
        applications = self._applications(task, operation, learning, result, selected_skills)
        # Validate all original versions before this transaction records uses.
        # An observed use and a procedure improvement may target the same Skill.
        self._validate_update_targets(task, learning, applications)
        episode_id = uuid4().hex
        episode = dict(id=episode_id, task_id=task['id'], operation_id=operation['id'],
                       created_at=now(), source_objective=task['objective'],
                       operation=operation, result=result, learning=learning.model_dump(),
                       selected_skills=selected_skills, applications=applications)
        self.store.record('episode', episode_id, episode)
        applied, pending = [], []
        observed_ids = set()
        for application in applications:
            identity = application['skill_id']
            observed_ids.add(identity)
            proof = self._application_proof(task, application, result, episode_id)
            self.store.record('skill_application', digest({'task': task['id'], 'application': application}), proof)
            skill = self.store.record_get('skill', identity)
            if self._writable(task, skill):
                self._append_use(task, skill, proof, episode_id)
                applied.append(identity)
            else:
                pending.append({'action': 'record_application', 'id': identity,
                                'expected_hash': skill['hash'], 'application': proof})
        for update in learning.skill_updates:
            kind = update.get('action')
            if kind not in {'create', 'use', 'improve', 'organize', 'merge', 'retire'}:
                raise PolicyError('Skill update action is unclassified')
            if kind == 'use':
                if update.get('id') not in observed_ids:
                    raise PolicyError('Selected or declared use is not observed application')
                continue
            if kind == 'create':
                skill = self._new(task, episode_id, update)
                applied.append(skill['id'])
                continue
            skill = self.store.record_get('skill', update.get('id'))
            if skill is None or not self._visible(task, skill):
                raise PolicyError('Skill update target is unavailable')
            targets = [skill] + [self.store.record_get('skill', i) for i in update.get('merge_ids', [])]
            if any(s is None or not self._visible(task, s) for s in targets):
                raise PolicyError('Merge source is unavailable')
            if any(not self._writable(task, s) for s in targets):
                pending.append(deepcopy(update))
                continue
            self._mutate_skill(task, episode_id, update, validated=True)
            applied.append(skill['id'])
        # Unclassified experience becomes explicitly unverified provisional knowledge.
        if not applied and not pending and not self.optional_deferral_enabled():
            skill = self._new(task, episode_id, {
                'title': learning.outcome_summary, 'content': learning.outcome_summary,
                'applicability': task['objective'] + ' / ' + operation['purpose'],
                'next_trigger': learning.next_use_trigger})
            applied.append(skill['id'])
        for idea in learning.ideas:
            body = idea.model_dump()
            identity = digest({'task': task['id'], 'idea': body})[:32]
            existing = self.store.record_get('idea', identity)
            if existing:
                # Repeated extraction must not reopen a resolved idea or replace its origin.
                self.store.record('idea_observation', digest({'idea': identity, 'episode': episode_id}),
                                  {'idea_id': identity, 'source_episode': episode_id})
                continue
            body.update(id=identity, task_id=task['id'], source_episode=episode_id,
                        created_at=episode['created_at'], source_operation_id=operation['id'],
                        applicability=task['objective'], next_trigger=learning.next_use_trigger,
                        implementation_refs=[], verification_refs=[], actual_use_refs=[])
            body.update(self.idea_disposition(task, idea))
            self.store.record('idea', identity, body)
            if body['status'] == 'deferred':
                self.store.record('deferred_idea_index', identity, self._deferred_source(body))
        family = {'kind': operation['kind'], 'classifications': sorted(learning.classifications),
                  'episode_refs': [episode_id], 'skill_refs': sorted(set(applied))}
        candidate_ids = []
        if task.get('parent_id'):
            exports = [self.store.record_get('skill', i) for i in set(applied)]
            exports = [s for s in exports if self._owner(s) == task['id']]
            if pending or exports or not self.optional_deferral_enabled():
                candidate = self._candidate(task, episode, pending, exports, family)
                candidate_ids.append(candidate['id'])
        else:
            self._merge_family(family)
        return {'episode_id': episode_id, 'skills': sorted(set(applied)),
                'observed_application_ids': sorted(observed_ids), 'candidate_ids': candidate_ids,
                'classifications': learning.classifications,
                'effect': 'UNVERIFIED_UNTIL_REAL_COMPARISON'}

    def _check_update_cas(self, update, skill):
        if _hash(update.get('expected_hash'), 'Skill expected_hash') != skill['hash']:
            raise PolicyError('Skill changed after update selection')
        if update.get('action') == 'merge':
            ids, hashes = update.get('merge_ids', []), update.get('merge_hashes', {})
            if not ids or len(set(ids)) != len(ids) or skill['id'] in ids or not update.get('content') or set(hashes) != set(ids):
                raise PolicyError('Merge needs distinct sources, their hashes and consolidated content')
            for identity in ids:
                other = self.store.record_get('skill', identity)
                if other is None or _hash(hashes[identity], 'Merge source hash') != other['hash']:
                    raise PolicyError('Merge source changed after selection')

    @staticmethod
    def _application_proof(task, application, result, episode_id):
        return dict(application, task_id=task['id'], result_ref=episode_id,
                    outcome=result['status'], effect='UNVERIFIED',
                    observation='REVIEWED_APPLICATION_WITH_BOUND_RESULT')

    @staticmethod
    def _use_value(task, skill, proof, episode_id):
        # Evidence retains the actual actor; parent integration is not parent use.
        skill = deepcopy(skill)
        if not any(digest(x) == digest(proof) for x in skill['uses']):
            skill['uses'].append(proof)
        skill['sources'] = sorted(set(skill['sources'] + [episode_id]))
        skill['touched_tasks'] = sorted(set(skill['touched_tasks'] + [task['id']]))
        skill['needs_cleanup'] = True
        skill['revision'] += 1
        return skill

    def _append_use(self, task, skill, proof, episode_id):
        self._save_skill(self._use_value(task, skill, proof, episode_id))

    def _mutate_skill(self, task, episode_id, update, *, validated=False):
        skill = self.store.record_get('skill', update['id'])
        if not validated:
            self._check_update_cas(update, skill)
        if not self._writable(task, skill):
            raise PolicyError('Only the primary parent may consume a shared knowledge change')
        kind = update['action']
        if kind == 'record_application':
            proof = update['application']
            if not self.store.record_get('episode', proof['result_ref']):
                raise PolicyError('Application source episode is unavailable')
            application = {k: proof[k] for k in ('skill_id', 'skill_hash', 'procedure_clause',
                           'procedure_sha256', 'operation_id', 'operation_sha256', 'result_sha256', 'evidence')}
            observed = self.store.record_get('skill_application', digest({'task': proof['task_id'], 'application': application}))
            if observed != proof:
                raise PolicyError('Shared use is not the original observed application')
            self._append_use(task, skill, proof, episode_id)
            return
        if kind == 'retire':
            if not update.get('reason'):
                raise PolicyError('Retirement requires a reason')
            skill.update(status='retired', retirement_reason=update['reason'])
        elif kind == 'merge':
            for identity in update['merge_ids']:
                other = self.store.record_get('skill', identity)
                if not self._writable(task, other):
                    raise PolicyError('Merge source is not owned')
                skill['sources'] = sorted(set(skill['sources'] + other['sources']))
                skill['counterexamples'] += [x for x in other['counterexamples'] if x not in skill['counterexamples']]
                other.update(status='retired', retirement_reason='merged into ' + skill['id'], needs_cleanup=False)
                other['revision'] += 1
                self._save_skill(other)
            skill['content'] = str(update['content'])
        elif kind in {'improve', 'organize'}:
            for field in ('content', 'applicability', 'next_trigger'):
                if field in update:
                    if not isinstance(update[field], str) or not update[field].strip():
                        raise PolicyError('Skill content and use conditions cannot be empty')
                    skill[field] = update[field]
            skill['counterexamples'] += [x for x in update.get('counterexamples', []) if x not in skill['counterexamples']]
        else:
            raise PolicyError('Unclassified shared Skill update')
        skill['revision'] += 1
        skill['sources'] = sorted(set(skill['sources'] + [episode_id]))
        skill['touched_tasks'] = sorted(set(skill['touched_tasks'] + [task['id']]))
        skill['needs_cleanup'] = True
        self._save_skill(skill)

    def _new(self, task, episode, update):
        required = {'title', 'content', 'applicability', 'next_trigger'}
        if not required <= update.keys() or not all(isinstance(update[k], str) and update[k].strip() for k in required):
            raise PolicyError('Provisional Skill needs content, applicability and next trigger')
        skill = dict(id=uuid4().hex, title=update['title'], content=update['content'],
                     applicability=update['applicability'], next_trigger=update['next_trigger'],
                     owner_task_id=task['id'], visibility='task' if task.get('parent_id') else 'shared',
                     sources=[episode], status='provisional', revision=1, uses=[], counterexamples=[],
                     touched_tasks=[task['id']], needs_cleanup=True, effect='UNUSED_EFFECT_UNVERIFIED')
        self._save_skill(skill)
        return self.store.record_get('skill', skill['id'])

    def _skill_value(self, skill, previous):
        skill = deepcopy(redact(skill))
        if previous:
            if skill.get('owner_task_id', self._owner(previous)) != self._owner(previous):
                raise PolicyError('Skill ownership cannot be rewritten')
            skill.setdefault('owner_task_id', self._owner(previous))
            skill.setdefault('visibility', 'task' if self.store.get_task(skill['owner_task_id']).get('parent_id') else 'shared')
        skill['hash'] = digest({k: v for k, v in skill.items() if k != 'hash'})
        return skill

    def _save_skill(self, skill):
        previous = self.store.record_get('skill', skill['id'])
        skill = self._skill_value(skill, previous)
        if previous:
            self.store.record('skill_history', previous['id'] + ':' + previous['hash'], previous)
        self.store.record('skill', skill['id'], skill)

    def _merge_family(self, changes):
        key = digest({'kind': changes['kind'], 'classification': changes['classifications']})[:24]
        family = self.store.record_get('global_family', key) or {
            'id': key, 'kind': changes['kind'], 'episode_refs': [], 'skill_refs': [],
            'claim': 'Source-linked knowledge; effects require real consumer observations.'}
        self.store.record('global_family_history', digest(family), family)
        family['episode_refs'] = sorted(set(family['episode_refs'] + changes['episode_refs']))
        family['skill_refs'] = sorted(set(family['skill_refs'] + changes['skill_refs']))
        self.store.record('global_family', key, family)

    def _candidate(self, task, episode, updates, exports, family):
        primary = self._primary_id(task)
        exported = []
        for skill in exports:
            target_id = digest({'primary': primary, 'source_skill': skill['id']})[:32]
            existing = self.store.record_get('skill', target_id)
            exported.append({'target_id': target_id, 'expected_hash': existing['hash'] if existing else None,
                             'source_skill': deepcopy(skill)})
        payload = dict(primary_task_id=primary, source_task_id=task['id'],
                       source_parent_id=task['parent_id'], source_episode_id=episode['id'],
                       source_operation_id=episode['operation_id'], source_objective=task['objective'],
                       operation_sha256=digest(episode['operation']), result_sha256=digest(episode['result']),
                       source_episode_sha256=digest(episode), shared_updates=updates,
                       export_skills=exported, family=family)
        identity = digest(payload)[:32]
        candidate = {'id': identity, 'payload': payload, 'hash': digest(payload),
                     'status': 'pending', 'created_at': now()}
        self.store.record('knowledge_candidate', identity, candidate)
        return candidate

    @staticmethod
    def _describe_record(candidate):
        return {'candidate_id': candidate['id'], 'candidate_hash': candidate['hash'],
                'payload': candidate['payload'], 'status': candidate['status'],
                'state_hash': digest(candidate),
                'disposition': deepcopy(candidate.get('disposition'))}

    def describe_candidate(self, parent_task, candidate_id):
        parent = self._task(parent_task)
        candidate = self.store.record_get('knowledge_candidate', candidate_id)
        if parent.get('parent_id') or not candidate or candidate['payload']['primary_task_id'] != parent['id']:
            raise PolicyError('Only the owning primary parent may integrate this candidate')
        if digest(candidate['payload']) != candidate['hash']:
            raise PolicyError('Knowledge candidate bytes changed')
        source = self.store.get_task(candidate['payload']['source_task_id'])
        if self._primary_id(source) != parent['id'] or source.get('parent_id') != candidate['payload']['source_parent_id']:
            raise PolicyError('Knowledge candidate source ownership changed')
        episode = self.store.record_get('episode', candidate['payload']['source_episode_id'])
        if (not episode or digest(episode) != candidate['payload']['source_episode_sha256'] or
                episode['task_id'] != source['id'] or
                episode['operation_id'] != candidate['payload']['source_operation_id'] or
                digest(episode['operation']) != candidate['payload']['operation_sha256'] or
                digest(episode['result']) != candidate['payload']['result_sha256']):
            raise PolicyError('Knowledge candidate source evidence changed')
        targets = {}
        for update in candidate['payload']['shared_updates']:
            for identity in [update['id']] + update.get('merge_ids', []):
                targets[identity] = self.store.record_get('skill', identity)
        for exported in candidate['payload']['export_skills']:
            targets[exported['target_id']] = self.store.record_get('skill', exported['target_id'])
        return dict(deepcopy(self._describe_record(candidate)),
                    source_episode=deepcopy(episode), current_targets=targets)

    def describe_candidate_disposition(self, parent_task, args):
        parent = self._task(parent_task)
        required = {'candidate_id', 'expected_hash', 'expected_state_hash', 'disposition', 'reason'}
        if not isinstance(args, dict) or not required <= set(args) or set(args) - (required | {'next_trigger', 'replacement_payload'}):
            raise PolicyError('Candidate disposition needs exact source/state hashes and a reason')
        description = self.describe_candidate(parent, args['candidate_id'])
        if _hash(args['expected_hash'], 'Candidate hash') != description['candidate_hash'] or _hash(args['expected_state_hash'], 'Candidate state hash') != description['state_hash']:
            raise PolicyError('Candidate content or current disposition changed')
        if description['status'] not in {'pending', 'deferred', 'rejected'}:
            raise PolicyError('An integrated or superseded candidate cannot be discarded')
        disposition = args['disposition']
        if disposition not in {'reject', 'defer', 'rebase'} or not str(args['reason']).strip():
            raise PolicyError('Candidate disposition requires reasoned reject/defer/rebase')
        if disposition == 'defer' and not str(args.get('next_trigger', '')).strip():
            raise PolicyError('Deferred candidate needs a concrete return trigger')
        replacement = args.get('replacement_payload')
        if disposition != 'rebase' and replacement is not None:
            raise PolicyError('Replacement bytes require a rebase decision')
        if disposition == 'rebase':
            original = description['payload']
            if not isinstance(replacement, dict) or set(replacement) != set(original):
                raise PolicyError('Rebase needs the complete replacement payload')
            mutable = {'shared_updates', 'export_skills'}
            if any(replacement[key] != value for key, value in original.items() if key not in mutable):
                raise PolicyError('Rebase cannot rewrite original source or family lineage')
            original_exports = {x['target_id']: x for x in original['export_skills']}
            if (len(replacement['export_skills']) != len(original_exports) or
                    {x.get('target_id') for x in replacement['export_skills']} != set(original_exports)):
                raise PolicyError('Rebase cannot silently drop or substitute exported sources')
            for exported in replacement['export_skills']:
                prior = original_exports[exported['target_id']]
                if set(exported) != set(prior) or any(exported[k] != v for k, v in prior.items() if k != 'expected_hash'):
                    raise PolicyError('Rebase cannot replace the original exported Skill bytes')
                current = self.store.record_get('skill', exported['target_id'])
                if exported['expected_hash'] != (current['hash'] if current else None):
                    raise PolicyError('Rebase export is not bound to the actual current target')
            old_updates = {(u['id'], u['action']) for u in original['shared_updates']}
            new_updates = {(u.get('id'), u.get('action')) for u in replacement['shared_updates']}
            if old_updates != new_updates or len(new_updates) != len(replacement['shared_updates']):
                raise PolicyError('Rebase must retain each original shared change target/action')
            for update in replacement['shared_updates']:
                target = self.store.record_get('skill', update['id'])
                if target is None or not self._writable(parent, target):
                    raise PolicyError('Rebase target is outside parent write scope')
                self._check_update_cas(update, target)
                for identity in update.get('merge_ids', []):
                    merged = self.store.record_get('skill', identity)
                    if merged is None or not self._writable(parent, merged):
                        raise PolicyError('Rebase merge source is outside parent write scope')
                    description['current_targets'][identity] = merged
                if update['action'] == 'record_application':
                    previous = next(u for u in original['shared_updates'] if u['id'] == update['id'] and u['action'] == update['action'])
                    if update.get('application') != previous.get('application'):
                        raise PolicyError('Rebase cannot invent a different observed application')
        return {'candidate': description, 'request': deepcopy(args),
                'replacement_sha256': digest(replacement) if replacement is not None else None}

    def dispose_candidate(self, parent_task, operation_id, args):
        parent = self._task(parent_task)
        key, request_hash = parent['id'] + ':' + operation_id, digest(args)

        def commit():
            prior = self.store.record_get('knowledge_candidate_disposition', key)
            if prior:
                if prior['input_hash'] != request_hash:
                    raise PolicyError('Candidate disposition changed after its operation')
                return prior['outcome']
            description = self.describe_candidate_disposition(parent, args)
            row = self._reviewed_row(parent['id'], operation_id)
            if row['status'] != 'executing' or row['operation']['kind'] != 'candidate_disposition' or row['operation']['args'] != args:
                raise PolicyError('Candidate disposition needs the reviewed executing parent operation')
            if row['pre_bundle'].get('candidate') != description:
                raise PolicyError('Parent did not review this complete source/current/replacement cut')
            candidate = self.store.record_get('knowledge_candidate', args['candidate_id'])
            self.store.record('knowledge_candidate_history', candidate['id'] + ':' + digest(candidate), candidate)
            next_id = None
            if args['disposition'] == 'rebase':
                payload = deepcopy(args['replacement_payload'])
                payload['rebase'] = {'prior_candidate_id': candidate['id'], 'prior_candidate_hash': candidate['hash'],
                                     'prior_state_hash': digest(candidate), 'parent_operation_id': operation_id,
                                     'reason': args['reason']}
                next_id = digest(payload)[:32]
                self.store.record('knowledge_candidate', next_id,
                                  {'id': next_id, 'payload': payload, 'hash': digest(payload),
                                   'status': 'pending', 'created_at': now()})
                candidate.update(status='rebased', superseded_by=next_id)
            else:
                candidate['status'] = 'rejected' if args['disposition'] == 'reject' else 'deferred'
            candidate['disposition'] = {'operation_id': operation_id, 'parent_task_id': parent['id'],
                                        'reason': args['reason'], 'choice': args['disposition'],
                                        'next_trigger': args.get('next_trigger'), 'at': now()}
            self.store.record('knowledge_candidate', candidate['id'], candidate)
            outcome = {'candidate_id': candidate['id'], 'candidate_hash': candidate['hash'],
                       'status': candidate['status'], 'state_hash': digest(candidate),
                       'replacement_candidate_id': next_id, 'shared_change_applied': False}
            self.store.record('knowledge_candidate_disposition', key,
                              {'id': key, 'input_hash': request_hash, 'outcome': outcome})
            return outcome
        return self.store._transaction(commit)

    def integrate_candidate(self, parent_task, operation_id, candidate_id, expected_hash):
        parent = self._task(parent_task)
        expected_hash = _hash(expected_hash, 'Candidate expected hash')
        key = parent['id'] + ':' + operation_id
        request_hash = digest({'candidate_id': candidate_id, 'expected_hash': expected_hash})

        def commit():
            prior = self.store.record_get('knowledge_integration', key)
            if prior:
                if prior['input_hash'] != request_hash:
                    raise PolicyError('Knowledge integration operation changed')
                return prior['outcome']
            description = self.describe_candidate(parent, candidate_id)
            if description['candidate_hash'] != expected_hash or description['status'] != 'pending':
                raise PolicyError('Candidate changed or has already been consumed')
            row = self._reviewed_row(parent['id'], operation_id)
            operation = row['operation']
            args = operation['args']
            if operation['kind'] != 'knowledge_integrate' or args.get('candidate_id') != candidate_id or args.get('expected_hash') != expected_hash or row['status'] != 'executing':
                raise PolicyError('Candidate integration requires the actual reviewed executing parent operation')
            if row['pre_bundle'].get('candidate') != description:
                raise PolicyError('Parent did not review these complete candidate bytes')
            payload = description['payload']
            changed = []
            # Validate all CAS targets before any mutation; transaction covers the rest.
            for update in payload['shared_updates']:
                target = self.store.record_get('skill', update['id'])
                if target is None or not self._writable(parent, target):
                    raise PolicyError('Shared candidate target is unavailable')
                self._check_update_cas(update, target)
            for export in payload['export_skills']:
                target = self.store.record_get('skill', export['target_id'])
                if (target['hash'] if target else None) != export['expected_hash']:
                    raise PolicyError('Imported Skill changed since the child candidate was made')
                source = self._skill_version(export['source_skill']['id'], export['source_skill']['hash'])
                if source != export['source_skill']:
                    raise PolicyError('Exported Skill source bytes differ')
            for update in payload['shared_updates']:
                self._mutate_skill(parent, payload['source_episode_id'], update, validated=True)
                changed.append(update['id'])
            for export in payload['export_skills']:
                skill = deepcopy(export['source_skill'])
                existing = self.store.record_get('skill', export['target_id'])
                skill.update(id=export['target_id'], owner_task_id=parent['id'], visibility='shared',
                             imported_from={'task_id': payload['source_task_id'], 'skill_id': export['source_skill']['id'],
                                            'skill_hash': export['source_skill']['hash'], 'candidate_id': candidate_id},
                             touched_tasks=[parent['id']], needs_cleanup=True,
                             revision=(existing['revision'] + 1) if existing else 1)
                if existing:
                    skill['sources'] = sorted(set(skill['sources'] + existing['sources']))
                    skill['uses'] = existing['uses'] + [u for u in skill['uses'] if u not in existing['uses']]
                    skill['counterexamples'] = existing['counterexamples'] + [x for x in skill['counterexamples'] if x not in existing['counterexamples']]
                self._save_skill(skill)
                changed.append(skill['id'])
            family = deepcopy(payload['family'])
            family['skill_refs'] = sorted(set(changed))
            self._merge_family(family)
            candidate = self.store.record_get('knowledge_candidate', candidate_id)
            self.store.record('knowledge_candidate_history', candidate_id + ':' + digest(candidate), candidate)
            candidate.update(status='integrated', integration_operation_id=operation_id, integrated_at=now())
            self.store.record('knowledge_candidate', candidate_id, candidate)
            # The exact raw source was included in describe_candidate and reviewed
            # by its owning primary parent. Explicit private episodes stay private.
            self.store.record('episode_access', payload['source_episode_id'],
                              {'episode_id': payload['source_episode_id'],
                               'episode_sha256': payload['source_episode_sha256'],
                               'candidate_id': candidate_id, 'candidate_hash': expected_hash,
                               'parent_task_id': parent['id'], 'operation_id': operation_id})
            outcome = {'candidate_id': candidate_id, 'candidate_hash': expected_hash,
                       'source_task_id': payload['source_task_id'], 'source_episode_id': payload['source_episode_id'],
                       'integration_operation_id': operation_id, 'skills': sorted(set(changed)),
                       'parent_application_not_inferred': True}
            self.store.record('knowledge_integration', key,
                              {'id': key, 'input_hash': request_hash, 'outcome': outcome})
            return outcome

        outcome = self.store._transaction(commit)
        self._render_skills()
        return outcome

    def _render_skills(self):
        result = self.rebuild_projections()
        if not result['repaired']:
            raise PolicyError('Knowledge projection has unowned or changed files: ' + ', '.join(result['conflicts']))

    @staticmethod
    def _skill_markdown(skill):
        return (f"# {skill['title']}\n\nState: {skill['status']} / {skill['effect']}\n\n{skill['content']}\n\n"
                f"Apply when: {skill['applicability']}\n\nNext verification: {skill['next_trigger']}\n\n"
                f"Sources: {', '.join(skill['sources'])}\n").encode('utf-8')

    def _projection_target(self, identity):
        if '/' in relative_path(identity):
            raise PolicyError('Projection Skill ID must be one confined path segment')
        target = confined(self.root, 'skills/' + identity + '/SKILL.md', allow_missing=True)
        if target.exists() and not target.is_file():
            raise PolicyError('Projection target must be a regular file or exactly absent')
        return target

    @staticmethod
    def _projection_bytes(content):
        return {'sha256': hashlib.sha256(content).hexdigest() if content is not None else None,
                'bytes': len(content) if content is not None else 0,
                'base64': base64.b64encode(content).decode('ascii') if content is not None else None,
                'absent': content is None}

    def _projection_index(self):
        path = confined(self.root, 'projection-index.json', allow_missing=True)
        index = json.loads(path.read_text('utf-8')) if path.exists() else {'schema': 'knowledge-projection-v1', 'entries': {}}
        if index.get('schema') != 'knowledge-projection-v1' or not isinstance(index.get('entries'), dict):
            raise PolicyError('Invalid projection ownership index')
        return path, index

    def describe_projection_repair(self, task, args):
        """An exact registered target/preimage; a path or model hash is not authority."""
        if isinstance(args, dict) and args.get('mode') == 'cleanup_reconcile':
            with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
                return self._describe_cleanup_reconciliation(task, args)
        if (not isinstance(args, dict) or set(args) != {'skill_id', 'expected_skill_hash', 'expected_file_sha256', 'reason'} or
                not isinstance(args['reason'], str) or not args['reason'].strip()):
            raise PolicyError('Projection repair needs exact Skill/file hashes and a reason')
        with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
            current = self._task(task)
            skill = self.store.record_get('skill', args['skill_id'])
            if not skill or not self._writable(current, skill):
                raise PolicyError('Projection repair is outside writable Skill ownership')
            expected = _hash(args['expected_skill_hash'], 'Projection Skill hash')
            if skill['hash'] != expected or digest({k: v for k, v in skill.items() if k != 'hash'}) != expected:
                raise PolicyError('Projection Skill version changed')
            target = self._projection_target(skill['id'])
            _, index = self._projection_index()
            entry = index['entries'].get(skill['id'])
            # A first renderer failure may precede its ownership index entry.
            # The exact DB Skill grants only an absent target creation here;
            # an existing unregistered file remains outside repair authority.
            absent_registration = entry is None and not target.exists()
            if not absent_registration and (not isinstance(entry, dict) or
                    not isinstance(entry.get('allowed_sha256'), list) or not entry['allowed_sha256']):
                raise PolicyError('Projection target has no registered byte ownership')
            for value in (entry or {}).get('allowed_sha256', []):
                _hash(value, 'Managed projection bytes')
            before = self._projection_bytes(target.read_bytes() if target.exists() else None)
            file_hash = args['expected_file_sha256']
            if file_hash is not None:
                file_hash = _hash(file_hash, 'Projection file hash')
            if before['sha256'] != file_hash:
                raise PolicyError('Projection file changed since selection')
            value = {'schema': 'knowledge-projection-repair-v1', 'task_id': current['id'],
                     'skill_id': skill['id'], 'skill_hash': skill['hash'], 'skill_record_sha256': digest(skill),
                     'target': 'skills/' + skill['id'] + '/SKILL.md', 'projection_index_entry': deepcopy(entry),
                     'before': before, 'after': self._projection_bytes(self._skill_markdown(skill)), 'reason': args['reason']}
            return dict(value, candidate_sha256=digest(value))

    def _repair_location(self, task_id, operation_id, *, create=False):
        identity = digest({'task_id': task_id, 'operation_id': operation_id})
        relative = 'projection-repairs/' + identity[:32]
        directory = confined(self.root, relative, allow_missing=True)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return identity, directory, confined(self.root, relative + '/record.json', allow_missing=True)

    def _repair_record(self, task, operation_id):
        current = self._task(task)
        row = self._reviewed_row(current['id'], operation_id)
        if row['operation']['kind'] != 'knowledge_projection_repair':
            raise PolicyError('Projection repair needs its governed operation kind')
        identity, directory, path = self._repair_location(current['id'], operation_id)
        if not path.exists():
            return current, row, None, directory, path
        record = json.loads(path.read_text('utf-8'))
        candidate = record.get('candidate', {})
        if (record.get('schema') != 'knowledge-projection-receipt-v1' or record.get('id') != identity or
                record.get('task_id') != current['id'] or record.get('operation_id') != operation_id or
                record.get('action_sha256') != digest(row['operation']) or
                candidate.get('candidate_sha256') != digest({k: v for k, v in candidate.items() if k != 'candidate_sha256'}) or
                row['pre_bundle'].get('candidate') != candidate):
            raise PolicyError('Projection repair receipt/review identity changed')
        return current, row, record, directory, path

    def _projection_observation(self, record):
        target = self._projection_target(record['candidate']['skill_id'])
        content = target.read_bytes() if target.exists() else None
        info = target.stat() if target.exists() else None
        return {'sha256': self._projection_bytes(content)['sha256'], 'exists': content is not None,
                'file_identity': {'device': info.st_dev, 'inode': info.st_ino} if info else None}

    def _repair_result(self, record, *, status, effect, observation, fault=None):
        candidate = record['candidate']
        return OperationResult(operation_id=record['operation_id'], status=status, effect=effect,
            started_at=record['started_at'], finished_at=now(), stderr=fault['message'] if fault else '',
            data={'repair_receipt_id': record['id'], 'candidate_sha256': candidate['candidate_sha256'],
                  'skill_id': candidate['skill_id'], 'skill_hash': candidate['skill_hash'],
                  'before_sha256': candidate['before']['sha256'], 'after_sha256': candidate['after']['sha256'],
                  'original_preimage': candidate['before'], 'observed_projection': observation,
                  'projection_matches_database': status == 'succeeded', 'database_mutated': False,
                  'first_fault': fault, 'backup_ref': record.get('backup_ref'),
                  'proof_ceiling': 'Exact projection bytes at this recorded observation; no semantic improvement or task completion inferred.'})

    @staticmethod
    def _publish_projection(target, content, before_publish=None):
        """Atomic no-clobber publication, including a competing external creator."""
        temporary = confined(target.parent, '.' + uuid4().hex + '.tmp', allow_missing=True)
        try:
            with temporary.open('xb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
            if temporary.read_bytes() != content:
                raise PolicyError('Prepared projection bytes changed')
            if before_publish:
                before_publish({'device': info.st_dev, 'inode': info.st_ino})
            # Unlike replace(), link() refuses to overwrite an unexpected file.
            os.link(temporary, target)
        finally:
            if temporary.exists() and temporary.read_bytes() == content:
                temporary.unlink()

    def repair_projection(self, task, operation_id, args):
        if isinstance(args, dict) and args.get('mode') == 'cleanup_reconcile':
            with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
                return self._reconcile_cleanup(task, operation_id, args)
        current, row, prior, _, _ = self._repair_record(task, operation_id)
        if row['operation'].get('args') != args:
            raise PolicyError('Projection repair arguments differ from the reviewed operation')
        if prior is not None:
            return self.reconcile_projection_repair(current, operation_id)
        if row['status'] != 'executing':
            raise PolicyError('Projection repair is not the executing reviewed operation')
        candidate = self.describe_projection_repair(current, args)
        if row['pre_bundle'].get('candidate') != candidate:
            raise PolicyError('Projection repair differs from the exact reviewed preimage')
        with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
            # Revalidate mutable source/bytes under the writer lease; never trust
            # a describe call alone across a model/reviewer wait.
            skill = self.store.record_get('skill', candidate['skill_id'])
            target = self._projection_target(candidate['skill_id'])
            current_hash = self._projection_bytes(target.read_bytes() if target.exists() else None)['sha256']
            index_path, index = self._projection_index()
            if (digest(skill) != candidate['skill_record_sha256'] or current_hash != candidate['before']['sha256'] or
                    index['entries'].get(candidate['skill_id']) != candidate['projection_index_entry']):
                raise PolicyError('Projection repair source/file/index changed before execution')
            identity, directory, path = self._repair_location(current['id'], operation_id, create=True)
            if path.exists():
                raise PolicyError('Projection repair already has a protected execution receipt')
            record = {'schema': 'knowledge-projection-receipt-v1', 'id': identity, 'task_id': current['id'],
                      'operation_id': operation_id, 'action_sha256': digest(row['operation']), 'candidate': candidate,
                      'started_at': now(), 'phase': 'prepared', 'history': [], 'result': None}
            # The full preimage is durable before the original file can move.
            atomic_json(path, record)
            try:
                before, after = candidate['before'], candidate['after']
                if before['sha256'] != after['sha256']:
                    if not before['absent']:
                        backup = confined(directory, 'before.bin', allow_missing=True)
                        if backup.exists():
                            raise PolicyError('Protected original backup already exists')
                        target.rename(backup)
                        record['backup_ref'] = backup.relative_to(self.root).as_posix()
                        captured = self._projection_bytes(backup.read_bytes())
                        record['captured_sha256'] = captured['sha256']
                        record['phase'] = 'captured'
                        atomic_json(path, record)
                        if captured != before:
                            raise PolicyError('Projection changed during original-file capture; exact captured bytes preserved')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    def ready(file_identity):
                        record.update(phase='publication-prepared', publication_identity=file_identity)
                        atomic_json(path, record)
                    self._publish_projection(target, base64.b64decode(after['base64'], validate=True), ready)
                observation = self._projection_observation(record)
                if observation['sha256'] != after['sha256'] or digest(self.store.record_get('skill', skill['id'])) != candidate['skill_record_sha256']:
                    raise PolicyError('Projection repair readback/source changed')
                index['entries'][skill['id']] = {'allowed_sha256': [after['sha256']]}
                atomic_json(index_path, index)
                result = self._repair_result(record, status='succeeded',
                    effect='none' if before['sha256'] == after['sha256'] else 'confirmed', observation=observation)
                record.update(phase='complete', result=result.model_dump())
                atomic_json(path, record)
                return result
            except (OSError, PolicyError, ValueError) as error:
                fault = {'type': type(error).__name__, 'message': str(error)}
                restoration = {'attempted': False, 'restored': False}
                # Never overwrite a competing file. If capture removed the old
                # target, restore its exact captured bytes only into absence.
                try:
                    target = self._projection_target(candidate['skill_id'])
                    if not target.exists() and record.get('backup_ref'):
                        captured = confined(self.root, record['backup_ref']).read_bytes()
                        restoration['attempted'] = True
                        self._publish_projection(target, captured)
                        restoration['restored'] = target.read_bytes() == captured
                    observation = self._projection_observation(record)
                except (OSError, PolicyError, ValueError) as recovery_error:
                    observation = {'state': 'unknown'}
                    restoration['first_fault'] = {'type': type(recovery_error).__name__, 'message': str(recovery_error)}
                result = self._repair_result(record, status='failed', effect='unknown', observation=observation, fault=fault)
                result.data['preimage_restoration'] = restoration
                record.update(phase='failed', result=result.model_dump(), first_fault=fault)
                try:
                    atomic_json(path, record)
                except (OSError, PolicyError) as journal_error:
                    result.data['receipt_write_error'] = str(journal_error)
                return result

    def reconcile_projection_repair(self, task, operation_id):
        """Read-only recovery; never repeat a replacement or infer use from hashes alone."""
        action = self.store.get_operation(operation_id)
        if action['operation']['args'].get('mode') == 'cleanup_reconcile':
            current = self._task(task)
            row = self._reviewed_row(current['id'], operation_id)
            saved = self.store.record_get('cleanup_reconciliation', operation_id)
            if saved and saved['action_sha256'] == digest(row['operation']) and saved['task_id'] == current['id']:
                if saved['candidate'] != row['pre_bundle'].get('candidate'):
                    raise PolicyError('Cleanup recovery lost its original reviewed candidate')
                result = OperationResult.model_validate(saved['result'])
                resolution = self.store.record_get('cleanup_reconciliation_resolution', operation_id)
                if result.effect == 'unknown' and resolution:
                    successor = self._reviewed_row(current['id'], resolution['successor_operation_id'], post=True)
                    final = self.store.record_get('projection_failure_resolution', 'cleanup_failure:' + saved['candidate']['failure_id'])
                    if (resolution['original_result_sha256'] != digest(saved['result']) or not final or not final.get('complete') or
                            final.get('resolution_operation_id') != resolution['successor_operation_id'] or
                            final.get('result_sha256') != digest(successor['result']) or
                            not self._describe_cleanup_reconciliation(current, successor['operation']['args'])['established']):
                        raise PolicyError('Cleanup successor evidence or current readback changed')
                    result.effect = 'confirmed'
                    result.data = dict(result.data, original_effect='unknown', reconciled_by=resolution,
                        proof_ceiling='Later reviewed original-scope readback; original observation remains unknown.')
                return result
            return OperationResult(operation_id=operation_id, status='failed', effect='none',
                                   data={'cleanup_readback_not_recorded': True, 'cleanup_replayed': False})
        _, row, record, _, _ = self._repair_record(task, operation_id)
        if record is None:
            return OperationResult(operation_id=operation_id, status='failed', effect='none',
                                   data={'repair_not_started': True, 'target_mutation_precedes_receipt': False})
        if record.get('result') is not None:
            original = OperationResult.model_validate(record['result'])
            resolution = self.store.record_get('projection_repair_resolution', operation_id)
            if original.effect != 'unknown' or not resolution:
                return original
            # A later repair proves today's scoped state, not what happened in
            # the original failed execution. Preserve that unknown and first fault.
            successor = self._reviewed_row(record['task_id'], resolution['successor_operation_id'], post=True)
            confirmation = self.store.record_get('projection_repair_confirmation', resolution['successor_operation_id'])
            candidate = record['candidate']
            skill = self.store.record_get('skill', candidate['skill_id'])
            if (resolution['original_receipt_sha256'] != digest(record) or not confirmation or
                    resolution['successor_result_sha256'] != digest(successor['result']) or
                    confirmation['proof']['result_sha256'] != digest(successor['result']) or
                    not skill or not self._writable(self._task(task), skill) or
                    self._projection_target(skill['id']).read_bytes() != self._skill_markdown(skill)):
                raise PolicyError('Projection successor evidence/current readback changed')
            original.effect = 'confirmed'
            original.finished_at = now()
            original.data = dict(original.data, original_effect='unknown', reconciled_by=deepcopy(resolution),
                current_projection_sha256=hashlib.sha256(self._skill_markdown(skill)).hexdigest(),
                proof_ceiling='A later reviewed repair confirms the current target scope; the original execution effect remains unknown.')
            return original
        try:
            observation = self._projection_observation(record)
            candidate = record['candidate']
            unchanged = candidate['before']['sha256'] == candidate['after']['sha256']
            published = observation['file_identity'] == record.get('publication_identity')
            source = self.store.record_get('skill', candidate['skill_id'])
            if (observation['sha256'] == candidate['after']['sha256'] and
                    digest(source) == candidate['skill_record_sha256'] and (unchanged or published)):
                return self._repair_result(record, status='succeeded', effect='none' if unchanged else 'confirmed', observation=observation)
            fault = {'type': 'InterruptedProjectionRepair', 'message': 'Original repair publication is not established; preserve original bytes and use a newly reviewed repair.'}
        except (OSError, PolicyError, ValueError) as error:
            observation = {'state': 'unknown'}
            fault = {'type': type(error).__name__, 'message': str(error)}
        return self._repair_result(record, status='failed', effect='unknown', observation=observation, fault=fault)

    def _projection_dependants(self, task):
        """This actor and its actual descendants, never another primary task."""
        current = self._task(task)
        tasks = self.store.list_tasks()
        result, seen = [current], {current['id']}
        for parent in result:
            for child in tasks:
                if child.get('parent_id') == parent['id']:
                    if child['id'] in seen:
                        raise PolicyError('Projection recovery ownership cycle')
                    seen.add(child['id']); result.append(child)
        return result

    def projection_repair_queue(self, task):
        """Expose exact repair dependencies to their authorized ancestor.

        Only failure metadata and repairable Skill identities are returned. This
        does not expose a child's private episode or transfer semantic ownership.
        """
        current = self._task(task)
        return [failure for owner in self._projection_dependants(current)
                for failure in self.pending_projection_failures(owner, repair_actor=current)
                if owner['id'] == current['id'] or failure['repair_args']]

    def capture_cleanup_intent(self, task, operation, decisions):
        """Freeze the renderer's existing source set before cleanup can write anything."""
        current = self._task(task)
        identity = current['id'] + ':' + operation['id']
        previous = self.store.record_get('cleanup_intent', identity)
        if previous:
            if (previous['task_id'] != current['id'] or previous['operation_sha256'] != digest(operation)
                    or previous['decisions'] != decisions):
                raise PolicyError('Cleanup intent identity or choices changed')
            return previous
        sources = [{'id': s['id'], 'hash': s['hash'], 'record_sha256': digest(s)}
                   for s in sorted(self.store.records('skill'), key=lambda x: x['id'])]
        _, index = self._projection_index()
        orphans = []
        for orphan_id in sorted(set(index['entries']) - {s['id'] for s in sources}):
            target = self._projection_target(orphan_id)
            orphans.append({'id': orphan_id, 'index_entry': deepcopy(index['entries'][orphan_id]),
                            'file_sha256': self._projection_bytes(target.read_bytes() if target.exists() else None)['sha256']})
        intent = {'schema': 'cleanup-intent-v2', 'id': identity, 'task_id': current['id'],
                  'operation_id': operation['id'], 'operation_sha256': digest(operation),
                  'source_hash': current.get('source_hash'), 'decisions': deepcopy(decisions),
                  'before_hash': digest(self.snapshot(current['id'])), 'skills': sources,
                  'ideas': [{'id': i['id'], 'sha256': digest(i)} for i in self.store.records('idea')
                            if i['task_id'] == current['id']],
                  'orphan_projections': orphans, 'projection_index_sha256': digest(index),
                  'captured_at': now(), 'scope': 'Existing DB Skill sources, original task ideas and registered orphan projections; later sources are excluded.'}
        self.store.record('cleanup_intent', identity, intent)
        return intent

    def validate_cleanup_intent(self, task, operation, decisions, intent):
        current = self._task(task)
        sources = [{'id': s['id'], 'hash': s['hash'], 'record_sha256': digest(s)}
                   for s in sorted(self.store.records('skill'), key=lambda x: x['id'])]
        if (intent.get('task_id') != current['id'] or intent.get('operation_sha256') != digest(operation)
                or intent.get('decisions') != decisions or intent.get('source_hash') != current.get('source_hash')
                or intent.get('before_hash') != digest(self.snapshot(current['id'])) or intent.get('skills') != sources or
                intent.get('projection_index_sha256') != digest(self._projection_index()[1])):
            raise PolicyError('Cleanup source changed after its intent; require a new reviewed finish')

    def _cleanup_scope(self, current, failure):
        """Resolve only original protected sources, never today's growing Skill inventory."""
        row = self.store.get_operation(failure['operation_id'])
        if (row['task_id'] != current['id'] or row['operation']['kind'] != 'finish' or
                failure['id'] != current['id'] + ':' + row['operation']['id']):
            raise PolicyError('Cleanup failure is outside the original finish ownership')
        decisions = (row.get('finalization') or {}).get('cleanup', {}).get('decisions')
        basis = {'failure_sha256': digest(failure), 'operation_sha256': digest(row['operation']),
                 'decisions_sha256': digest(decisions)}
        reference = failure.get('cleanup_intent_ref')
        if reference:
            intent = self.store.record_get('cleanup_intent', reference['id'])
            valid = bool(intent and digest(intent) == reference['sha256'] and
                         intent.get('id') == failure['id'] and intent.get('task_id') == current['id'] and
                         intent.get('operation_sha256') == basis['operation_sha256'] and intent.get('decisions') == decisions)
            sources = intent.get('skills', []) if valid else []
            recovery = failure.get('projection_recovery', {})
            conflicts = recovery.get('conflicts', [])
            explicit = {x.split(':', 1)[0] for x in conflicts if ':' in x}
            transaction_known = failure.get('transaction_rolled_back') or failure.get('commit_readback', {}).get('identity_and_state_match')
            if (valid and transaction_known and conflicts and len(explicit) == len(conflicts) and
                    explicit <= {s['id'] for s in sources} and not recovery.get('error') and not recovery.get('scope_unknown')):
                sources = [s for s in sources if s['id'] in explicit]
            return {'known': valid, 'skill_ids': sorted({s['id'] for s in sources}),
                    'original_versions': sources, 'basis': dict(basis, cleanup_intent_ref=reference),
                    'orphan_projections': intent.get('orphan_projections', []) if valid else [],
                    'reason': 'Exact original cleanup intent' if valid else 'Original cleanup intent is missing or changed'}
        recovery = failure.get('projection_recovery', {})
        # Old failures with explicit targets/conflicts already carry a finite scope.
        if 'skill_ids' in failure:
            ids = failure['skill_ids']; known = isinstance(ids, list)
        else:
            conflicts = recovery.get('conflicts', [])
            explicit = [x.split(':', 1)[0] for x in conflicts if ':' in x]
            try:
                saved = self.store.record_get('cleanup_result', failure['id'])
            except OSError:
                saved = None
            saved_valid = bool(saved and saved.get('id') == failure['id'] and
                               saved.get('operation_id') == row['operation']['id'] and saved.get('decisions') == decisions and
                               isinstance(saved.get('skill_readback'), list))
            if saved_valid and not recovery.get('error') and not recovery.get('scope_unknown'):
                ids = [s['id'] for s in saved['skill_readback']] + list(recovery.get('rendered_skill_ids', [])) + explicit
                basis['cleanup_result_sha256'] = digest(saved)
                known = True
            else:
                ids = explicit
                known = bool(explicit) and len(explicit) == len(conflicts) and not recovery.get('scope_unknown')
        return {'known': known, 'skill_ids': sorted(set(ids)) if known else [], 'original_versions': [],
                'orphan_projections': [{'id': i, 'expected': 'absent'} for i in recovery.get('removed_orphan_ids', [])],
                'basis': basis, 'reason': 'Finite original legacy evidence' if known else 'Legacy original scope is not established; recover its protected source evidence'}

    def _describe_cleanup_reconciliation(self, task, args):
        if (set(args) != {'mode', 'failure_id', 'expected_failure_sha256', 'reason'} or
                not isinstance(args['reason'], str) or not args['reason'].strip()):
            raise PolicyError('Cleanup reconciliation needs exact original failure and reason')
        current = self._task(task)
        failure = self.store.record_get('cleanup_failure', args['failure_id'])
        if (not failure or (failure.get('task_id') or failure['id'].split(':', 1)[0]) != current['id'] or failure.get('effect') != 'unknown' or
                digest(failure) != _hash(args['expected_failure_sha256'], 'Cleanup failure hash')):
            raise PolicyError('Cleanup reconciliation failure ownership or version changed')
        scope = self._cleanup_scope(current, failure)
        readback_issue = None
        try:
            saved = self.store.record_get('cleanup_result', failure['id'])
        except OSError as error:
            saved = None
            readback_issue = {'reason': 'Committed cleanup readback unavailable', 'error': str(error)}
        row = self.store.get_operation(failure['operation_id'])
        decisions = (row.get('finalization') or {}).get('cleanup', {}).get('decisions')
        observations, issues = [], [readback_issue] if readback_issue else []
        if self.store.db.in_transaction:
            issues.append({'reason': 'Cleanup readback is inside an uncommitted transaction'})
        for identity in scope['skill_ids']:
            source = self.store.record_get('skill', identity)
            if not source:
                issues.append({'skill_id': identity, 'reason': 'Original source is unavailable'})
                continue
            try:
                target = self._projection_target(identity)
                content = target.read_bytes() if target.exists() else None
                matches = content == self._skill_markdown(source)
                observations.append({'skill_id': identity, 'skill_hash': source['hash'],
                                     'file_sha256': self._projection_bytes(content)['sha256'], 'matches': matches})
                if not matches: issues.append({'skill_id': identity, 'reason': 'Current projection differs from its DB source'})
            except (OSError, PolicyError) as error:
                issues.append({'skill_id': identity, 'reason': str(error)})
        for original in scope.get('orphan_projections', []):
            identity = original['id']
            try:
                target = self._projection_target(identity)
                raw = target.read_bytes() if target.exists() else None
                entry = self._projection_index()[1]['entries'].get(identity)
                absent = raw is None and entry is None
                unchanged = ('index_entry' in original and entry == original['index_entry'] and
                             self._projection_bytes(raw)['sha256'] == original['file_sha256'])
                matches = not self.store.record_get('skill', identity) and (absent or (saved is None and unchanged))
                observations.append({'orphan_id': identity, 'file_sha256': self._projection_bytes(raw)['sha256'],
                                     'index_entry': entry, 'matches': matches})
                if not matches: issues.append({'orphan_id': identity, 'reason': 'Original orphan projection state is not established'})
            except (OSError, PolicyError, ValueError) as error:
                issues.append({'orphan_id': identity, 'reason': str(error)})
        committed = bool(saved and saved.get('id') == failure['id'] and saved.get('operation_id') == row['operation']['id']
                         and saved.get('decisions') == decisions and not self.store.db.in_transaction)
        if committed:
            try:
                if failure.get('cleanup_intent_ref') and saved.get('cleanup_intent_sha256') != failure['cleanup_intent_ref']['sha256']:
                    raise PolicyError('Committed cleanup intent differs')
                for observed in saved['skill_readback']:
                    source = self._skill_version(observed['id'], observed['hash'])
                    if any(source.get(k) != v for k, v in observed.items()):
                        raise PolicyError('Original committed Skill readback differs from retained history')
            except (KeyError, PolicyError) as error:
                committed = False
                issues.append({'reason': str(error)})
        resolution = self.store.record_get('projection_failure_resolution', 'cleanup_failure:' + failure['id']) or {}
        repairs = resolution.get('repaired_skill_ids', []) if resolution.get('source_sha256') == digest(failure) else []
        repaired = bool(scope['known'] and scope['skill_ids'] and set(scope['skill_ids']) <= set(repairs))
        # An empty original renderer scope needs a successful explicit readback,
        # including unchanged semantic state if no committed result exists.
        intent_ref = failure.get('cleanup_intent_ref')
        intent = self.store.record_get('cleanup_intent', intent_ref['id']) if intent_ref else None
        empty_unchanged = bool(scope['known'] and not scope['skill_ids'] and saved is None and intent and
                               isinstance(intent.get('ideas'), list) and
                               all(digest(self.store.record_get('idea', i['id'])) == i['sha256'] for i in intent['ideas']) and
                               not self.store.db.in_transaction)
        established = scope['known'] and not issues and (committed or repaired or empty_unchanged)
        value = {'schema': 'cleanup-reconciliation-v1', 'task_id': current['id'], 'failure_id': failure['id'],
                 'failure_sha256': digest(failure), 'original_scope': scope, 'scope_sha256': digest(scope),
                 'cleanup_result_sha256': digest(saved) if saved else None, 'projections': observations,
                 'issues': issues, 'established': established, 'committed_readback': committed,
                 'basis': 'committed-cleanup-readback' if committed else 'finite-reviewed-repairs' if repaired else
                          'empty-original-scope-and-unchanged-source' if empty_unchanged else 'unresolved',
                 'original_effect': 'unknown', 'first_fault': failure['first_fault'], 'reason': args['reason']}
        return dict(value, candidate_sha256=digest(value))

    def _reconcile_cleanup(self, task, operation_id, args):
        current = self._task(task)
        row = self._reviewed_row(current['id'], operation_id)
        previous = self.store.record_get('cleanup_reconciliation', operation_id)
        if previous:
            if previous['action_sha256'] != digest(row['operation']):
                raise PolicyError('Cleanup reconciliation operation changed')
            return OperationResult.model_validate(previous['result'])
        if row['status'] != 'executing':
            raise PolicyError('Cleanup reconciliation is not the executing reviewed operation')
        candidate = self._describe_cleanup_reconciliation(current, args)
        if row['operation']['kind'] != 'knowledge_projection_repair' or row['operation']['args'] != args or row['pre_bundle'].get('candidate') != candidate:
            raise PolicyError('Cleanup reconciliation differs from the exact reviewed readback')
        result = OperationResult(operation_id=operation_id, status='succeeded' if candidate['established'] else 'unknown',
                                 effect='confirmed' if candidate['established'] else 'unknown', data=candidate)
        self.store.record('cleanup_reconciliation', operation_id, {'id': operation_id, 'task_id': current['id'],
                          'action_sha256': digest(row['operation']), 'candidate': candidate, 'result': result.model_dump()})
        return result

    def _cleanup_learning_transitions(self, current, row, before, after):
        """Bridge only this reviewed Learning's committed original-Skill changes."""
        if len(before) != len(after):
            raise PolicyError('Cleanup projection membership changed before confirmation')
        changed = [(old, actual) for old, actual in zip(before, after) if old != actual]
        if not changed:
            return None
        identity = current['id'] + ':' + row['operation']['id']
        application = self.store.record_get('knowledge_application', identity)
        selected = [s['id'] for s in row['pre_bundle']['skills']['selected']]
        learned = Learning.model_validate(row['post_bundle']['learning']).model_dump()
        input_hash = digest({'operation': row['operation'], 'learning': learned,
                             'result': row['result'], 'selected': selected})
        outcome = (application or {}).get('outcome') or {}
        receipt = outcome.get('skill_transitions') or {}
        episode = self.store.record_get('episode', outcome['episode_id']) if outcome.get('episode_id') else None
        if (not application or application.get('id') != identity or application.get('input_hash') != input_hash
                or row.get('knowledge') != outcome or not episode
                or episode.get('task_id') != current['id'] or episode.get('operation_id') != row['operation']['id']
                or episode.get('operation') != row['operation'] or episode.get('result') != row['result']
                or episode.get('learning') != learned or episode.get('selected_skills') != selected
                or receipt.get('schema') != 'knowledge-skill-transitions-v1'
                or receipt.get('task_id') != current['id'] or receipt.get('operation_id') != row['operation']['id']
                or receipt.get('input_hash') != input_hash or receipt.get('episode_id') != episode['id']):
            raise PolicyError('Cleanup change lacks its exact reviewed Learning application transition')
        entries = receipt.get('entries')
        if (not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get('skill_id'), str) for e in entries)
                or len({e['skill_id'] for e in entries}) != len(entries)):
            raise PolicyError('Cleanup Learning transition entries are missing or changed')
        by_id = {entry['skill_id']: entry for entry in entries}
        used = []
        for old, actual in changed:
            skill_id = old.get('skill_id')
            entry = by_id.get(skill_id)
            if (not skill_id or actual.get('skill_id') != skill_id or not entry
                    or not old.get('matches') or not actual.get('matches')):
                raise PolicyError('Cleanup projection change is outside its Learning transition')
            for observed, side in ((old, 'before'), (actual, 'after')):
                version = self._skill_version(skill_id, observed['skill_hash'])
                expected = {'hash': version['hash'], 'record_sha256': digest(version)}
                projection = {'skill_id': skill_id, 'skill_hash': version['hash'],
                              'file_sha256': self._projection_bytes(self._skill_markdown(version))['sha256'], 'matches': True}
                if (digest({k: v for k, v in version.items() if k != 'hash'}) != version['hash']
                        or entry.get(side) != expected or observed != projection):
                    raise PolicyError('Cleanup Learning version or projection changed outside its committed transition')
            used.append(deepcopy(entry))
        return {'knowledge_application_id': identity, 'input_hash': input_hash,
                'outcome_sha256': digest(outcome), 'transition_sha256': digest(receipt),
                'episode_id': episode['id'], 'entries': used}

    def _confirm_cleanup_reconciliation(self, task, operation_id):
        current = self._task(task)
        row = self._reviewed_row(current['id'], operation_id, post=True)
        saved = self.store.record_get('cleanup_reconciliation', operation_id)
        if (not saved or saved['task_id'] != current['id'] or saved['action_sha256'] != digest(row['operation']) or
                saved['result'].get('data') != row['result'].get('data') or row['result']['status'] != 'succeeded' or
                row['result']['effect'] != 'confirmed' or saved['result']['status'] != 'succeeded' or
                not saved['candidate']['established']):
            raise PolicyError('Cleanup resolution needs its exact successful reviewed readback')
        candidate = saved['candidate']
        actual = self._describe_cleanup_reconciliation(current, row['operation']['args'])
        # Original scope/commit evidence stays immutable. Lawful post Learning
        # may change an original Skill, but only its atomic application receipt
        # can bridge the reviewed readback to the current DB and file bytes.
        for key in ('failure_sha256', 'scope_sha256', 'cleanup_result_sha256'):
            if actual[key] != candidate[key]:
                raise PolicyError('Cleanup recovery scope or readback changed before confirmation')
        if not actual['established']:
            raise PolicyError('Cleanup readback is no longer established')
        transition = self._cleanup_learning_transitions(current, row, candidate['projections'], actual['projections'])
        key = 'cleanup_failure:' + candidate['failure_id']
        resolution = {'id': key, 'task_id': current['id'], 'repair_actor_id': current['id'],
                      'source_sha256': candidate['failure_sha256'], 'scope_sha256': candidate['scope_sha256'],
                      'repaired_skill_ids': candidate['original_scope']['skill_ids'], 'complete': True,
                      'original_effect': 'unknown', 'resolution_operation_id': operation_id,
                      'result_sha256': digest(row['result']), 'basis': candidate['basis'], 'observed_at': now(),
                       'proof_ceiling': 'Verified original commit or current finite scope; original fault and unknown record remain unchanged.'}
        if transition:
            resolution['learning_transition'] = transition
            resolution['confirmed_projections_sha256'] = digest(actual['projections'])
        def commit():
            previous = self.store.record_get('projection_failure_resolution', key)
            if previous and previous.get('resolution_operation_id') == operation_id:
                if previous.get('result_sha256') != digest(row['result']):
                    raise PolicyError('Cleanup confirmation result changed')
                return previous
            if previous:
                self.store.record('projection_resolution_history', digest(previous), previous)
            self.store.record('projection_failure_resolution', key, resolution)
            for old in self.store.operations(current['id']):
                args = old['operation']['args']
                if (old['operation']['id'] == operation_id or old['operation']['kind'] != 'knowledge_projection_repair' or
                        args.get('mode') != 'cleanup_reconcile' or args.get('failure_id') != candidate['failure_id'] or
                        (old.get('result') or {}).get('effect') != 'unknown'):
                    continue
                source = self.store.record_get('cleanup_reconciliation', old['operation']['id'])
                if source:
                    self.store.record('cleanup_reconciliation_resolution', old['operation']['id'],
                        {'original_result_sha256': digest(source['result']), 'original_effect': 'unknown',
                         'successor_operation_id': operation_id, 'successor_result_sha256': digest(row['result'])})
            return resolution
        return self.store._transaction(commit)

    def pending_projection_failures(self, task, *, repair_actor=None):
        """Original learning/cleanup failures plus separate reviewed resolutions."""
        current = self._task(task)
        actor = self._task(repair_actor) if repair_actor is not None else current
        if current['id'] not in {t['id'] for t in self._projection_dependants(actor)}:
            raise PolicyError('Projection recovery actor is not the failure owner or ancestor')
        result = []
        for kind in ('projection_failure', 'cleanup_failure'):
            for failure in self.store.records(kind):
                owner = failure.get('task_id') or (current['id'] if failure.get('id', '').startswith(current['id'] + ':') else None)
                if owner != current['id'] or (kind == 'cleanup_failure' and failure.get('effect') != 'unknown'):
                    continue
                key = kind + ':' + failure['id']
                resolution = self.store.record_get('projection_failure_resolution', key)
                recovery = failure.get('recovery', failure.get('projection_recovery', {}))
                if kind == 'cleanup_failure':
                    scope = self._cleanup_scope(current, failure)
                    skills = scope['skill_ids']
                else:
                    # An explicitly captured empty set is not today's inventory.
                    skills = failure.get('skill_ids')
                    if skills is None:
                        skills = [entry.split(':', 1)[0] for entry in recovery.get('conflicts', []) if ':' in entry]
                    scope = {'known': 'skill_ids' in failure or bool(skills), 'skill_ids': sorted(set(skills)),
                             'basis': {'failure_sha256': digest(failure)}, 'reason': 'Original projection failure targets'}
                if resolution and resolution.get('source_sha256') == digest(failure) and resolution.get('complete'):
                    if kind != 'cleanup_failure' or (scope['known'] and (
                            resolution.get('scope_sha256') == digest(scope) or
                            ('scope_sha256' not in resolution and set(skills) <= set(resolution.get('repaired_skill_ids', []))))):
                        continue
                resolved = (resolution or {}).get('repaired_skill_ids', [])
                repairs, unavailable = [], []
                for identity in sorted(set(skills) - set(resolved)):
                    skill = self.store.record_get('skill', identity)
                    if not skill or not self._writable(actor, skill):
                        unavailable.append({'skill_id': identity, 'reason': 'Outside writable ownership or source unavailable'})
                        continue
                    try:
                        target = self._projection_target(identity)
                        file_hash = self._projection_bytes(target.read_bytes() if target.exists() else None)['sha256']
                        repairs.append({'skill_id': identity, 'expected_skill_hash': skill['hash'], 'expected_file_sha256': file_hash,
                                        'reason': 'Restore this registered projection from the exact stored Skill and retain the observed original preimage.'})
                    except (OSError, PolicyError) as error:
                        unavailable.append({'skill_id': identity, 'reason': str(error)})
                result.append({'id': failure['id'], 'task_id': current['id'], 'failure_kind': kind, 'source_sha256': digest(failure),
                                'operation_id': failure['operation_id'], 'first_fault': failure['first_fault'],
                                'skill_ids': sorted(set(skills)), 'repair_args': repairs, 'unavailable': unavailable,
                                'resolved_skill_ids': resolved, 'status': 'pending', 'original_scope': scope,
                                'scope_sha256': digest(scope),
                                'cleanup_reconcile_args': {'mode': 'cleanup_reconcile', 'failure_id': failure['id'],
                                    'expected_failure_sha256': digest(failure), 'reason': 'Read back and review this exact original cleanup scope without replay.'}
                                    if kind == 'cleanup_failure' and current['id'] == actor['id'] else None})
        return result

    def confirm_projection_repair(self, task, operation_id):
        """Only exact accepted post-review may resolve prior failures; never rewrite them."""
        row = self.store.get_operation(operation_id)
        if row['operation']['args'].get('mode') == 'cleanup_reconcile':
            with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
                return self._confirm_cleanup_reconciliation(task, operation_id)
        current, _, record, _, _ = self._repair_record(task, operation_id)
        row = self._reviewed_row(current['id'], operation_id, post=True)
        actual = row['result']
        if not record or actual.get('status') != 'succeeded' or actual.get('effect') not in {'confirmed', 'none'}:
            raise PolicyError('Projection resolution needs its actual successful protected repair')
        protected = record.get('result') or self.reconcile_projection_repair(current, operation_id).model_dump()
        if (protected.get('status') != 'succeeded' or actual.get('effect') != protected['effect'] or
                digest(actual.get('data')) != digest(protected['data'])):
            raise PolicyError('Projection result differs from the protected repair outcome')
        identity = record['candidate']['skill_id']
        with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
            skill = self.store.record_get('skill', identity)
            if not skill or not self._writable(current, skill):
                raise PolicyError('Projection resolution lost writable source ownership')
            target = self._projection_target(identity)
            if not target.exists() or target.read_bytes() != self._skill_markdown(skill):
                raise PolicyError('Projection changed before reviewed resolution')
            proof = {'operation_id': operation_id, 'operation_sha256': digest(row['operation']),
                     'result_sha256': digest(actual), 'receipt_sha256': digest(record),
                     'skill_id': identity, 'current_skill_hash': skill['hash'], 'file_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
            def commit():
                confirmed = self.store.record_get('projection_repair_confirmation', operation_id)
                if confirmed:
                    if confirmed['proof']['result_sha256'] != proof['result_sha256']:
                        raise PolicyError('Projection confirmation result changed')
                    return confirmed
                resolved = []
                for failure in [f for dependent in self._projection_dependants(current)
                                for f in self.pending_projection_failures(dependent, repair_actor=current)]:
                    if identity not in failure['skill_ids']:
                        continue
                    key = failure['failure_kind'] + ':' + failure['id']
                    previous = self.store.record_get('projection_failure_resolution', key)
                    if previous and previous['source_sha256'] != failure['source_sha256']:
                        raise PolicyError('Original projection failure changed')
                    if previous:
                        self.store.record('projection_resolution_history', digest(previous), previous)
                    ids = sorted(set((previous or {}).get('repaired_skill_ids', [])) | {identity})
                    resolution = {'id': key, 'task_id': failure['task_id'], 'repair_actor_id': current['id'],
                                  'source_sha256': failure['source_sha256'], 'repaired_skill_ids': ids,
                                  'proofs': [*(previous or {}).get('proofs', []), proof],
                                  'scope_sha256': failure['scope_sha256'], 'original_scope': failure['original_scope'],
                                  'complete': failure['original_scope']['known'] and bool(failure['skill_ids']) and
                                              not failure['original_scope'].get('orphan_projections') and set(failure['skill_ids']) <= set(ids),
                                  'original_effect': 'unknown', 'observed_at': now()}
                    self.store.record('projection_failure_resolution', key, resolution)
                    if resolution['complete']:
                        resolved.append(key)
                resolved_operations = []
                for old in self.store.operations(current['id']):
                    old_id = old['operation']['id']
                    if (old_id == operation_id or old['operation']['kind'] != 'knowledge_projection_repair' or
                            (old.get('result') or {}).get('effect') != 'unknown' or
                            old['operation'].get('args', {}).get('skill_id') != identity):
                        continue
                    _, _, original, _, _ = self._repair_record(current, old_id)
                    if original and (original.get('result') or {}).get('effect') == 'unknown':
                        self.store.record('projection_repair_resolution', old_id,
                            {'id': old_id, 'original_receipt_sha256': digest(original),
                             'original_result_sha256': digest(old['result']), 'original_effect': 'unknown',
                             'successor_operation_id': operation_id, 'successor_result_sha256': digest(actual),
                             'current_readback': proof, 'proof_ceiling': 'Current target restored; original execution effects are not reconstructed.'})
                        resolved_operations.append(old_id)
                confirmation = {'id': operation_id, 'proof': proof, 'resolved_failure_ids': resolved,
                                'reconcilable_operation_ids': resolved_operations,
                                'original_failures_unchanged': True}
                self.store.record('projection_repair_confirmation', operation_id, confirmation)
                return confirmation
            return self.store._transaction(commit)

    def _replace_projection(self, target, expected, content):
        """Capture the actual original and publish only into an absent name.

        The preimage remains reachable even if an external writer changes the
        file between inspection and capture. No replace/unlink can discard it.
        Mechanical receipts are observations, not successful Skill-use evidence.
        """
        directory = confined(self.root, 'projection-preimages/' + uuid4().hex, allow_missing=True)
        directory.mkdir(parents=True, exist_ok=False)
        receipt_path, backup = directory / 'record.json', directory / 'before.bin'
        receipt = {'id': directory.name, 'target': target.relative_to(self.root).as_posix(),
                   'expected_sha256': expected,
                   'desired_sha256': hashlib.sha256(content).hexdigest() if content is not None else None,
                   'phase': 'prepared', 'created_at': now()}
        atomic_json(receipt_path, receipt)
        try:
            if expected is not None:
                target.rename(backup)
                captured = hashlib.sha256(backup.read_bytes()).hexdigest()
                receipt.update(phase='captured', captured_sha256=captured,
                               backup_ref=backup.relative_to(self.root).as_posix())
                atomic_json(receipt_path, receipt)
                if captured != expected:
                    raise PolicyError('Projection changed during capture; actual preimage retained')
            if content is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._publish_projection(target, content)
                if target.read_bytes() != content:
                    raise PolicyError('Projection changed after no-clobber publication')
            elif target.exists():
                raise PolicyError('A competing file appeared during orphan removal')
            receipt.update(phase='complete', observed_at=now())
            atomic_json(receipt_path, receipt)
            return receipt_path.relative_to(self.root).as_posix()
        except (OSError, PolicyError, ValueError) as error:
            receipt.update(phase='failed', first_fault={'type': type(error).__name__, 'message': str(error)})
            if backup.exists() and not target.exists():
                try:
                    self._publish_projection(target, backup.read_bytes())
                    receipt['preimage_restored'] = target.read_bytes() == backup.read_bytes()
                except (OSError, PolicyError) as restoration_error:
                    receipt['restoration_fault'] = {'type': type(restoration_error).__name__, 'message': str(restoration_error)}
            try:
                atomic_json(receipt_path, receipt)
            except (OSError, PolicyError):
                pass  # The durable prepared receipt and captured file remain.
            error.projection_receipt_ref = receipt_path.relative_to(self.root).as_posix()
            raise

    def rebuild_projections(self):
        """Repair DB-derived files after rollback without replaying semantic writes.

        The mechanical index tracks only exact bytes written by this renderer. An
        interrupted replacement can leave its old or proposed bytes; both remain
        eligible for repair. Unknown or externally changed files are preserved.
        Call after the outer transaction has rolled back, under controller ownership.
        """
        with self.store.lock, process_lock(confined(self.root, 'projection.lock', allow_missing=True)):
            path = confined(self.root, 'projection-index.json', allow_missing=True)
            index = json.loads(path.read_text('utf-8')) if path.exists() else {'schema': 'knowledge-projection-v1', 'entries': {}}
            if index.get('schema') != 'knowledge-projection-v1' or not isinstance(index.get('entries'), dict):
                raise PolicyError('Knowledge projection index is invalid; preserve its files')
            entries = index['entries']
            skills = {s['id']: s for s in self.store.records('skill')}
            for identity in set(entries) | set(skills):
                if '/' in relative_path(identity):
                    raise PolicyError('Knowledge projection ID must be one confined path segment')
            rendered, removed, conflicts, preimages = [], [], [], []
            base = confined(self.root, 'skills', allow_missing=True)
            base.mkdir(exist_ok=True)
            # An unindexed orphan is not ours to delete and cannot be called repaired.
            for directory in base.iterdir():
                safe = confined(base, directory.name)
                if safe.is_dir() and directory.name not in entries and directory.name not in skills:
                    orphan = confined(safe, 'SKILL.md', allow_missing=True)
                    if orphan.exists():
                        conflicts.append(directory.name + ':unmanaged-orphan')
            for identity in sorted(set(entries) | set(skills)):
                target = confined(self.root, f'skills/{identity}/SKILL.md', allow_missing=True)
                actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
                tracked = entries.get(identity, {})
                allowed = tracked.get('allowed_sha256', [])
                if not isinstance(allowed, list):
                    raise PolicyError('Knowledge projection byte ownership is invalid')
                for value in allowed:
                    _hash(value, 'Projection managed bytes')
                if identity not in skills:
                    if actual is not None and actual not in allowed:
                        conflicts.append(identity + ':changed-orphan')
                        continue
                    if actual is not None:
                        try:
                            preimages.append(self._replace_projection(target, actual, None))
                        except (OSError, PolicyError, ValueError) as error:
                            error.projection_skill_id = identity
                            raise
                        removed.append(identity)
                    entries.pop(identity, None)
                    atomic_json(path, index)
                    continue
                content = self._skill_markdown(skills[identity])
                desired = hashlib.sha256(content).hexdigest()
                # Accept the original renderer's native newline projection only
                # when its complete bytes equal the current authoritative Skill.
                legacy = hashlib.sha256(content.replace(b'\n', os.linesep.encode())).hexdigest()
                if actual is not None and actual not in {*allowed, desired, legacy}:
                    conflicts.append(identity + ':changed-projection')
                    continue
                entries[identity] = {'allowed_sha256': sorted({desired} | ({actual} if actual else set()))}
                atomic_json(path, index)  # Commit possible effects before replacement.
                if actual != desired:
                    try:
                        preimages.append(self._replace_projection(target, actual, content))
                    except (OSError, PolicyError, ValueError) as error:
                        error.projection_skill_id = identity
                        raise
                if hashlib.sha256(target.read_bytes()).hexdigest() != desired:
                    raise PolicyError('Knowledge projection readback differs')
                entries[identity] = {'allowed_sha256': [desired]}
                atomic_json(path, index)
                rendered.append(identity)
            return {'repaired': not conflicts, 'rendered_skill_ids': rendered, 'removed_orphan_ids': removed,
                    'preimage_receipts': preimages,
                    'conflicts': conflicts, 'semantic_writes_replayed': False,
                    'proof_ceiling': 'Authoritative DB projection repair; no task or improvement outcome inferred.'}

    def finish(self, task_id, decisions, main_achieved):
        outcome = self.store._transaction(lambda: self._finish(task_id, decisions, main_achieved))
        self._render_skills()
        return outcome

    def _finish(self, task_id, decisions, main_achieved):
        task = self._task(task_id)
        if self.pending_projection_failures(task):
            raise PolicyError('Projection failures require an actual reviewed repair before finish')
        touched = {s['id']: s for s in self.store.records('skill')
                   if task_id in s['touched_tasks'] and s['needs_cleanup'] and self._writable(task, s)}
        entries = {d.get('id'): d for d in decisions}
        if len(entries) != len(decisions) or set(entries) != set(touched):
            raise PolicyError('Finish must account for every created/updated Skill exactly once')
        for identity, skill in touched.items():
            choice = entries[identity]
            if choice.get('disposition') not in {'retain', 'retire'} or not choice.get('reason'):
                raise PolicyError('Cleanup needs reasoned retain/retire; merge/organize before finish')
            if choice['disposition'] == 'retire':
                skill.update(status='retired', retirement_reason=choice['reason'])
            skill['cleanup'], skill['needs_cleanup'] = choice, False
            skill['effect'] = 'UNUSED_EFFECT_UNVERIFIED' if not skill['uses'] else 'USED_EFFECT_UNVERIFIED'
            self._save_skill(skill)
        unfinished = []
        for idea in self.store.records('idea'):
            if idea['task_id'] != task_id or idea['status'] in {'rejected', 'verified', 'deferred'}:
                continue
            if main_achieved and idea['target'] == 'task' and not self.optional_deferral_enabled():
                self.store.record('idea_history', uuid4().hex, idea)
                idea['status'] = 'deferred'
                self.store.record('idea', idea['id'], idea)
                self.store.record('deferred_idea_index', idea['id'], self._deferred_source(idea))
            else:
                unfinished.append(idea['id'])
        if unfinished:
            raise PolicyError('Unresolved improvements need a reasoned disposition; executed adopted changes still require validation and actual use: ' + ','.join(unfinished))
        return {'skills_cleaned': list(touched), 'unverified_effects_explicit': True}

    def snapshot(self, task_id):
        task = self._task(task_id)
        return {'skills': [s for s in self.store.records('skill')
                           if task_id in s['touched_tasks'] and self._writable(task, s)],
                'ideas': [i for i in self.store.records('idea') if i['task_id'] == task_id],
                'episodes': [e for e in self.store.records('episode') if e['task_id'] == task_id],
                'applications': [a for a in self.store.records('skill_application') if a['task_id'] == task_id],
                'candidates': [self._describe_record(c) for c in self.store.records('knowledge_candidate')
                               if c['payload']['source_task_id'] == task_id or
                               (not task.get('parent_id') and c['payload']['primary_task_id'] == task_id)]}

    def resolve_ideas(self, task_id, decisions, operation_id=None):
        def commit():
            identity = task_id + ':' + operation_id if operation_id else None
            prior = self.store.record_get('idea_resolution', identity) if identity else None
            if prior:
                if prior['input_hash'] != digest(decisions):
                    raise PolicyError('Improvement disposition changed after application')
                return prior['outcome']
            outcome = self._resolve_ideas(task_id, decisions)
            if identity:
                self.store.record('idea_resolution', identity,
                                  {'id': identity, 'input_hash': digest(decisions), 'outcome': outcome})
            return outcome
        return self.store._transaction(commit)

    def _improvement_evidence(self, task_id, idea, operation_id, candidate, role):
        row = self._reviewed_row(task_id, operation_id, post=True)
        operation, result = row['operation'], row.get('result') or {}
        if operation['kind'] in {'plan_task', 'knowledge_update', 'finish', 'wait_children', 'reconcile'}:
            raise PolicyError('Improvement evidence needs a real implementation/test/consumer operation')
        if role == 'use' and operation['kind'] in {'prepare_update', 'verify_update', 'activate_update', 'rollback_update',
                                                  'controller_read', 'adopt_policy', 'candidate_disposition', 'judgment_resolve'}:
            raise PolicyError('Update, activation, inspection or disposition alone is not actual ordinary use')
        if result.get('operation_id') != operation_id or result.get('effect') == 'unknown':
            raise PolicyError('Improvement evidence has unknown or foreign effects')
        bindings = [b for b in operation.get('improvement_bindings', [])
                    if b.get('idea_id') == idea['id'] and b.get('role') == role and b.get('candidate_sha256') == candidate]
        if len(bindings) != 1:
            raise PolicyError('Improvement role/candidate was not bound exactly once before execution')
        binding = bindings[0]
        required = {'idea_id', 'candidate_sha256', 'role', 'result_pointer', 'source_pointer', 'source_sha256', 'claim'}
        if set(binding) - (required | {'expected_status'}) or not required <= set(binding) or not str(binding['claim']).strip():
            raise PolicyError('Improvement binding schema is incomplete')
        expected = binding.get('expected_status', 'succeeded')
        if expected not in {'succeeded', 'failed'} or (expected == 'failed' and role != 'verification') or result.get('status') != expected:
            raise PolicyError('Improvement result does not satisfy its bound oracle status')
        # A negative verification can legitimately expect failure. Its exact claim
        # still needs post review; a failed use is not a verified improvement.
        if _hash(binding['source_sha256'], 'Observed source hash') != candidate:
            raise PolicyError('Improvement source is not the implementation candidate')
        actual_source = _pointer(result, binding['source_pointer'])
        if not isinstance(actual_source, str) or actual_source.lower() != candidate:
            raise PolicyError('Actual result does not attest the bound candidate source hash')
        pointer = binding['result_pointer']
        if not (pointer in {'/stdout', '/stderr', '/data', '/artifacts'} or pointer.startswith(('/data/', '/artifacts/'))):
            raise PolicyError('Improvement result pointer needs actual output evidence')
        observed = _pointer(result, pointer)
        start, finish = _time(result.get('started_at')), _time(result.get('finished_at'))
        if start < self._idea_time(idea) or finish < start:
            raise PolicyError('Improvement evidence predates the idea or has invalid execution order')
        return {'operation_id': operation_id, 'role': role, 'operation_sha256': digest(operation),
                'result_sha256': digest(result), 'candidate_sha256': candidate,
                'result_pointer': pointer, 'observed_sha256': digest(observed),
                'started_at': result['started_at'], 'finished_at': result['finished_at']}

    def _trusted_runtime(self, task_id, operation_id):
        row = self._reviewed_row(task_id, operation_id, post=True)
        operation, result = row['operation'], row.get('result') or {}
        if not callable(self.runtime_evidence):
            raise PolicyError('Controller improvement requires protected runtime records')
        record = self.runtime_evidence(task_id, operation_id)
        runtime = result.get('controller_runtime')
        raw = {k: v for k, v in result.items() if k != 'controller_runtime'}
        if (not isinstance(record, dict) or record.get('status') != 'recorded' or
                record.get('task_id') != task_id or record.get('operation_id') != operation_id or
                record.get('operation_sha256') != digest(operation) or record.get('result_sha256') != digest(raw) or
                not isinstance(runtime, dict) or record.get('controller_runtime') != runtime or
                runtime.get('operation_sha256') != digest(operation) or runtime.get('result_sha256') != digest(raw) or
                not runtime.get('record_id') or runtime.get('record_id') != record.get('record_id')):
            raise PolicyError('Controller runtime record does not bind these actual operation/result bytes')
        _hash(runtime.get('loaded_source_version'), 'Loaded controller source')
        if (type(runtime.get('pid')) is not int or runtime['pid'] <= 0 or
                not isinstance(runtime.get('process_id'), str) or not runtime['process_id']):
            raise PolicyError('Controller runtime has no actual process identity')
        return row, runtime, record

    def _is_controller_candidate(self, task_id, decision, candidate):
        if decision.get('activation_operation_id'):
            return True
        referenced = set(decision.get('implementation_operation_ids', []) + decision.get('verification_operation_ids', []) +
                         decision.get('actual_use_operation_ids', []))
        # A controller candidate remains one even if a caller omits its stage from
        # the resolution. Its protected operation history is not model classification.
        for row in self.store.operations(task_id):
            operation = row['operation']
            if operation['kind'] not in {'prepare_update', 'verify_update', 'activate_update', 'rollback_update'}:
                continue
            result = row.get('result') or {}
            args, data = operation.get('args', {}), result.get('data', {})
            if (operation['id'] in referenced or candidate in {args.get('expected_candidate_sha256'), args.get('candidate_id'),
                                                              data.get('candidate_id'), data.get('candidate_sha256')}):
                return True
        return False

    def _controller_update_evidence(self, task_id, idea, decision, candidate, evidence):
        activation_id = decision.get('activation_operation_id')
        if not isinstance(activation_id, str) or not activation_id:
            raise PolicyError('Controller improvement needs the exact activation_operation_id')
        activation, activated, _ = self._trusted_runtime(task_id, activation_id)
        op, result = activation['operation'], activation['result']
        if (op['kind'] != 'activate_update' or result.get('status') != 'succeeded' or result.get('effect') != 'confirmed' or
                op.get('args', {}).get('candidate_id') != activated.get('activation_candidate_id') or
                result.get('data', {}).get('candidate_id') != activated.get('activation_candidate_id') or
                activated.get('candidate_sha256') != candidate or activated.get('activation_status') != 'activated' or
                activated.get('activation_pid') != activated['pid'] or
                activated.get('activation_process_id') != activated['process_id']):
            raise PolicyError('Controller activation is not the confirmed reviewed candidate')
        expected = _hash(activated.get('expected_source_version'), 'Post-restart source')
        _hash(activated.get('activation_sha256'), 'Activation source record')
        activation_start, activation_finish = _time(result['started_at']), _time(result['finished_at'])
        if activation_start < self._idea_time(idea) or activation_finish < activation_start:
            raise PolicyError('Activation predates the idea or has invalid execution order')
        stage_ids, verification_ids = [], []
        for role, kind in [('implementation', 'prepare_update'), ('verification', 'verify_update')]:
            matching = [item for item in evidence[role] if self.store.get_operation(item['operation_id'])['operation']['kind'] == kind]
            if not matching:
                raise PolicyError('Controller improvement needs reviewed prepare_update and verify_update evidence')
            for item in matching:
                checked, _, _ = self._trusted_runtime(task_id, item['operation_id'])
                action, observed = checked['operation'], checked['result']
                if kind == 'prepare_update':
                    # Engine keeps the reviewed public operation immutable and
                    # injects this candidate hash only into its trusted stage call.
                    # A model-supplied original argument is not the source authority.
                    described = checked['pre_bundle'].get('candidate')
                    if not isinstance(described, dict):
                        raise PolicyError('Controller preparation has no reviewed candidate bytes')
                    source = _hash(described.get('candidate_sha256'), 'Reviewed controller candidate')
                    if (digest({k: v for k, v in described.items() if k != 'candidate_sha256'}) != source or
                            described.get('after_version') != expected or not observed.get('data', {}).get('staged')):
                        raise PolicyError('Controller reviewed candidate bytes or staged result differ')
                    if observed['data'].get('candidate_sha256', source) != source:
                        raise PolicyError('Controller staged candidate hash differs from the reviewed bytes')
                else:
                    source = action['args'].get('candidate_id')
                if source != candidate or observed.get('data', {}).get('candidate_id') != candidate:
                    raise PolicyError('Controller preparation/verification belongs to another candidate')
                if kind == 'prepare_update':
                    stage_ids.append(item['operation_id'])
                else:
                    verification_ids.append(item['operation_id'])
        if any(_time(item['finished_at']) > activation_start for item in evidence['implementation'] + evidence['verification']):
            raise PolicyError('Controller activation must follow implementation and verification')
        uses = []
        for item in evidence['use']:
            used, runtime, record = self._trusted_runtime(task_id, item['operation_id'])
            actual = used['result']
            if (actual.get('status') != 'succeeded' or not ordinary_use_result_observed(used['operation']['kind'], actual) or
                    runtime.get('normal_use') != 'observed' or record.get('normal_use', {}).get('status') != 'observed' or
                    runtime.get('candidate_sha256') != candidate or runtime.get('loaded_source_version') != expected or
                    runtime.get('expected_source_version') != expected or
                    runtime.get('activation_candidate_id') != activated['activation_candidate_id'] or
                    runtime.get('activation_sha256') != activated['activation_sha256'] or
                    runtime.get('activation_status') != 'activated' or
                    runtime.get('activation_pid') != activated['pid'] or runtime.get('activation_process_id') != activated['process_id'] or
                    runtime['pid'] == activated['pid'] or runtime['process_id'] == activated['process_id'] or
                    _time(actual['started_at']) < activation_finish):
                raise PolicyError('Controller actual use needs the matching post-restart source and a different process')
            binding = next(b for b in used['operation']['improvement_bindings']
                           if b.get('idea_id') == idea['id'] and b.get('role') == 'use' and b.get('candidate_sha256') == candidate)
            if binding['source_pointer'] != '/controller_runtime/candidate_sha256':
                raise PolicyError('Controller use source must come from protected runtime, never executor data or arguments')
            uses.append({'operation_id': item['operation_id'], 'runtime_record_id': runtime['record_id'],
                         'runtime_record_sha256': digest(record), 'loaded_source_version': runtime['loaded_source_version'],
                         'pid': runtime['pid'], 'process_id': runtime['process_id']})
        return {'activation_operation_id': activation_id, 'activation_result_sha256': digest(result),
                'candidate_sha256': candidate, 'expected_source_version': expected,
                'prepare_operation_ids': stage_ids, 'verification_operation_ids': verification_ids,
                'actual_use': uses, 'empirical_benefit': 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'}

    def _optional_deferral(self, task, idea, decision):
        if not self.optional_deferral_enabled():
            raise PolicyError('Optional deferral requires the approved scoped-delivery policy')
        deferred = validate_optional_deferral(decision.get('deferral'), decision.get('reason'))
        if idea['status'] not in {'pending', 'deferred'}:
            raise PolicyError('Deferral cannot replace a completed Idea disposition')
        bound = [row for row in self.store.operations(task['id'])
                 if any(b.get('idea_id') == idea['id'] for b in row['operation'].get('improvement_bindings', []))]
        if (any(idea.get(key) for key in ('implementation_refs', 'verification_refs', 'actual_use_refs'))
                or any(row.get('result') is not None or self.store.operation_started_without_result(row)
                       for row in bound)):
            raise PolicyError('An executed or unknown adopted change cannot defer its required verification/use')
        return deferred

    def describe_idea_resolution(self, task, decisions):
        """Original targets reach the existing pre-review before disposition."""
        task = self._task(task)
        if not decisions or len({d.get('id') for d in decisions}) != len(decisions):
            raise PolicyError('Improvement resolution needs distinct targets')
        originals = []
        for decision in decisions:
            idea = self.store.record_get('idea', decision.get('id'))
            if not idea or idea['task_id'] != task['id'] or not decision.get('reason'):
                raise PolicyError('Improvement target is not owned or reason is missing')
            if decision.get('disposition') == 'defer':
                self._optional_deferral(task, idea, decision)
            originals.append(deepcopy(idea))
        return {'ideas': originals, 'decisions': deepcopy(decisions),
                'source_hash': task['source_hash'], 'mandatory_acceptance': task['acceptance'],
                'instruction': 'Judge each reason and current mandatory-outcome impact against the exact original. Optional deferral does not prove completion or erase any adopted change, unknown effect or pending mandatory correction.'}

    def _resolve_ideas(self, task_id, decisions):
        if not decisions or len({d.get('id') for d in decisions}) != len(decisions):
            raise PolicyError('Improvement resolution needs distinct targets and evidence')
        changed = []
        for decision in decisions:
            idea = self.store.record_get('idea', decision.get('id'))
            if not idea or idea['task_id'] != task_id or not decision.get('reason'):
                raise PolicyError('Improvement target is not owned or reason is missing')
            self.store.record('idea_history', uuid4().hex, idea)
            disposition = decision.get('disposition')
            if disposition == 'reject':
                idea.update(status='rejected', resolution_reason=decision['reason'])
            elif disposition == 'defer':
                task = self._task(task_id)
                deferred = self._optional_deferral(task, idea, decision)
                idea.update(status='deferred', deferral=deferred, resolution_reason=decision['reason'],
                            next_trigger=deferred['next_trigger'], deferral_source_hash=task['source_hash'],
                            deferral_acceptance_hash=digest(task['acceptance']),
                            deferral_policy_hash=task.get('policy_hash'))
            elif disposition == 'verified':
                candidate = _hash(decision.get('candidate_sha256'), 'Improvement candidate')
                evidence = {}
                for role, field in [('implementation', 'implementation_operation_ids'),
                                    ('verification', 'verification_operation_ids'), ('use', 'actual_use_operation_ids')]:
                    ids = decision.get(field, [])
                    if not ids or len(set(ids)) != len(ids):
                        raise PolicyError('Improvement needs distinct implementation, verification and use evidence')
                    evidence[role] = [self._improvement_evidence(task_id, idea, i, candidate, role) for i in ids]
                implemented = max(_time(e['finished_at']) for e in evidence['implementation'])
                for item in evidence['verification'] + evidence['use']:
                    if _time(item['started_at']) < implemented:
                        raise PolicyError('Verification/use ran before the candidate was implemented')
                controller = self._controller_update_evidence(task_id, idea, decision, candidate, evidence) if self._is_controller_candidate(task_id, decision, candidate) else None
                if controller is not None:
                    evidence['controller_update'] = controller
                idea.update(status='verified', candidate_sha256=candidate, evidence=evidence,
                            implementation_refs=decision['implementation_operation_ids'],
                            verification_refs=decision['verification_operation_ids'],
                            actual_use_refs=decision['actual_use_operation_ids'],
                            resolution_reason=decision['reason'], semantic_effect='UNVERIFIED_UNTIL_MEASURED_COMPARISON',
                            reported_effect=decision.get('effect', 'UNVERIFIED'))
            else:
                raise PolicyError('Resolve by reasoned rejection, approved optional deferral or bound verification; other work remains open')
            self.store.record('idea', idea['id'], idea)
            if idea['status'] == 'deferred':
                self.store.record('deferred_idea_index', idea['id'], self._deferred_source(idea))
            changed.append(idea['id'])
        return {'resolved_idea_ids': changed, 'measured_benefit_not_inferred': True}
