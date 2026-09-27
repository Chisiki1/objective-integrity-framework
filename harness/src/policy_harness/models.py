from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Any
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, StrictBool


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Decision(StrictModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class Operation(StrictModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal['file_read', 'file_write', 'file_list', 'exec', 'web_fetch', 'capability_request', 'delegate', 'wait_children', 'finish', 'policy_amendment', 'plan_task', 'reconcile', 'child_reconcile', 'knowledge_update', 'history_read', 'child_integrate', 'knowledge_integrate', 'controller_read', 'prepare_update', 'verify_update', 'activate_update', 'rollback_update', 'adopt_policy', 'judgment_resolve', 'knowledge_read', 'attachment_read', 'candidate_disposition', 'knowledge_index', 'source_read', 'child_scope', 'knowledge_projection_repair', 'source_prepare', 'harness_info', 'harness_read', 'harness_propose', 'harness_verify', 'harness_apply', 'harness_rollback']
    args: dict[str, Any] = Field(default_factory=dict)
    purpose: str = Field(min_length=1)
    decisions: list[Decision] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    resource_keys: list[str] = Field(default_factory=list)
    improvement_bindings: list[dict[str,Any]] = Field(default_factory=list)

    @field_validator('decisions')
    @classmethod
    def unique_decisions(cls, values):
        if len({x.id for x in values})!=len(values):raise ValueError('Each finalized decision must have a unique identity')
        return values


class OperationResult(StrictModel):
    operation_id: str
    status: Literal['succeeded', 'failed', 'unknown', 'pending']
    stdout: str = ''
    stderr: str = ''
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str = Field(default_factory=now)
    finished_at: str = Field(default_factory=now)
    elapsed_seconds: float = 0
    exit_code: int | None = None
    effect: Literal['none', 'confirmed', 'unknown'] = 'none'
    controller_runtime: dict[str, Any] = Field(default_factory=dict)


class Idea(StrictModel):
    id: str = Field(min_length=1)
    target: Literal['task', 'workflow']
    proposal: str = Field(min_length=1)
    disposition: Literal['adopt', 'reject', 'investigate']
    rationale: str = Field(min_length=1)
    next_operation: dict[str, Any] | None = None


def optional_improvement_contract():
    """Approved deferral uses existing fields; saved Idea schemas stay exact."""
    return {'idea': "Keep disposition='investigate' and give next_operation={'defer': deferral}. rationale explains why this improvement is optional for the current mandatory outcome.",
        'knowledge_update': "For an existing Idea use {id,disposition:'defer',reason,deferral}; this receives the ordinary operation review, not a new review tier.",
        'deferral': {'required_for_current_completion': False,
            'current_completion_impact': 'Explain why every current mandatory outcome can be achieved without this improvement.',
            'next_trigger': 'Concrete later condition for reconsideration.'},
        'limits': 'A deferral is an explicit semantic judgment subject to the existing independent review. It does not satisfy an acceptance criterion, resolve a mandatory correction, undo adoption or waive verification/use of an executed change. Unclassified Ideas remain open; no automatic optional classification.',
        'skill_updates': 'An empty skill_updates list records the actual episode and lifecycle classification without automatically creating a new auxiliary Skill. Explicit updates and observed applications retain their normal validation and effect recording.'}


def scoped_policy_input(payload):
    """One current input definition for construction and saved-version lookup."""
    value = dict(payload, policy_input_contract='role-scoped-v1')
    if 'skill_update_contract' in value:
        value['skill_update_contract'] = dict(value['skill_update_contract'],
            default=optional_improvement_contract()['skill_updates'])
    return value


def validate_optional_deferral(value, reason):
    """Shape only; the normal independent reviewer decides the claimed impact."""
    if (not isinstance(value, dict)
            or set(value) != {'required_for_current_completion', 'current_completion_impact', 'next_trigger'}
            or value.get('required_for_current_completion') is not False
            or any(not isinstance(value.get(key), str) or not value[key].strip()
                   for key in ('current_completion_impact', 'next_trigger'))
            or not isinstance(reason, str) or not reason.strip()):
        raise PolicyError('Optional deferral needs explicit nondependency, current mandatory-outcome impact, reason and next trigger')
    return dict(value, reason=reason)


def idea_deferral(idea):
    body = idea.model_dump() if isinstance(idea, Idea) else idea
    followup = body.get('next_operation')
    if not isinstance(followup, dict) or 'defer' not in followup:
        return None
    if body.get('disposition') != 'investigate' or set(followup) != {'defer'}:
        raise PolicyError('Deferred consideration must remain investigate with only its deferral metadata; it is not an adopted operation')
    return validate_optional_deferral(followup['defer'], body.get('rationale'))


class Assessment(StrictModel):
    objective_link: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    success: Literal['expected', 'succeeded', 'failed', 'unknown']
    mistakes: list[str]
    recurrence: Literal['yes', 'no', 'unknown']
    efficiency: str = Field(min_length=1)
    interactions: str = Field(min_length=1)
    thinking_targets: dict[str, str]
    ideas: list[Idea] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class TargetAssessment(StrictModel):
    target_id: str
    assessment: Assessment

    @model_validator(mode='before')
    @classmethod
    def equivalent_flat_representation(cls,value):
        # Lossless wire-shape adaptation only. No field is dropped, no default
        # outcome/idea is fabricated, and normal strict nested validation follows.
        if isinstance(value,dict) and 'assessment' not in value and 'target_id' in value:
            if set(value)<={'target_id',*Assessment.model_fields}:
                return {'target_id':value['target_id'],'assessment':{k:v for k,v in value.items() if k!='target_id'}}
        return value


class AssessmentBatch(StrictModel):
    assessments: list[TargetAssessment] = Field(min_length=1)


class Opinion(StrictModel):
    id: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class Review(StrictModel):
    summary: str = Field(min_length=1)
    opinions: list[Opinion]


class OpinionResponse(StrictModel):
    opinion_id: str
    disposition: Literal['accept', 'reject', 'investigate']
    rationale: str = Field(min_length=1)


class Disposition(StrictModel):
    verdict: Literal['proceed', 'revise', 'hold']
    rationale: str = Field(min_length=1)
    opinion_responses: list[OpinionResponse]
    web_refs: list[str]


# Input semantics are versioned separately from the historical response schema.
# A rationale is never interpreted as a patch or executed as an instruction.
DISPOSITION_INSTRUCTION = (
    'Give a reasoned response to every exact opinion and consume every listed acquired source. '
    'proceed means the exact reviewed operative payload remains UNCHANGED and may be applied as stored. '
    'Accepting an opinion that requires changing that payload requires verdict=revise; describe the change '
    'in the response, then the controller obtains a NEW complete proposal and independent review before applying it. '
    'A rationale, qualification or claimed correction cannot edit any Learning, Skill, evidence, argument or result. '
    'Reject or qualify an opinion with reasons when no operative change is needed. '
    'hold keeps unresolved requirements open. Each page binds the same complete original payload; '
    'composition retains every hold/revise. The final opinion response itself does not require recursive review.'
)


_SKILL_UPDATE_FIELDS = {
    **{name: {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
       for name in ('id', 'title', 'content', 'applicability', 'next_trigger', 'reason')},
    'expected_hash': {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'},
    'merge_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1},
                  'minItems': 1, 'uniqueItems': True},
    'merge_hashes': {'type': 'object', 'additionalProperties': {
        'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'}},
    'counterexamples': {'type': 'array', 'items': {}},
}
_SKILL_UPDATE_ACTIONS = {
    'create': (('title', 'content', 'applicability', 'next_trigger'), ()),
    'use': (('id',), ()),
    'improve': (('id', 'expected_hash'), ('content', 'applicability', 'next_trigger', 'counterexamples')),
    'organize': (('id', 'expected_hash'), ('content', 'applicability', 'next_trigger', 'counterexamples')),
    'merge': (('id', 'expected_hash', 'merge_ids', 'merge_hashes', 'content'), ()),
    'retire': (('id', 'expected_hash', 'reason'), ()),
}


def learning_updates_schema():
    """The existing consumer's wire contract, also shown in model JSON schema."""
    return {'type': 'array', 'items': {'oneOf': [
        {'type': 'object', 'additionalProperties': False,
         'properties': {'action': {'const': action, 'type': 'string'},
                        **{name: _SKILL_UPDATE_FIELDS[name] for name in (*required, *optional)}},
         'required': ['action', *required]}
        for action, (required, optional) in _SKILL_UPDATE_ACTIONS.items()]}}


def learning_update_contract():
    return {'schema': 'learning-skill-updates-v1', 'skill_updates': learning_updates_schema(),
        'meaning': {
            'create': 'Creates provisional knowledge with source episode, applicability and next_trigger. It does not activate a method or prove use/benefit. Do not invent an existing id.',
            'use': 'Only records an application already proved in applications against the exact pre-selected Skill/version/procedure and actual result. Selection alone is not use.',
            'improve_organize': 'Existing visible id and exact current expected_hash. Only supplied content/applicability/next_trigger/counterexamples are changed. Shared child changes remain parent-integration candidates.',
            'merge': 'Existing visible target and distinct other merge_ids, their exact merge_hashes, and complete resulting content. All identities/hashes are checked again atomically.',
            'retire': 'Existing visible id, exact expected_hash and substantive reason; history remains.'},
        'default': 'skill_updates=[] is valid. The actual outcome is always recorded. If neither actual applications nor explicit/pending updates produce Skill work, the consumer creates provisional knowledge from outcome_summary, task applicability and next_use_trigger, including unknown recurrence. It remains UNUSED_EFFECT_UNVERIFIED pending cleanup and any required later validation/use.',
        'observations': 'Put observations and provenance in outcome_summary/evidence_refs and reasoned candidates in ideas. They are not additional action names. Use only these exact wire fields; no create-provisional or record-only aliases. The controller never rewrites a rejected action into an accepted one.',
        'validation': 'Shape, visible target, CAS and actual-application evidence are checked before choice review and again by the final atomic consumer. This is not semantic approval; the complete corrected proposal still receives normal assessment and review.'}


def validate_learning_updates(updates):
    """Pure validation: retain old dict-shaped records, never coerce proposals."""
    mutations = set()
    uses = set()
    for index, update in enumerate(updates):
        prefix = f'skill_updates[{index}]'
        if not isinstance(update, dict) or not isinstance(update.get('action'), str):
            raise LearningContractError(prefix + ' requires an exact action string')
        action = update['action']
        if action not in _SKILL_UPDATE_ACTIONS:
            raise LearningContractError(prefix + '.action is unsupported: ' + repr(action) +
                                        '; allowed: ' + ', '.join(_SKILL_UPDATE_ACTIONS))
        required, optional = _SKILL_UPDATE_ACTIONS[action]
        missing = set(required) - update.keys()
        unknown = update.keys() - {'action', *required, *optional}
        if missing or unknown:
            raise LearningContractError(prefix + f' {action}: missing fields {sorted(missing)}; unsupported fields {sorted(unknown)}')
        for name, value in update.items():
            if name == 'action':
                continue
            kind = _SKILL_UPDATE_FIELDS[name]['type']
            if kind == 'string' and (not isinstance(value, str) or not value.strip()):
                raise LearningContractError(prefix + '.' + name + ' must be a nonempty string')
            if name == 'expected_hash' and (len(value) != 64 or any(c not in '0123456789abcdefABCDEF' for c in value)):
                raise LearningContractError(prefix + '.expected_hash must be an exact SHA256')
            if kind == 'array' and not isinstance(value, list):
                raise LearningContractError(prefix + '.' + name + ' must be an array')
            if name == 'merge_hashes' and (not isinstance(value, dict) or any(
                    not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdefABCDEF' for c in v)
                    for v in value.values())):
                raise LearningContractError(prefix + '.merge_hashes must map each source id to an exact SHA256')
        if action == 'merge':
            ids = update['merge_ids']
            if (not ids or any(not isinstance(i, str) or not i.strip() for i in ids)
                    or len(ids) != len(set(ids)) or update['id'] in ids
                    or set(update['merge_hashes']) != set(ids)):
                raise LearningContractError(prefix + '.merge_ids must be distinct other ids with exactly matching merge_hashes')
        # A use declaration cites an independently evidenced application. It is
        # not a second procedure mutation; the atomic consumer records that use
        # before applying the one update against its original-version CAS.
        if action == 'use':
            if update['id'] in uses:
                raise LearningContractError(prefix + ' repeats an exact Skill use declaration')
            uses.add(update['id'])
        elif action != 'create':
            touched = {update['id'], *update.get('merge_ids', [])}
            if mutations & touched:
                raise LearningContractError('Coherent Learning contains interacting merge/write targets')
            mutations.update(touched)


APPLICATION_FIELDS = ('skill_id', 'skill_hash', 'procedure_clause', 'procedure_sha256',
                      'operation_id', 'operation_sha256', 'result_sha256', 'evidence')
APPLICATION_EVIDENCE_FIELDS = ('pointer', 'sha256', 'explanation')
APPLICATION_OUTPUT_ROOTS = ('stdout', 'stderr', 'data', 'artifacts')


def application_evidence_pointer(pointer):
    return isinstance(pointer, str) and (pointer in {'/' + name for name in APPLICATION_OUTPUT_ROOTS}
                                        or pointer.startswith(('/data/', '/artifacts/')))


def application_evidence_value(result, pointer):
    """Resolve a recorded application witness, not proof of use or benefit.

    An explicit empty collection/text records absence at that result's scope.
    Missing values, null and unknown execution do not become observations.
    The caller still authenticates the operation, result, selected procedure,
    hashes and independent semantic judgment before admitting an application.
    """
    if not application_evidence_pointer(pointer):
        raise PolicyError('Application evidence must point to observed output, not an identity/status label')
    if (not isinstance(result, dict) or result.get('status') not in {'succeeded', 'failed'}
            or result.get('effect') == 'unknown'):
        raise PolicyError('Unknown execution cannot establish actual Skill application')
    value = result
    try:
        for token in pointer[1:].split('/'):
            if '~' in token.replace('~1', '').replace('~0', ''):raise ValueError()
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not token.isascii() or not token.isdecimal() or str(int(token)) != token:raise ValueError()
                value = value[int(token)]
            else:value = value[token]
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise PolicyError('Evidence pointer is absent from the actual result') from error
    if value is None:
        raise PolicyError('Application evidence value is unavailable, not an observed absence')
    return value


def learning_applications_schema():
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    sha = {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'}
    evidence = {'type': 'object', 'additionalProperties': False,
        'required': list(APPLICATION_EVIDENCE_FIELDS), 'properties': {
            'pointer': {'type': 'string', 'pattern': r'^/(stdout|stderr|data|artifacts)$|^/(data|artifacts)/'},
            'sha256': sha, 'explanation': text}}
    return {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'required': list(APPLICATION_FIELDS), 'properties': {
            **{name: text for name in ('skill_id', 'procedure_clause', 'operation_id')},
            **{name: sha for name in ('skill_hash', 'procedure_sha256', 'operation_sha256', 'result_sha256')},
            'evidence': {'type': 'array', 'minItems': 1, 'items': evidence}}}}


def learning_applications_contract():
    return {'schema': 'learning-applications-v1', 'applications': learning_applications_schema(),
        'field_sources': 'skill_id/hash/procedure_clause/procedure_sha256 come from the exact pre-execution selection (id becomes skill_id; hash becomes skill_hash in your new proposal). operation_id is the actual operation id; operation_sha256/result_sha256 and evidence pointer hashes bind the supplied original result. Write a substantive explanation for each observed application. No aliases or extra fields.',
        'evidence_scope': 'Only observed output roots /stdout, /stderr, /data, /artifacts and descendants of /data or /artifacts are permitted. Identity/status/timing labels are not application evidence. A permitted pointer or selected Skill alone does not prove use.',
        'unobserved_use': 'Keep applications=[] when actual procedure application cannot be established. Never fabricate explanation, execution, hashes or benefit. Rejected historical dictionaries remain unchanged; only a new complete reviewed proposal can be admitted.'}


APPLICATION_OUTPUT_VERSION = 'learning-application-output-v2'
APPLICATION_OUTPUT_INSTRUCTION = (
    'This response owns only the supplied Application focus. considered_skill_ids must contain exactly '
    'the current selected_skill_ids, once each, including selected Skills with no observed application. '
    'applications contains only evidenced applications of those selected Skills; [] is correct when use '
    'is unobserved. ideas contains every current required_idea_id and only genuinely new candidates '
    'discovered in this focus. Do not repeat previously disposed Ideas or other selected Skills from '
    'bounded_context, retained proposals or rejected responses. Those complete records are evidence '
    'and history, not additional output assignments. Do not output lifecycle updates, classifications '
    'or a whole Learning report here. All Ideas and lifecycle decisions still reach the later complete '
    'composition, choice assessments and independent review before any Knowledge application.')


def learning_application_output_contract(payload, already_owned=()):
    return {'schema': APPLICATION_OUTPUT_VERSION,
        'allowed_output_fields': ['considered_skill_ids', 'applications', 'ideas'],
        'selected_skill_ids': [s['id'] for s in payload.get('pre', {}).get('skills', {}).get('selected', [])],
        'required_idea_ids': [i['id'] for i in payload.get('required_ideas', [])],
        'already_owned_idea_ids': sorted(set(already_owned) - {i['id'] for i in payload.get('required_ideas', [])}),
        'instruction': APPLICATION_OUTPUT_INSTRUCTION}


class Learning(StrictModel):
    outcome_summary: str = Field(min_length=1)
    classifications: list[Literal['create', 'use', 'improve', 'organize', 'merge', 'retire']] = Field(min_length=1)
    # Historical invalid proposals remain parseable for exact failure recovery.
    # Admission uses the same pure contract in Engine and Knowledge, not coercion.
    skill_updates: list[dict[str, Any]] = Field(json_schema_extra=learning_updates_schema())
    recurrence: Literal['yes', 'no', 'unknown']
    next_use_trigger: str = Field(min_length=1)
    ideas: list[Idea]
    evidence_refs: list[str] = Field(default_factory=list)
    applications: list[dict[str,Any]] = Field(default_factory=list, json_schema_extra=learning_applications_schema())


class LearningIdeas(StrictModel):
    """Only dispositions owned by this focus; no repeated global lifecycle work."""
    ideas: list[Idea]


class LearningApplications(StrictModel):
    considered_skill_ids: list[str]
    applications: list[dict[str, Any]] = Field(json_schema_extra=learning_applications_schema())
    ideas: list[Idea]


class LearningSynthesis(StrictModel):
    """One coherent lifecycle proposal, composed with already observed decisions."""
    outcome_summary: str = Field(min_length=1)
    classifications: list[Literal['create', 'use', 'improve', 'organize', 'merge', 'retire']] = Field(min_length=1)
    skill_updates: list[dict[str, Any]] = Field(json_schema_extra=learning_updates_schema())
    recurrence: Literal['yes', 'no', 'unknown']
    next_use_trigger: str = Field(min_length=1)
    evidence_refs: list[str]
    ideas: list[Idea]


LEARNING_SCHEMAS = (Learning, LearningIdeas, LearningApplications, LearningSynthesis)


class AcceptanceResult(StrictModel):
    criterion: str = Field(min_length=1)
    achieved: StrictBool
    evidence_refs: list[str]
    independent_refs: list[str]


class Completion(StrictModel):
    achieved: StrictBool
    acceptance: list[AcceptanceResult]
    evidence_refs: list[str]
    unresolved: list[str]
    summary: str = Field(min_length=1)


class ResearchQuery(StrictModel):
    query: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    private_data_excluded: StrictBool


class PolicyError(RuntimeError):
    pass


class LearningContractError(PolicyError):
    """A rejected, effect-free Learning proposal needs an exact new proposal."""


class ConfigurationRequired(PolicyError):
    pass


class TaskPlan(StrictModel):
    task_kind: Literal['implementation','diagnosis','research','document','maintenance','external','mixed']
    objective: str = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    deliverables: list[str] = Field(min_length=1)
    constraints: list[str]
    preservation: list[str]
    needed_capabilities: list[str]
    missing_capabilities: list[str]
    phases: list[str] = Field(min_length=1)
    source_coverage: list[dict[str, str]] = Field(default_factory=list)
    source_hash: str | None = None
    source_dispositions: list[dict[str,Any]] = Field(default_factory=list)
    acceptance_dispositions: list[dict[str,Any]] = Field(default_factory=list)
    deferred_index_hash: str | None = None
    deferred_considerations: list[dict[str, Any]] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class EngineeringScope(StrictModel):
    """An independent source interpretation, never an execution permit."""
    engineering: StrictBool
    correction: StrictBool
    rationale: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    work_items: list[str] = Field(min_length=1)
    normal_success: list[str] = Field(min_length=1)
    preservation: list[str] = Field(min_length=1)


class HarmScenario(StrictModel):
    id: str = Field(min_length=1)
    normal_path: str = Field(min_length=1)
    boundary: str = Field(min_length=1)
    harm: str = Field(min_length=1)
    interactions: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class EngineeringScenarios(StrictModel):
    scenarios: list[HarmScenario] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class CandidateCheck(StrictModel):
    work_item: str = Field(min_length=1)
    state: Literal['inspected', 'blocked', 'later']
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    next_step: str = Field(min_length=1)


class EngineeringInvestigation(StrictModel):
    status: Literal['ready', 'needs_evidence']
    first_fault: str = Field(min_length=1)
    causal_chain: list[str] = Field(min_length=1)
    alternative_causes: list[str] = Field(min_length=1)
    falsifiable_predictions: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    findings: list[Opinion]
    checks: list[CandidateCheck] = Field(min_length=1)
    missing_evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ReadinessEvidence(StrictModel):
    aspect: Literal['target', 'environment', 'inputs', 'observation', 'comparison', 'effects', 'recovery']
    state: Literal['observed', 'missing']
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class AdmissionDependency(StrictModel):
    """Independent relation to one original check or missing fact."""
    prerequisite_id: str = Field(min_length=1)
    relation: Literal['required', 'independent']
    finding_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class EngineeringAdmission(StrictModel):
    route: Literal['read_only', 'routine', 'construction', 'diagnosis', 'material_correction', 'verification']
    phase: Literal['NONE', 'BUILD', 'SWEEP', 'REPAIR', 'ACCEPT']
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    preservation: list[str] = Field(min_length=1)
    interactions: list[str] = Field(min_length=1)
    addressed_findings: list[str] = Field(default_factory=list)
    dependencies: list[AdmissionDependency] = Field(default_factory=list)
    required_preparation: list[str] = Field(default_factory=list)
    readiness: list[ReadinessEvidence] = Field(default_factory=list)
    opinions: list[Opinion] = Field(default_factory=list)


class SkillSelection(StrictModel):
    selected: list[dict[str, str]]
    rejected: list[dict[str, str]]
    new_knowledge_needed: list[str]
    rationale: str = Field(min_length=1)


class Cleanup(StrictModel):
    decisions: list[dict[str, str]]
    rationale: str = Field(min_length=1)


class IdeaResolution(StrictModel):
    decisions: list[dict]
    rationale: str = Field(min_length=1)
