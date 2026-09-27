"""One source-owned reference domain, without changing the response schema."""
from collections import Counter
import json

from .models import PolicyError


VERSION = 'disposition-reference-feedback-v1'
# Keep the previous normal producer bytes. New diagnostics belong to a new
# corrective request, never to a historical request or its accepted response.
INSTRUCTION = 'Return exactly one reasoned opinion_responses entry for each listed opinion_id. web_refs must be exactly the listed IDs, once each, with no URL, title or explanation substituted. When the list is empty, web_refs must be []. A prepared acquisition is not acquired source evidence. Discuss relevant facts and limits in rationale. Choose accept/reject and proceed/revise/hold yourself under the current source; these ID constraints do not decide your judgment.'
DOMAIN = ('web_refs names only members of sources/web.sources resolved for THIS response. '
          'Review opinions belong to opinion_responses.opinion_id. Acquisition, exchange, '
          'operation, history, context-packet and transport-fragment IDs are not acquired '
          'source IDs merely because they appear elsewhere. Empty expected means []. '
          'Retain the substantive verdict and reasoned responses; do not infer a patch.')


class DispositionContractError(PolicyError):
    def __init__(self, violations):
        self.diagnostic = {'kind': 'disposition_contract', 'version': VERSION,
                           'violations': violations, 'source_domain': DOMAIN}
        super().__init__('Disposition exact reference contract: ' + ', '.join(v['field'] for v in violations))


def _ids(items, field):
    if not isinstance(items, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in items):
        raise PolicyError('DISPOSITION_SOURCE: ' + field + ' requires exact source-owned IDs')
    ids = [x['id'] for x in items]
    if len(ids) != len(set(ids)):
        raise PolicyError('DISPOSITION_SOURCE: duplicate input IDs in ' + field)
    return ids


def sources(payload):
    top = payload.get('sources') if 'sources' in payload else None
    web = payload.get('web', {})
    if not isinstance(web, dict):
        raise PolicyError('DISPOSITION_SOURCE: web must be an object')
    nested = web.get('sources') if 'sources' in web else None
    for present, value, name in [('sources' in payload, top, 'sources'), ('sources' in web, nested, 'web.sources')]:
        if present: _ids(value, name)
    if 'sources' in payload and 'sources' in web:
        if {x['id']: x for x in top} != {x['id']: x for x in nested}:
            raise PolicyError('DISPOSITION_SOURCE: conflicting sources and web.sources ownership')
    return nested if 'sources' in web else top if 'sources' in payload else []


def contract(payload):
    opinions = _ids(payload['review']['opinions'], 'review.opinions')
    return {'opinion_ids': opinions, 'web_refs': [x['id'] for x in sources(payload)], 'instruction': INSTRUCTION}


def _difference(field, expected, returned):
    count = Counter(returned)
    missing = [x for x in expected if x not in count]
    extra = list(dict.fromkeys(x for x in returned if x not in set(expected)))
    duplicates = [x for x in dict.fromkeys(returned) if count[x] > 1]
    if missing or extra or duplicates:
        return {'field': field, 'expected': list(expected), 'returned': list(returned),
                'missing': missing, 'extra': extra, 'duplicates': duplicates,
                'violations': [name for name, items in [('missing', missing), ('extra', extra), ('duplicate', duplicates)] if items]}


def validate(payload, value):
    exact = contract(payload)
    violations = [x for x in [
        _difference('opinion_responses.opinion_id', exact['opinion_ids'], [x.opinion_id for x in value.opinion_responses]),
        _difference('web_refs', exact['web_refs'], value.web_refs)] if x]
    if violations: raise DispositionContractError(violations)


def recurrence(diagnostic, *, schema=None):
    """Finite field/violation identity; retain actionable IDs in the diagnostic."""
    original = diagnostic
    if (diagnostic.get('representation') == 'json'
            and diagnostic.get('diagnostic_truncated') is False
            and diagnostic.get('omitted_chars') == 0
            and isinstance(diagnostic.get('sanitized_response'), str)
            and diagnostic.get('sanitized_full_chars') == len(diagnostic['sanitized_response'])):
        try:observed = json.loads(diagnostic['sanitized_response'])
        except (ValueError, TypeError):observed = None
        if isinstance(observed, dict) and isinstance(observed.get('errors'), list):
            diagnostic = observed
        elif isinstance(observed, list) and all(isinstance(e, dict) for e in observed):
            diagnostic = {'kind': 'schema_validation', 'errors': observed}
    if diagnostic.get('kind') == 'disposition_contract':
        return {'kind': diagnostic['kind'], 'version': diagnostic['version'],
                'violations': sorted((v['field'], tuple(sorted(v['violations']))) for v in diagnostic['violations'])}
    errors = diagnostic.get('errors')
    if schema is not None:
        # Only fully observed structured errors have a structural identity.
        # Truncated/opaque diagnostics keep their exact old identity. Neither
        # model text nor a changing returned selector chooses this key.
        from .semantic_wire import VERSION as wire_version, WIRE_SCHEMAS
        if (schema.__name__ not in WIRE_SCHEMAS
                or diagnostic.get('kind') not in {'semantic_wire', 'schema_validation'}
                or (diagnostic.get('kind') == 'semantic_wire' and diagnostic.get('version') != wire_version)
                or not isinstance(errors, list) or not errors
                or any(not isinstance(e, dict) or not isinstance(e.get('type'), str)
                       or not isinstance(e.get('loc'), (list, tuple))
                       or any(type(x) not in {str, int} for x in e['loc']) for e in errors)):
            return original
        from .store import digest
        return {'kind': 'wire_structural_defect', 'version': wire_version,
                'schema': schema.__name__, 'schema_sha256': digest(WIRE_SCHEMAS[schema.__name__].model_json_schema()),
                'errors': sorted(set((e['type'], tuple('*' if type(x) is int else x for x in e['loc'])) for e in errors))}
    if isinstance(errors, list):
        return {'kind': diagnostic.get('kind'), 'errors': sorted(
            (str(e.get('type')), tuple('*' if isinstance(x, int) else str(x) for x in e.get('loc', []))) for e in errors)}
    return diagnostic
