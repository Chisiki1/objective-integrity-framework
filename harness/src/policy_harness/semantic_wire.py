"""Explicit semantic responses at the existing model boundary.

Canonical records remain unchanged. A frozen request supplies fixed metadata;
the model supplies every judgment, reason and evidence selection. Association
does not prove understanding, use, permission or success.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field, StrictBool, StrictInt, ValidationError

from . import models as m
from .disposition_contracts import contract as disposition_contract
from .store import digest

VERSION = 'semantic-decisions-v1'
LEARNING_BINDINGS = 'learning-bindings-v1'
LEARNING_TARGETS = 'learning-targets-v1'
APPLIED_TARGETS = 'applied-knowledge-targets-v1'
ADMISSION_REFERENCES = 'admission-references-v1'
CONTROLLER_CONTEXT = 'controller-context-v1'
LEARNING_EVIDENCE = 'learning-evidence-v1'
APPLICATION_CONTRACT = 'application-contract-v1'
REFERENCE_TREE = 'reference-tree-v1'
INSTALLED_CONTEXT = 'installed-controller-context-v1'
ASSESSMENT_DEFAULTS = 'assessment-defaults-v1'
REFERENCED_ASSESSMENT_DEFAULTS = 'referenced-assessment-defaults-v1'
ASSESSMENT_EVIDENCE = 'assessment-evidence-v1'
ASSESSMENT_COMPACT = 'assessment-compact-v2'
ASSESSMENT_TARGET = 'assessment-target-v1'
ASSESSMENT_TARGET_CORRECTION = 'assessment-target-v2'
ASSESSMENT_TARGET_RANGES = 'assessment-target-v3'
ASSESSMENT_REFERENCE_SLOTS = 'assessment-reference-slots-v4'


class NewIdea(m.StrictModel):
    target: Literal['task', 'workflow']
    proposal: str = Field(min_length=1)
    disposition: Literal['adopt', 'reject', 'investigate']
    rationale: str = Field(min_length=1)
    next_operation: dict[str, Any] | None


class IdeaDecision(m.StrictModel):
    disposition: Literal['adopt', 'reject', 'investigate']
    rationale: str = Field(min_length=1)
    next_operation: dict[str, Any] | None


def idea_revision_contract():
    """Describe the existing decoder's edit surface without changing captures."""
    mutable = list(IdeaDecision.model_fields)
    return {'version': 'idea-reference-edit-contract-v1',
        'fixed_existing_fields': [key for key in m.Idea.model_fields if key not in mutable],
        'mutable_existing_fields': mutable,
        'changed_proposal_route': {'existing_idea': 'reject with a specific rationale',
            'changed_idea': 'new_ideas', 'new_fields': list(NewIdea.model_fields)},
        'meaning': 'An editable Learning source slot permits a new disposition, rationale and next_operation, '
            'not rewriting an existing idea id, proposal or target. If the proposal itself is wrong, retain '
            'and reject it with reasons and return the changed proposal through new_ideas. Code allocates '
            'the new identity. The original proposal and every opinion remain evidence; rejecting a '
            'proposal does not waive a standing policy. Review the actual correction, not an impossible '
            'in-place rewrite of the old proposal.'}


class AssessmentDecision(m.StrictModel):
    objective_link: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    success: Literal['expected', 'succeeded', 'failed', 'unknown']
    mistakes: list[str]
    recurrence: Literal['yes', 'no', 'unknown']
    efficiency: str = Field(min_length=1)
    interactions: str = Field(min_length=1)
    thinking_values: list[str]
    ideas: list[NewIdea] = Field(min_length=1)
    evidence_refs: list[str]


class AssessmentDecisions(m.StrictModel):
    assessments: list[AssessmentDecision] = Field(min_length=1)


class ExistingCandidate(m.StrictModel):
    candidate_slot: StrictInt = Field(ge=0)
    consideration: str = Field(min_length=1)


class ReferencedAssessment(AssessmentDecision):
    ideas: list[NewIdea | ExistingCandidate] = Field(min_length=1)


class ReferencedAssessments(m.StrictModel):
    assessments: list[ReferencedAssessment] = Field(min_length=1)


class DefaultAssessment(AssessmentDecision):
    # Derive the optional bookkeeping default from the canonical definition.
    # Historical classes above are immutable captured schema definitions.
    evidence_refs: list[str] = Field(default_factory=m.Assessment.model_fields['evidence_refs'].default_factory)


class DefaultAssessments(m.StrictModel):
    assessments: list[DefaultAssessment] = Field(min_length=1)


class DefaultReferencedAssessment(DefaultAssessment):
    ideas: list[NewIdea | ExistingCandidate] = Field(min_length=1)


class DefaultReferencedAssessments(m.StrictModel):
    assessments: list[DefaultReferencedAssessment] = Field(min_length=1)


class SourceCitation(m.StrictModel):
    source_id: str
    source_sha256: str
    start_byte: StrictInt = Field(ge=0)
    end_byte: StrictInt = Field(gt=0)


class EvidenceAssessment(DefaultReferencedAssessment):
    source_citations: list[SourceCitation]
    requested_source_ranges: list[SourceCitation]


class EvidenceAssessments(m.StrictModel):
    assessments: list[EvidenceAssessment] = Field(min_length=1)


class SlotAssessment(m.StrictModel):
    objective_link: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    success: Literal['expected', 'succeeded', 'failed', 'unknown']
    mistakes: list[str]
    recurrence: Literal['yes', 'no', 'unknown']
    efficiency: str = Field(min_length=1)
    interactions: str = Field(min_length=1)
    thinking_values: list[str]
    existing_idea_decisions: list[ExistingCandidate]
    new_ideas: list[NewIdea]
    evidence_refs: list[str] = Field(default_factory=m.Assessment.model_fields['evidence_refs'].default_factory)
    source_citation_slots: list[StrictInt]
    requested_source_slots: list[StrictInt]


class SlotAssessments(m.StrictModel):
    assessments: list[SlotAssessment] = Field(min_length=1)


class OpinionDecision(m.StrictModel):
    disposition: Literal['accept', 'reject', 'investigate']
    rationale: str = Field(min_length=1)


class DispositionDecision(m.StrictModel):
    verdict: Literal['proceed', 'revise', 'hold']
    rationale: str = Field(min_length=1)
    opinion_decisions: list[OpinionDecision]


class LearningDispositionDecision(DispositionDecision):
    revision_scope: Literal['none', 'learning', 'governed']


def wire_schema(name, payload, *, revision=None):
    from .learning_projection import active
    if revision is not None:
        if revision == ASSESSMENT_REFERENCE_SLOTS:
            if name == 'Assessment': return SlotAssessment
            if name == 'AssessmentBatch': return SlotAssessments
            _proof('assessment reference slots belong to a batch response')
        if revision in {ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES}:
            if name == 'Assessment': return EvidenceAssessment
            if name == 'AssessmentBatch': return EvidenceAssessments
            _proof('assessment evidence revision belongs to another response family')
        if revision in {ASSESSMENT_DEFAULTS, REFERENCED_ASSESSMENT_DEFAULTS}:
            referenced = revision == REFERENCED_ASSESSMENT_DEFAULTS
            if name == 'Assessment': return DefaultReferencedAssessment if referenced else DefaultAssessment
            if name == 'AssessmentBatch': return DefaultReferencedAssessments if referenced else DefaultAssessments
            _proof('semantic wire revision belongs to another response family')
        if revision == ADMISSION_REFERENCES:
            if name in ADMISSION_WIRE_SCHEMAS: return ADMISSION_WIRE_SCHEMAS[name]
            _proof('semantic wire revision belongs to another response family')
        if revision == LEARNING_TARGETS:
            if name == 'Review': return SelectedReview
            if name == 'Disposition': return SelectedLearningDisposition
            _proof('semantic wire revision belongs to another response family')
        if revision == APPLIED_TARGETS:
            if name == 'Review': return SelectedReview
            if name == 'Disposition': return SelectedAppliedDisposition
            _proof('semantic wire revision belongs to another response family')
        if revision != LEARNING_BINDINGS: _proof('unknown semantic wire revision')
        if name == 'Assessment': return ReferencedAssessment
        if name == 'AssessmentBatch': return ReferencedAssessments
        if name == 'Review': return GroundedReview
        if name == 'Disposition': return GroundedLearningDisposition
        _proof('semantic wire revision belongs to another response family')
    if name=='Disposition' and active(payload) and payload.get('learning_correction_scope'):
        return LearningDispositionDecision
    return WIRE_SCHEMAS[name]


class NewOpinion(m.StrictModel):
    observation: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str]


class ReviewDecision(m.StrictModel):
    summary: str = Field(min_length=1)
    opinions: list[NewOpinion]


class SourceReference(m.StrictModel):
    pointer: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class GroundedOpinion(NewOpinion):
    evidence_refs: list[SourceReference] = Field(min_length=1)


class GroundedReview(ReviewDecision):
    opinions: list[GroundedOpinion]


class GroundedLearningDisposition(LearningDispositionDecision):
    revision_targets: list[SourceReference]


class SourceSlot(m.StrictModel):
    source_slot: StrictInt = Field(ge=0)


class SelectedOpinion(NewOpinion):
    evidence_refs: list[SourceSlot] = Field(min_length=1)


class SelectedReview(ReviewDecision):
    opinions: list[SelectedOpinion]


class SelectedLearningDisposition(LearningDispositionDecision):
    revision_targets: list[SourceSlot]


class SelectedAppliedDisposition(DispositionDecision):
    # Every source is already committed or captured. A revise uses the existing
    # governed correction route; there is no editable Learning to regenerate.
    revision_targets: list[SourceSlot]


class SkillDecision(m.StrictModel):
    choice: Literal['select', 'reject']
    reason: str = Field(min_length=1)
    application: str | None
    procedure_clause: str | None


class SkillDecisions(m.StrictModel):
    decisions: list[SkillDecision]
    new_knowledge_needed: list[str]
    rationale: str = Field(min_length=1)


class CleanupDecision(m.StrictModel):
    disposition: Literal['retain', 'retire']
    reason: str = Field(min_length=1)


class CleanupDecisions(m.StrictModel):
    decisions: list[CleanupDecision]
    rationale: str = Field(min_length=1)


class RequiredIdeas(m.StrictModel):
    existing_idea_decisions: list[IdeaDecision]
    new_ideas: list[NewIdea]


class EvidenceChoice(m.StrictModel):
    pointer: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ApplicationChoice(m.StrictModel):
    skill_slot: StrictInt = Field(ge=0)
    evidence: list[EvidenceChoice] = Field(min_length=1)


class ApplicationsDecision(RequiredIdeas):
    applications: list[ApplicationChoice]


def skill_updates_schema():
    schema = deepcopy(m.learning_updates_schema())
    for choice in schema['items']['oneOf']:
        for field in ('expected_hash', 'merge_hashes'):
            choice['properties'].pop(field, None)
            if field in choice['required']:choice['required'].remove(field)
    return schema


class LearningDecision(RequiredIdeas):
    outcome_summary: str = Field(min_length=1)
    classifications: list[Literal['create', 'use', 'improve', 'organize', 'merge', 'retire']] = Field(min_length=1)
    skill_updates: list[dict[str, Any]] = Field(json_schema_extra=skill_updates_schema())
    recurrence: Literal['yes', 'no', 'unknown']
    next_use_trigger: str = Field(min_length=1)
    evidence_refs: list[str]
    applications: list[ApplicationChoice]


class SynthesisDecision(RequiredIdeas):
    outcome_summary: str = Field(min_length=1)
    classifications: list[Literal['create', 'use', 'improve', 'organize', 'merge', 'retire']] = Field(min_length=1)
    skill_updates: list[dict[str, Any]] = Field(json_schema_extra=skill_updates_schema())
    recurrence: Literal['yes', 'no', 'unknown']
    next_use_trigger: str = Field(min_length=1)
    evidence_refs: list[str]


class CriterionDecision(m.StrictModel):
    achieved: StrictBool
    evidence_refs: list[str]


class CompletionDecision(m.StrictModel):
    achieved: StrictBool
    acceptance: list[CriterionDecision]
    evidence_refs: list[str]
    unresolved: list[str]
    summary: str = Field(min_length=1)


class SourceDecision(m.StrictModel):
    classification: Literal['add', 'clarify', 'correct', 'replace', 'withdraw']
    reason: str = Field(min_length=1)


class PriorAcceptanceDecision(m.StrictModel):
    disposition: Literal['retain', 'replace', 'withdraw']
    criterion: str | None
    source_ids: list[str]
    source_quote: str
    reason: str = Field(min_length=1)


class AddedAcceptance(m.StrictModel):
    criterion: str = Field(min_length=1)
    source_ids: list[str]
    source_quote: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DeferredDecision(m.StrictModel):
    disposition: Literal['include', 'exclude']
    reason: str = Field(min_length=1)
    planned_application: str | None


class PlanDecision(m.StrictModel):
    task_kind: Literal['implementation', 'diagnosis', 'research', 'document', 'maintenance', 'external', 'mixed']
    objective: str = Field(min_length=1)
    deliverables: list[str] = Field(min_length=1)
    constraints: list[str]
    preservation: list[str]
    needed_capabilities: list[str]
    missing_capabilities: list[str]
    phases: list[str] = Field(min_length=1)
    source_decisions: list[SourceDecision]
    prior_acceptance_decisions: list[PriorAcceptanceDecision]
    added_acceptance: list[AddedAcceptance]
    deferred_decisions: list[DeferredDecision]
    rationale: str = Field(min_length=1)


class ContextDecision(m.StrictModel):
    summary: str = Field(min_length=1)
    limitations: list[str]


class InvestigationCheck(m.StrictModel):
    state: Literal['inspected', 'blocked', 'later']
    evidence_refs: list[str]
    reason: str = Field(min_length=1)
    next_step: str = Field(min_length=1)


class InvestigationDecision(m.StrictModel):
    status: Literal['ready', 'needs_evidence']
    first_fault: str = Field(min_length=1)
    causal_chain: list[str] = Field(min_length=1)
    alternative_causes: list[str] = Field(min_length=1)
    falsifiable_predictions: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    findings: list[NewOpinion]
    checks: list[InvestigationCheck] = Field(min_length=1)
    missing_evidence: list[str]
    limitations: list[str]


class NewScenario(m.StrictModel):
    normal_path: str = Field(min_length=1)
    boundary: str = Field(min_length=1)
    harm: str = Field(min_length=1)
    interactions: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class ScenarioDecisions(m.StrictModel):
    scenarios: list[NewScenario] = Field(min_length=1)
    limitations: list[str]


class AdmissionDecision(m.StrictModel):
    route: Literal['read_only', 'routine', 'construction', 'diagnosis', 'material_correction', 'verification']
    phase: Literal['NONE', 'BUILD', 'SWEEP', 'REPAIR', 'ACCEPT']
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    preservation: list[str] = Field(min_length=1)
    interactions: list[str] = Field(min_length=1)
    addressed_findings: list[str]
    dependencies: list[m.AdmissionDependency]
    required_preparation: list[str]
    readiness: list[m.ReadinessEvidence]
    opinions: list[NewOpinion]


class AdmissionReference(m.StrictModel):
    reference_slot: StrictInt = Field(ge=0)


class ReferencedScope(m.EngineeringScope):
    source_refs: list[AdmissionReference] = Field(min_length=1)


class ReferencedOpinion(NewOpinion):
    evidence_refs: list[AdmissionReference]


class ReferencedInvestigationCheck(InvestigationCheck):
    evidence_refs: list[AdmissionReference]


class ReferencedInvestigation(InvestigationDecision):
    evidence_refs: list[AdmissionReference] = Field(min_length=1)
    findings: list[ReferencedOpinion]
    checks: list[ReferencedInvestigationCheck] = Field(min_length=1)


class ReferencedScenario(NewScenario):
    evidence_refs: list[AdmissionReference] = Field(min_length=1)


class ReferencedScenarios(ScenarioDecisions):
    scenarios: list[ReferencedScenario] = Field(min_length=1)


class ReferencedReadiness(m.ReadinessEvidence):
    evidence_refs: list[AdmissionReference] = Field(default_factory=list)


class ReferencedDependency(m.AdmissionDependency):
    prerequisite_id: AdmissionReference
    finding_ids: list[AdmissionReference] = Field(default_factory=list)
    evidence_refs: list[AdmissionReference] = Field(min_length=1)


class ReferencedAdmission(AdmissionDecision):
    evidence_refs: list[AdmissionReference] = Field(min_length=1)
    addressed_findings: list[AdmissionReference]
    dependencies: list[ReferencedDependency]
    readiness: list[ReferencedReadiness]
    opinions: list[ReferencedOpinion]


ADMISSION_WIRE_SCHEMAS = {
    'EngineeringScope': ReferencedScope, 'EngineeringScenarios': ReferencedScenarios,
    'EngineeringInvestigation': ReferencedInvestigation, 'EngineeringAdmission': ReferencedAdmission,
}


class NewDecision(m.StrictModel):
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str]


class OperationDecision(m.StrictModel):
    kind: m.Operation.model_fields['kind'].annotation
    args: dict[str, Any]
    purpose: str = Field(min_length=1)
    decisions: list[NewDecision] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    resource_keys: list[str]
    improvement_bindings: list[dict[str, Any]]


WIRE_SCHEMAS = {
    'Disposition': DispositionDecision, 'Assessment': AssessmentDecision, 'AssessmentBatch': AssessmentDecisions,
    'SkillSelection': SkillDecisions, 'Cleanup': CleanupDecisions, 'TaskPlan': PlanDecision,
    'Learning': LearningDecision, 'LearningIdeas': RequiredIdeas, 'LearningApplications': ApplicationsDecision,
    'LearningSynthesis': SynthesisDecision, 'Completion': CompletionDecision, 'ContextObservation': ContextDecision,
    'EngineeringScope': m.EngineeringScope,
    'EngineeringInvestigation': InvestigationDecision, 'EngineeringScenarios': ScenarioDecisions,
    'EngineeringAdmission': AdmissionDecision, 'Operation': OperationDecision, 'Review': ReviewDecision,
}


class WireShapeError(ValueError):
    def __init__(self, field, expected, returned, kind='slot_count'):
        self.diagnostic = {'kind': 'semantic_wire', 'version': VERSION,
                           'errors': [{'type': kind, 'loc': [field], 'expected': expected, 'returned': returned}]}
        super().__init__(f'{field}: {kind}; expected {expected!r}, returned {returned!r}')


class WireCardinalityError(m.PolicyError):
    """An authenticated failed output may reach its existing partition owner."""

    def __init__(self, evidence, call_ref):
        super().__init__('Authenticated outer wire count requires a strictly smaller decision focus')
        self.evidence = deepcopy(evidence)
        self.bounded_call_ref = deepcopy(call_ref)


class WirePartialAssessmentError(m.PolicyError):
    """A rejected response has authenticated rows, not a successful batch."""

    def __init__(self, evidence, call_ref=None):
        super().__init__('Authenticated assessment rows remain; correct only unresolved rows')
        self.evidence = deepcopy(evidence)
        self.bounded_call_ref = deepcopy(call_ref)


def outer_cardinality(record, schema, raw):
    """Classify complete typed content, never a diagnostic string or guessed slot."""
    name = schema.__name__
    field = ('assessments' if name == 'AssessmentBatch' else
             'existing_idea_decisions' if name in {'Learning', 'LearningIdeas'} else None)
    if field is None:
        return None
    fixed = record['association']
    if name == 'AssessmentBatch':
        members = [[identity] for identity in fixed['target_ids']]
        expected = len(members)
        # A rejected row is never admitted here. The outer count can still be
        # used to divide a later request, even if a field in that rejected row
        # is invalid. The saved decoder diagnostic is checked by the caller;
        # semantic contents here never become an accepted judgment.
        if (not isinstance(raw, dict) or not isinstance(raw.get('assessments'), list)
                or len(raw['assessments']) == expected or len(members) < 2):
            return None
        rows = raw['assessments']
        return {'field': field, 'expected': expected, 'returned': len(rows),
                'logical_count': len(members), 'members': members, 'raw_sha256': digest(raw)}
    else:
        # Learning's cardinality remains typed under its original schema.
        value = wire_schema(name, record['canonical_input'], revision=record.get('wire_revision')).model_validate(raw, strict=True).model_dump()
        from .learning_projection import groups
        members = [g['members'] for g in groups(record['canonical_input'].get('required_ideas', []))]
        expected = len(fixed.get('required_idea_groups', fixed['required_ideas']))
    if len(value[field]) == expected or len(members) < 2:
        return None
    return {'field': field, 'expected': expected, 'returned': len(value[field]),
            'logical_count': len(members), 'members': members, 'raw_sha256': digest(raw)}


def _count(items, expected, field):
    if len(items) != len(expected): raise WireShapeError(field, len(expected), len(items))


def associations(payload, name, store=None, *, revision=None, presentation_revision=None, binding=None):
    """Only explicit known metadata; no inferred evidence or semantic defaults."""
    fixed = {}
    if name == 'Disposition':
        exact = disposition_contract(payload)
        fixed = {'opinion_ids': exact['opinion_ids'], 'web_refs': exact['web_refs']}
    elif name in {'Assessment', 'AssessmentBatch'}:
        fixed = {'thinking_targets': payload['required_thinking_targets'],
                 'target_ids': [x['id'] for x in payload.get('targets', [])]}
    elif name in {'SkillSelection', 'Cleanup'}:
        fixed = {'skills': deepcopy(payload['knowledge']['skills'])}
    elif name == 'TaskPlan':
        source = payload['source_context']; deferred = payload.get('deferred_for_planning', {})
        fixed = {'source_hash': source['source_hash'], 'pending_ids': source['pending_ids'],
                 'prior_acceptance': source['prior_acceptance'], 'deferred_index_hash': deferred.get('index_hash'),
                 'deferred': [{k: c[k] for k in ('idea_id', 'source_hash')} for c in deferred.get('candidates', [])]}
    elif name == 'Operation' and payload.get('policy_input_contract')=='role-scoped-v1':
        corrections=payload.get('web_correction_context',{}).get('correction_inputs',[])
        if corrections:
            fixed['judgment_corrections']=[{
                'review_id':row['review_id'],'expected_hash':row['expected_hash'],
                'evidence_operation_ids':[item['operation_id'] for item in row['eligible_evidence']]}
                for row in corrections]
    elif name in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
        fixed = {'required_ideas': [{k: i[k] for k in ('id', 'proposal', 'target')} for i in payload.get('required_ideas', [])],
                 'selected_skills': deepcopy(payload.get('pre', {}).get('skills', {}).get('selected', []))}
        source = payload
        if store is not None and payload.get('source_packet_id'):
            packet = store.record_get('bounded_input', payload['source_packet_id'])
            if not packet or packet['id'] != digest({k:v for k,v in packet.items() if k!='id'}):_proof('Skill version packet differs')
            source = packet['value']
        knowledge = source.get('knowledge', {})
        versions = {}
        for skill in [*knowledge.get('skills', []), *knowledge.get('indices', {}).get('skill', {}).get('items', []),
                      *source.get('pre', {}).get('skills', {}).get('selected', [])]:
            if not skill.get('id') or not skill.get('hash'):continue
            if skill['id'] in versions and versions[skill['id']] != skill['hash']:_proof('captured Skill versions conflict')
            versions[skill['id']] = skill['hash']
        fixed['skill_versions'] = versions
        if presentation_revision == APPLICATION_CONTRACT:
            original = _application_source(payload, store, binding)
            fixed['application_context'] = _application_context(payload, original)
    elif name == 'Completion':
        fixed = {'acceptance': deepcopy(payload['acceptance']), 'independent_id': payload['independent_refutation']['id']}
    elif name == 'ContextObservation':
        fixed = {'coverage_ids': [r['id'] for r in payload['source_records']]}
    elif name == 'EngineeringInvestigation':
        fixed = {'work_items': deepcopy(payload.get('work_items', payload.get('admission_contract', {}).get('work_items', [])))}
    if revision == ADMISSION_REFERENCES:
        from .policy_admission import admission_anchors
        anchors = admission_anchors(payload)
        fixed['admission_references'] = [
            {'kind': kind, 'id': identity}
            for kind, field in (('evidence', 'evidence_ids'), ('finding', 'finding_ids'), ('prerequisite', 'prerequisite_ids'))
            for identity in anchors[field]]
        if name == 'EngineeringInvestigation': fixed['work_items'] = anchors['work_items']
    from .learning_projection import active, groups, candidates
    if active(payload):
        if name in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
            fixed['required_idea_groups'] = groups(payload.get('required_ideas', []))
        if name in {'Assessment', 'AssessmentBatch'}:
            source=payload
            ref=payload.get('learning_candidate_source')
            if ref:
                packet=store.record_get('bounded_input',ref.get('id')) if store is not None else None
                if (not packet or digest(packet)!=ref.get('sha256')
                        or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                        or packet['value'].get('operation')!=payload.get('operation')
                        or packet['value'].get('learning_correction_scope')!=payload.get('learning_correction_scope')
                        or not active(packet['value'])):_proof('Assessment candidate source differs')
                source=packet['value']
            fixed['known_ideas'] = candidates(source)
    if revision in {LEARNING_BINDINGS, LEARNING_TARGETS, APPLIED_TARGETS} and name in {'Review', 'Disposition'}:
        source = _reference_source(payload, store, applied=revision == APPLIED_TARGETS)
        if revision == APPLIED_TARGETS and not _applied_review(source): _proof('applied reference source differs')
        fixed['reference_source_sha256'] = digest(source)
        fixed['reference_roots'] = _reference_roots(source, applied=revision == APPLIED_TARGETS)
        if revision in {LEARNING_TARGETS, APPLIED_TARGETS}:
            fixed['source_slots'] = _source_slots(source, fixed['reference_roots'], fields=revision == APPLIED_TARGETS)
    identities = []
    for field in ('opinion_ids', 'web_refs', 'thinking_targets', 'target_ids', 'pending_ids', 'coverage_ids', 'work_items'):
        if field in fixed:identities.append((field, fixed[field]))
    for field in ('skills', 'required_ideas', 'selected_skills'):
        if field in fixed:identities.append((field, [i['id'] for i in fixed[field]]))
    for field, values in identities:
        if any(not isinstance(i,str) or not i for i in values) or len(values)!=len(set(values)):
            _proof('fixed input membership is missing or ambiguous: ' + field)
    return deepcopy(fixed)


def _proof(reason):
    raise m.PolicyError('WIRE_PROVENANCE: ' + reason)


def _applied_review(payload):
    """Recognize only the existing code-owned applied consumer's bundle."""
    while isinstance(payload, dict):
        if (payload.get('contract') in {'applied-knowledge-review-v2', 'applied-knowledge-review-v3'}
                and 'actual_result' in payload and 'accepted_input' in payload):
            return True
        payload = payload.get('bundle')
    return False


def _reference_source(payload, store, *, applied=False):
    """Reuse the existing bounded input, not a second evidence store."""
    from .learning_projection import active
    ref = payload.get('learning_reference_source')
    identity = ref.get('id') if isinstance(ref, dict) else payload.get('source_packet_id')
    source = payload
    context = payload.get('bounded_context')
    contract = payload.get('operative_disposition_contract')
    synopsis = None
    if not identity and (active(payload) or applied) and contract and context:
        if context.get('operative_disposition_contract'):
            source = context
        elif context.get('packet_id'):
            identity = context['packet_id']; synopsis = context
        else:
            if applied and not active(payload): return payload
            _proof('Learning disposition context has no original source')
    if identity:
        packet = store.record_get('bounded_input', identity) if store is not None else None
        source = packet.get('value', {}) if packet else {}
        # Ordinary bounded reviews already have their own source authentication.
        # In particular, final review supplies its operation outside the source.
        if not active(source) and not active(payload) and not (applied and _applied_review(source)): return payload
        source_hash = payload.get('source_context', {}).get('source_hash')
        if (not packet or packet.get('id') != identity
                or packet['id'] != digest({k:v for k,v in packet.items() if k != 'id'})
                or (ref is not None and digest(packet) != ref.get('sha256'))
                or packet.get('task_id') != payload.get('task_id')
                or packet.get('policy_hash') != payload.get('policy_hash')
                or (source_hash is not None and packet.get('source_hash') != source_hash)
                or (synopsis is not None and digest(source) != synopsis.get('source_sha256'))):
            _proof('Learning reference source differs')
    if source is payload: return payload
    if applied and not active(source) and not _applied_review(source): return payload
    if (not active(source) and not (applied and _applied_review(source))) or source.get('operation') != payload.get('operation'):
        _proof('Learning reference source differs')
    if contract:
        originals = [{k:v for k,v in source.items() if k != 'operative_disposition_contract'}]
        if 'bundle' in source: originals.append(source['bundle'])
        if (source.get('operative_disposition_contract') != contract
                or digest(source.get('operation')) != contract.get('operation_sha256')
                or digest(source.get('review')) != contract.get('review_sha256')
                or not any(digest(original) == contract.get('payload_sha256') for original in originals)):
            _proof('Learning disposition original contract differs')
    return source


def _reference_roots(source, *, applied=False):
    roots = []
    def root(pointer, stage, **metadata):
        roots.append({'pointer': pointer, 'editable': False, 'stage': stage, **metadata})
    def add(container, prefix=''):
        for key in sorted(container):
            pointer = prefix + '/' + key.replace('~', '~0').replace('/', '~1')
            if key == 'bundle' and isinstance(container[key], dict):
                add(container[key], pointer)
            elif applied and key == 'accepted_input' and isinstance(container[key], dict) and container[key]:
                for field in sorted(container[key]):
                    stage = ('parent_operation_requirements' if field == 'parent_operation' else
                             'accepted_before_snapshot' if field == 'pre' else 'immutable_accepted_input')
                    root(pointer + '/' + field.replace('~', '~0').replace('/', '~1'), stage)
            elif applied and key == 'assessments' and isinstance(container[key], list) and container[key]:
                choices = container.get('choices', [])
                by_id = {choice['id']: index for index, choice in enumerate(choices)}
                if len(by_id) != len(choices): _proof('applied assessment choices are ambiguous')
                seen = set()
                for index, row in enumerate(container[key]):
                    target = row.get('target_id')
                    if target not in by_id or target in seen: _proof('applied assessment has no unique current target')
                    seen.add(target)
                    root(pointer + '/' + str(index), 'current_after_assessment', target_id=target,
                         target_pointer=prefix + '/choices/' + str(by_id[target]))
                if seen != set(by_id): _proof('applied assessment coverage differs from its current targets')
            elif applied and key == 'actual_result' and isinstance(container[key], dict) and container[key]:
                for field, value in sorted(container[key].items()):
                    path = pointer + '/' + field.replace('~', '~0').replace('/', '~1')
                    if field == 'skill_transitions' and isinstance(value, dict) and value.get('entries'):
                        for name in sorted(value):
                            if name != 'entries': root(path + '/' + name, 'committed_knowledge_outcome')
                            else:
                                for index, transition in enumerate(value[name]):
                                    for part in sorted(transition):
                                        stage = ('committed_transition_' + part if part in {'before', 'after'}
                                                 else 'committed_knowledge_outcome')
                                        root(path + '/entries/' + str(index) + '/' + part, stage,
                                             skill_id=transition['skill_id'])
                    else: root(path, 'committed_knowledge_outcome')
            elif applied:
                root(pointer, {'operation': 'current_operation', 'choices': 'current_after_assessment_targets',
                    'original_operation_result': 'original_operation_result', 'history': 'immutable_history',
                    'controller_facts': 'installed_controller_facts'}.get(key, 'immutable_captured_context'))
            else:
                editable = key == 'learning' and prefix in ('', '/bundle')
                stage = ('current_uncommitted_learning' if editable else
                         'before_snapshot' if key == 'pre' else
                         'before_extraction_snapshot' if key == 'collection_context' else
                         'captured_result' if key == 'result' else 'immutable_captured_context')
                roots.append({'pointer': pointer, 'editable': editable, 'stage': stage})
    add(source)
    return roots


def _source_reference(record, store, reference):
    source = _reference_source(record['canonical_input'], store, applied=record.get('wire_revision') == APPLIED_TARGETS)
    if digest(source) != record['association']['reference_source_sha256']:
        _proof('Learning reference source changed')
    if record.get('wire_revision') in {LEARNING_TARGETS, APPLIED_TARGETS}:
        slot = reference['source_slot']; slots = record['association']['source_slots']
        if slot >= len(slots):
            raise WireShapeError('source_slot', list(range(len(slots))), slot, 'foreign_selector')
        return dict(slots[slot], source_slot=slot, capture_id=record['id'], source_sha256=digest(source))
    pointer = reference['pointer']; value = source
    try:
        if not pointer.startswith('/'): raise ValueError()
        for token in pointer[1:].split('/'):
            # RFC6901 escapes and array membership are strict, including -1.
            if '~' in token.replace('~1', '').replace('~0', ''): raise ValueError()
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not token.isascii() or not token.isdecimal() or str(int(token)) != token: raise ValueError()
                value = value[int(token)]
            else: value = value[token]
    except (ValueError, KeyError, IndexError, TypeError):
        raise WireShapeError('source_reference.pointer', 'existing captured JSON pointer', pointer, 'foreign_selector') from None
    # A claim points to its precise value, not a matching sentence somewhere
    # inside an entire prompt or a different stage's nested history.
    if isinstance(value, (dict, list)) and value:
        raise WireShapeError('source_reference.pointer', 'scalar or empty collection', pointer, 'imprecise_reference')
    text = value if isinstance(value, str) and value else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if not reference['quote'].strip() or reference['quote'] not in text:
        raise WireShapeError('source_reference.quote', 'exact text at the selected pointer', reference['quote'], 'foreign_quote')
    root = next((r for r in record['association']['reference_roots']
                 if pointer == r['pointer'] or pointer.startswith(r['pointer'] + '/')), None)
    if root is None: _proof('Learning reference has no captured owner')
    return dict(reference, capture_id=record['id'], source_sha256=digest(source),
                value_sha256=digest(value), editable=root['editable'], stage=root['stage'])


def _source_slots(source, roots, *, fields=False):
    """Keep original records/fields as choices without copying their bodies."""
    slots = []
    def walk(value, pointer, root):
        if isinstance(value, dict) and value and (not isinstance(value.get('id'), str)
                or (fields and root['stage'] != 'current_operation')):
            for key in sorted(value):
                walk(value[key], pointer + '/' + key.replace('~', '~0').replace('/', '~1'), root)
        elif isinstance(value, list) and value:
            for index, item in enumerate(value): walk(item, pointer + '/' + str(index), root)
        else:
            slots.append(dict(root, pointer=pointer, value_sha256=digest(value)))
    for root in roots:
        value = source
        for token in root['pointer'][1:].split('/'):
            token = token.replace('~1', '/').replace('~0', '~')
            value = value[int(token)] if isinstance(value, list) else value[token]
        walk(value, root['pointer'], root)
    return slots


def _reference_tree(association):
    """Factor only canonical pointer prefixes; leaves remain global slot IDs."""
    roots = []
    for root in association['reference_roots']:
        tree = None
        prefix = root['pointer']
        for index, slot in enumerate(association['source_slots']):
            pointer = slot['pointer']
            if pointer != prefix and not pointer.startswith(prefix + '/'):
                continue
            if any(slot.get(key) != value for key, value in root.items() if key != 'pointer'):
                _proof('reference root metadata differs from its canonical slot')
            if pointer == prefix:
                if tree is not None: _proof('reference root is both a leaf and a container')
                tree = index
                continue
            if tree is None: tree = {}
            if not isinstance(tree, dict): _proof('reference root has conflicting leaves')
            node = tree
            tokens = pointer[len(prefix) + 1:].split('/')
            for token in tokens[:-1]:
                node = node.setdefault(token, {})
                if not isinstance(node, dict): _proof('reference path has conflicting leaves')
            if tokens[-1] in node: _proof('reference path is duplicated')
            node[tokens[-1]] = index
        if tree is None: _proof('reference root has no canonical slot')
        roots.append(dict(root, tree=tree))
    return roots


def _task_binding(task, policy_hash):
    return {'task_id': task['id'], 'source_hash': task['source_hash'], 'policy_hash': policy_hash,
            'owner': task['actor'], 'parent_id': task['parent_id'],
            'lease': deepcopy(task['state'].get('delegation_lease'))}


def _identity(binding, role, phase, payload, schema, revision, *, presentation_revision=None):
    identity = {'version': VERSION, **binding, 'role': role, 'phase': phase,
                'canonical_schema': schema.__name__, 'canonical_schema_sha256': digest(schema.model_json_schema()),
                'wire_schema_sha256': digest(wire_schema(schema.__name__, payload, revision=revision).model_json_schema()),
                'model_input_sha256': digest(payload)}
    if revision is not None: identity['wire_revision'] = revision
    if presentation_revision is not None:
        if presentation_revision not in {CONTROLLER_CONTEXT, LEARNING_EVIDENCE, APPLICATION_CONTRACT, REFERENCE_TREE, ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}: _proof('unknown input presentation revision')
        identity['presentation_revision'] = presentation_revision
    return identity


def _assessment_evidence(payload):
    """Bounded exact UTF-8 pages; selection never asserts unread relevance."""
    source = (payload.get('result', {}).get('data', {}).get('response_analysis', {})
              .get('source', {}))
    if not isinstance(source, dict) or not isinstance(source.get('text'), str): return None
    if not isinstance(source.get('id'), str) or not source['id']: return None
    body = source['text']; raw = body.encode('utf-8')
    if len(raw) < 20000 or source.get('sha256') != hashlib.sha256(raw).hexdigest(): return None
    # The caller can supply requested original ranges after a source-needed
    # response. Those ranges take priority; no guessed heading or page semantics.
    requested = payload.get('additional_source_ranges', [])
    if not isinstance(requested, list): return None
    urls = [source.get('url', ''), *payload.get('collection_context', {}).get('planned_urls', [])]
    terms = [unquote(urlsplit(url).fragment).lower() for url in urls if urlsplit(url).fragment]
    terms += [part.lower() for target in payload.get('targets', [])
              for part in re.findall(r'[A-Za-z][A-Za-z_0-9]{3,}', target.get('id', ''))]
    terms = [term for term in terms if len(term) >= 3][:16]
    # Fixed original-byte pages make omitted intervals and later requests exact.
    pages = []
    for start in range(0, len(raw), 4096):
        end = min(len(raw), start + 4096)
        while end < len(raw) and raw[end] & 0xC0 == 0x80: end += 1
        if pages and start < pages[-1][1]: start = pages[-1][1]
        if start < end: pages.append((start, end))
    if not pages: return None
    scored = sorted(range(len(pages)), key=lambda i: (-sum(
        raw[pages[i][0]:pages[i][1]].decode('utf-8').lower().count(term) for term in terms), i))
    selected = {0, *scored[:3]}
    for item in requested:
        if (not isinstance(item, dict) or item.get('source_id') != source.get('id')
                or item.get('source_sha256') != source['sha256']
                or type(item.get('start_byte')) is not int or type(item.get('end_byte')) is not int
                or not 0 <= item['start_byte'] < item['end_byte'] <= len(raw)):
            return None
        selected.update(i for i, (a, b) in enumerate(pages)
                        if a < item['end_byte'] and b > item['start_byte'])
    entries = [{'start_byte': pages[i][0], 'end_byte': pages[i][1],
                'sha256': hashlib.sha256(raw[pages[i][0]:pages[i][1]]).hexdigest(),
                'text': raw[pages[i][0]:pages[i][1]].decode('utf-8')}
               for i in sorted(selected)]
    covered = sorted((item['start_byte'], item['end_byte']) for item in entries)
    omitted = []; cursor = 0
    for start, end in covered:
        if cursor < start: omitted.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < len(raw): omitted.append([cursor, len(raw)])
    return {'source_id': source.get('id'), 'source_sha256': source['sha256'],
            'total_bytes': len(raw), 'excerpts': entries,
            'omitted_ranges': omitted}


def _assessment_source_slots(payload):
    """Derive selectable original-byte pages from the same captured evidence."""
    evidence = _assessment_evidence(payload)
    if evidence is None: return None
    raw = payload['result']['data']['response_analysis']['source']['text'].encode('utf-8')
    requested = []
    for first, last in evidence['omitted_ranges']:
        start = first
        while start < last:
            end = min(start + 4096, last)
            while end < last and raw[end] & 0xC0 == 0x80: end += 1
            requested.append({'slot': len(requested), 'start_byte': start, 'end_byte': end})
            start = end
    return {'source_id': evidence['source_id'], 'source_sha256': evidence['source_sha256'],
            'citation_slots': [dict(slot=i, start_byte=e['start_byte'], end_byte=e['end_byte'])
                               for i, e in enumerate(evidence['excerpts'])],
            'request_slots': requested}


def requested_assessment_ranges(payload, diagnostic):
    """Extract a typed, exact source request from authenticated wire feedback."""
    if (not isinstance(diagnostic, dict) or diagnostic.get('representation') != 'json'
            or diagnostic.get('diagnostic_truncated') is not False): return []
    try: errors = json.loads(diagnostic['sanitized_response'])['errors']
    except (KeyError, ValueError, TypeError): return []
    requests = [item['returned'] for item in errors
                if item.get('type') == 'source_needed' and item.get('loc')
                and isinstance(item['loc'][-1], str)
                and item['loc'][-1].endswith('requested_source_ranges')]
    if len(requests) != 1 or not isinstance(requests[0], list): return []
    evidence = _assessment_evidence(payload)
    if evidence is None: return []
    valid = []
    for item in requests[0]:
        if (not isinstance(item, dict) or item.get('source_id') != evidence['source_id']
                or item.get('source_sha256') != evidence['source_sha256']
                or type(item.get('start_byte')) is not int or type(item.get('end_byte')) is not int
                or not any(a <= item['start_byte'] < item['end_byte'] <= b
                           for a, b in evidence['omitted_ranges'])): return []
        valid.append(item)
    return valid


def supplied_assessment_request(diagnostic):
    """Recognize a v3 request for bytes already shown, not another page need."""
    if (not isinstance(diagnostic, dict) or diagnostic.get('representation') != 'json'
            or diagnostic.get('diagnostic_truncated') is not False
            or diagnostic.get('omitted_chars') != 0):
        return False
    try: observed = json.loads(diagnostic['sanitized_response'])
    except (KeyError, ValueError, TypeError): return False
    return (isinstance(observed, dict) and observed.get('kind') == 'semantic_wire'
            and observed.get('version') == VERSION
            and isinstance(observed.get('errors'), list)
            and any(item.get('type') == 'foreign_range'
                    and item.get('loc') == ['assessments/0.requested_source_ranges']
                    for item in observed['errors'] if isinstance(item, dict)))


def retained_input(store, task, role, phase, payload, schema):
    """Select an original input before constructing new installed facts.

    This reads the existing captures, including measured but not yet sent ones.
    It neither creates a capture nor decides whether a response may be reused;
    the original call/recovery consumer still owns that decision.
    """
    if schema.__name__ not in WIRE_SCHEMAS: return None
    with store.lock:
        rows = store.db.execute(
            "SELECT body FROM records WHERE kind=? AND json_extract(body,'$.task_id')=? "
            "AND json_extract(body,'$.role')=? AND json_extract(body,'$.phase')=? "
            "AND json_extract(body,'$.canonical_schema')=? ORDER BY rowid",
            ('semantic_wire_request', task['id'], role, phase, schema.__name__)).fetchall()
    matches = {}; exact = {}
    for row in rows:
        record = json.loads(row['body'])
        original = record.get('canonical_input')
        if not isinstance(original, dict): _proof('captured model input is malformed')
        comparable = {k: v for k, v in original.items() if k != 'installed_controller_context'}
        incoming=payload
        if original.get('policy_input_contract')=='role-scoped-v1':
            incoming=m.scoped_policy_input(payload)
        elif 'policy_input_contract' not in original:
            # The display contract is not a change to an already captured
            # judgment. Restore its original input; only new requests get U34.
            incoming={k:v for k,v in payload.items() if k!='policy_input_contract'}
        # Existing call recovery permits only this send-time operation preview
        # to age. Reuse that same boundary: caller payload/history, source and
        # every other field must still match, and load checks the current lease.
        if ({k: v for k, v in comparable.items() if k != 'history_lookup'} !=
                {k: v for k, v in incoming.items() if k != 'history_lookup'}): continue
        load(store, {'id': record['id'], 'sha256': digest(record)}, original, schema, role, phase)
        matches[digest(original)] = original
        if comparable == incoming: exact[digest(original)] = original
    if exact: matches = exact
    if len(matches) > 1: _proof('original installed-context input is ambiguous')
    return deepcopy(next(iter(matches.values()))) if matches else None


def capture(store, task, role, phase, payload, schema):
    if schema.__name__ not in WIRE_SCHEMAS: return None
    binding = _task_binding(task, store.policy_hash)
    # Old captures keep their original schema, messages and authentication. A
    # newly issued request gets an explicit wire branch, without changing the
    # canonical caller payload or silently restamping a saved response.
    identity = _identity(binding, role, phase, payload, schema, None)
    old_key = digest(identity)
    old = store.record_get('semantic_wire_request', old_key)
    if old is not None:
        load(store, {'id': old_key, 'sha256': digest(old)}, payload, schema, role, phase)
        return {'id': old_key, 'sha256': digest(old)}
    if schema.__name__ in ADMISSION_WIRE_SCHEMAS:
        identity = _identity(binding, role, phase, payload, schema, ADMISSION_REFERENCES)
    from .learning_projection import active
    if schema.__name__ in {'Assessment', 'AssessmentBatch', 'Review', 'Disposition'}:
        source = _reference_source(payload, store) if schema.__name__ in {'Review', 'Disposition'} else payload
        if active(source) and (schema.__name__ != 'Disposition' or source.get('learning_correction_scope')):
            identity = _identity(binding, role, phase, payload, schema, LEARNING_BINDINGS)
            old_key = digest(identity)
            old = store.record_get('semantic_wire_request', old_key)
            if old is not None:
                load(store, {'id': old_key, 'sha256': digest(old)}, payload, schema, role, phase)
                return {'id': old_key, 'sha256': digest(old)}
            if schema.__name__ in {'Review', 'Disposition'}:
                identity = _identity(binding, role, phase, payload, schema, LEARNING_TARGETS)
    # Preserve any original capture, including an unobserved/partial call. A
    # presentation change belongs only to a request that has never been captured.
    previous_key = digest(identity)
    previous = store.record_get('semantic_wire_request', previous_key)
    if previous is not None:
        load(store, {'id': previous_key, 'sha256': digest(previous)}, payload, schema, role, phase)
        return {'id': previous_key, 'sha256': digest(previous)}
    if _controller_shared_fields(payload):
        identity = _identity(binding, role, phase, payload, schema, identity.get('wire_revision'),
                             presentation_revision=CONTROLLER_CONTEXT)
        previous_key = digest(identity)
        previous = store.record_get('semantic_wire_request', previous_key)
        if previous is not None:
            load(store, {'id': previous_key, 'sha256': digest(previous)}, payload, schema, role, phase)
            return {'id': previous_key, 'sha256': digest(previous)}
    if _learning_feedback_roots(payload):
        identity = _identity(binding, role, phase, payload, schema, identity.get('wire_revision'),
                             presentation_revision=LEARNING_EVIDENCE)
        previous_key = digest(identity)
        previous = store.record_get('semantic_wire_request', previous_key)
        if previous is not None:
            load(store, {'id': previous_key, 'sha256': digest(previous)}, payload, schema, role, phase)
            return {'id': previous_key, 'sha256': digest(previous)}
    # All historical branches above retain their exact schema and messages.
    # Applied Knowledge uses the same source-slot codec only on a new capture.
    if (schema.__name__ in {'Review', 'Disposition'}
            and _applied_review(_reference_source(payload, store, applied=True))):
        identity = _identity(binding, role, phase, payload, schema, APPLIED_TARGETS,
                             presentation_revision=REFERENCE_TREE)
    if schema.__name__ in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
        identity = _identity(binding, role, phase, payload, schema, identity.get('wire_revision'),
                             presentation_revision=APPLICATION_CONTRACT)
    if identity.get('wire_revision') == LEARNING_TARGETS:
        identity = _identity(binding, role, phase, payload, schema, LEARNING_TARGETS,
                             presentation_revision=REFERENCE_TREE)
    if schema.__name__ in {'Assessment', 'AssessmentBatch'}:
        # All saved request variants above retain their original class/hash.
        referenced = identity.get('wire_revision') == LEARNING_BINDINGS
        if referenced:
            # A request captured by the previous installed controller still
            # owns its exact response, including an empty candidate catalog.
            # Resolve that identity before choosing the narrower fresh schema.
            old_identity = _identity(binding, role, phase, payload, schema, REFERENCED_ASSESSMENT_DEFAULTS,
                                     presentation_revision=identity.get('presentation_revision'))
            old_key = digest(old_identity)
            old = store.record_get('semantic_wire_request', old_key)
            if old is not None:
                load(store, {'id': old_key, 'sha256': digest(old)}, payload, schema, role, phase)
                return {'id': old_key, 'sha256': digest(old)}
        known = (associations(payload, schema.__name__, store, revision=LEARNING_BINDINGS).get('known_ideas', [])
                 if referenced else [])
        revision = REFERENCED_ASSESSMENT_DEFAULTS if known else ASSESSMENT_DEFAULTS
        identity = _identity(binding, role, phase, payload, schema, revision,
                             presentation_revision=identity.get('presentation_revision'))
        if schema.__name__ == 'AssessmentBatch' and _assessment_evidence(payload):
            # An old exact capture remains its own request, regardless of
            # success, failure or an unknown send. Only a fresh canonical
            # input receives the new evidence presentation and response shape.
            prior_key = digest(identity)
            prior = store.record_get('semantic_wire_request', prior_key)
            if prior is not None:
                load(store, {'id': prior_key, 'sha256': digest(prior)}, payload, schema, role, phase)
                return {'id': prior_key, 'sha256': digest(prior)}
            evidence_identity = _identity(binding, role, phase, payload, schema, ASSESSMENT_EVIDENCE,
                                          presentation_revision=ASSESSMENT_EVIDENCE)
            saved_evidence = store.record_get('semantic_wire_request', digest(evidence_identity))
            if saved_evidence is not None:
                load(store, {'id': saved_evidence['id'], 'sha256': digest(saved_evidence)}, payload, schema, role, phase)
                return {'id': saved_evidence['id'], 'sha256': digest(saved_evidence)}
            compact_identity = _identity(binding, role, phase, payload, schema, ASSESSMENT_COMPACT,
                                         presentation_revision=ASSESSMENT_COMPACT)
            saved_compact = store.record_get('semantic_wire_request', digest(compact_identity))
            if saved_compact is not None:
                load(store, {'id': saved_compact['id'], 'sha256': digest(saved_compact)}, payload, schema, role, phase)
                return {'id': saved_compact['id'], 'sha256': digest(saved_compact)}
            target_identity = _identity(binding, role, phase, payload, schema, ASSESSMENT_TARGET,
                                        presentation_revision=ASSESSMENT_TARGET)
            saved_target = store.record_get('semantic_wire_request', digest(target_identity))
            if saved_target is not None:
                load(store, {'id': saved_target['id'], 'sha256': digest(saved_target)}, payload, schema, role, phase)
                return {'id': saved_target['id'], 'sha256': digest(saved_target)}
            corrected_identity = _identity(binding, role, phase, payload, schema, ASSESSMENT_TARGET_CORRECTION,
                                           presentation_revision=ASSESSMENT_TARGET_CORRECTION)
            saved_corrected = store.record_get('semantic_wire_request', digest(corrected_identity))
            if saved_corrected is not None:
                load(store, {'id': saved_corrected['id'], 'sha256': digest(saved_corrected)}, payload, schema, role, phase)
                return {'id': saved_corrected['id'], 'sha256': digest(saved_corrected)}
            ranged_identity = _identity(binding, role, phase, payload, schema, ASSESSMENT_TARGET_RANGES,
                                        presentation_revision=ASSESSMENT_TARGET_RANGES)
            saved_ranged = store.record_get('semantic_wire_request', digest(ranged_identity))
            if saved_ranged is not None:
                load(store, {'id': saved_ranged['id'], 'sha256': digest(saved_ranged)}, payload, schema, role, phase)
                return {'id': saved_ranged['id'], 'sha256': digest(saved_ranged)}
            transition = payload.get('assessment_revision_transition', {})
            selected = _new_assessment_revision(payload)
            identity = _identity(binding, role, phase, payload, schema, selected,
                                 presentation_revision=selected)
    key = digest(identity)
    def create_once():
        saved = store.record_get('semantic_wire_request', key)
        if saved:
            load(store, {'id': key, 'sha256': digest(saved)}, payload, schema, role, phase)
            return {'id': key, 'sha256': digest(saved)}
        record = {'id': key, **identity, 'nonce': uuid4().hex, 'canonical_input': deepcopy(payload),
                  'association': associations(payload, schema.__name__, store, revision=identity.get('wire_revision'),
                                              presentation_revision=identity.get('presentation_revision'), binding=binding)}
        store.record('semantic_wire_request', key, record)
        return {'id': key, 'sha256': digest(record)}
    return store._transaction(create_once)


def _new_assessment_revision(payload):
    """Select only a new capture; exact saved captures are resolved first."""
    transition = payload.get('assessment_revision_transition', {})
    if (len(payload.get('targets', [])) != 1
            or transition.get('new_wire_revision') == ASSESSMENT_COMPACT):
        return ASSESSMENT_COMPACT
    # The explicit resume marker is made by the authenticated stopped-call
    # path. A new request after that boundary must use the corrected shape;
    # the original unknown send and all of its old v1 captures stay intact.
    resumed = payload.get('interrupted_model_resume', {}).get('version') == 'explicit-model-resume-v1'
    if transition.get('new_wire_revision') in {ASSESSMENT_TARGET_CORRECTION,
                                                ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        return transition['new_wire_revision']
    if resumed or payload.get('actual_format_feedback') is not None:
        return ASSESSMENT_REFERENCE_SLOTS
    return transition.get('new_wire_revision', ASSESSMENT_REFERENCE_SLOTS)


def load(store, ref, payload, schema, role, phase, *, current=True):
    try:return _load(store, ref, payload, schema, role, phase, current=current)
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        _proof('captured association is malformed: ' + type(error).__name__)


def _load(store, ref, payload, schema, role, phase, *, current):
    record = store.record_get('semantic_wire_request', ref.get('id')) if isinstance(ref, dict) else None
    if not record or digest(record) != ref.get('sha256'): _proof('captured association reference differs')
    identity = {k: v for k, v in record.items() if k not in {'id', 'nonce', 'canonical_input', 'association'}}
    binding = {key: record[key] for key in ('task_id', 'source_hash', 'policy_hash', 'owner', 'parent_id', 'lease')}
    expected = _identity(binding, role, phase, payload, schema, record.get('wire_revision'),
                         presentation_revision=record.get('presentation_revision'))
    if (record['id'] != digest(identity) or identity != expected
            or record.get('canonical_input') != payload
            or record.get('association') != associations(payload, schema.__name__, store, revision=record.get('wire_revision'),
                                                        presentation_revision=record.get('presentation_revision'), binding=binding)):
        _proof('captured input/schema/role/ordered association differs')
    try: UUID(record['nonce'])
    except (ValueError, TypeError): _proof('identity seed is invalid')
    # Authentic historical bytes and applicability to current work are separate.
    if current and binding != _task_binding(store.get_task(record['task_id']), store.policy_hash):
        _proof('current source/policy/owner/parent/lease differs from captured request')
    return record


def _compact_assessment_input(record, original):
    """Present one decision's facts; keep the complete input in the capture."""
    plan = original.get('parent_operation', {}).get('args', {}).get('plan', {})
    result = original.get('result', {})
    data = result.get('data', {})
    analysis = data.get('response_analysis', {})
    evidence = _assessment_evidence(original)
    if evidence is None: _proof('captured assessment evidence cannot be reproduced')
    # Source coverage is parent-owned. Its exact complete rows, candidate
    # meanings and HTTP result remain bound to canonical_input/association.
    # The child still receives the current target, applicable plan conditions,
    # actual result status and the exact source bytes needed for this judgment.
    compact_plan = {k: deepcopy(v) for k, v in plan.items() if k != 'source_coverage'}
    compact = {k: deepcopy(original[k]) for k in (
        'objective', 'instruction', 'policy_input_contract', 'phase_contract',
        'targets', 'required_thinking_targets', 'operation', 'source_context',
        'collection_context', 'learning_correction_scope', 'research_choice',
        'role_context', 'acceptance') if k in original}
    compact['parent_operation'] = {k: deepcopy(v) for k, v in original.get('parent_operation', {}).items()
                                   if k not in {'args', 'decisions'}}
    compact['parent_operation']['plan'] = compact_plan
    compact['parent_operation']['source_coverage'] = {
        'count': len(plan.get('source_coverage', [])),
        'sha256': digest(plan.get('source_coverage', [])),
        'meaning': 'Parent-owned complete condition mapping remains in the authenticated original input.'}
    compact['result'] = {k: deepcopy(v) for k, v in result.items() if k != 'data'}
    compact['result']['data'] = {k: deepcopy(v) for k, v in data.items() if k != 'response_analysis'}
    compact['result']['data']['response_analysis'] = {
        **{k: deepcopy(v) for k, v in analysis.items() if k != 'source'},
        'source': {**{k: deepcopy(v) for k, v in analysis['source'].items() if k != 'text'},
                   'text': evidence}}
    compact['captured_context'] = {
        'sha256': digest(original), 'candidate_count': len(record['association'].get('known_ideas', [])),
        'instruction': 'The controller retains the complete original input, parent source coverage, HTTP result and candidate meanings. This digest does not prove those omitted bytes were read by this assessment. Judge the supplied target and exact excerpts; request any additional original source range before citing it.'}
    return compact


def projected_input(record):
    payload = (_compact_assessment_input(record, record['canonical_input'])
               if record.get('presentation_revision') in {ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}
               else deepcopy(record['canonical_input']))
    # The original complete input stays captured. Replace only redundant output
    # transcription instructions; policy and semantic facts are not trimmed.
    for name in ('exact_response_contract', 'thinking_instruction', 'deferred_contract', 'application_output_contract'):
        payload.pop(name, None)
    # Full bodies already occur in the original input. Name their exact ordered
    # slots without adding another copy of long Skills, opinions or Ideas.
    slots = {
        'Disposition': {'opinion_decisions': 'review.opinions'},
        'Assessment': {'thinking_values': 'required_thinking_targets'},
        'AssessmentBatch': {'assessments': 'targets', 'thinking_values': 'required_thinking_targets'},
        'SkillSelection': {'decisions': 'knowledge.skills'}, 'Cleanup': {'decisions': 'knowledge.skills'},
        'TaskPlan': {'source_decisions': 'source_context.pending_ids', 'prior_acceptance_decisions': 'source_context.prior_acceptance', 'deferred_decisions': 'deferred_for_planning.candidates'},
        'Completion': {'acceptance': 'acceptance'},
        'EngineeringInvestigation': {'checks': 'admission_contract.work_items'},
    }.get(record['canonical_schema'], {})
    if record['canonical_schema'] in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
        slots = {'existing_idea_decisions': 'required_ideas', 'applications.skill_slot': 'pre.skills.selected (zero based)'}
        if 'application_contract' in payload:
            payload['application_contract']['instruction'] = ('Selection is not use. Select an actual pre-selected Skill slot only when observed use is supported, and choose actual result JSON pointers with explanations. Empty applications preserve unobserved use. Code binds the selected versions, operation and result; no use or benefit is inferred.')
            for key in ('fields', 'field_sources', 'required_fields', 'required_application_fields', 'schema'):
                payload['application_contract'].pop(key, None)
        if 'skill_update_contract' in payload:
            payload['skill_update_contract']['skill_updates'] = skill_updates_schema()
            payload['skill_update_contract']['meaning']['improve_organize'] = 'Choose an existing visible target and the substantive changes. The controller attaches its captured version; atomic CAS and visibility guards remain.'
            payload['skill_update_contract']['meaning']['merge'] = 'Choose a visible target and distinct other merge_ids and complete resulting content. The controller attaches their captured versions; atomic guards remain.'
    if record['canonical_schema'] == 'TaskPlan':
        payload['source_context']['instruction'] = ('Classify every pending source in order. Decide every prior acceptance in order, retain with criterion=null, or replace/withdraw with an exact quote from an explicitly corrective source. New criteria require exact source quotes. Code binds the original source/criterion/index/hash and derives the resulting acceptance list. Fully read attachments before classification. A classification is reviewed interpretation, never permission to amend policy.')
    payload['semantic_response'] = {'version': VERSION, 'family': record['canonical_schema'],
        'ordered_slot_sources': slots, 'association_sha256': digest(record['association']),
        'instruction': 'Return only the supplied semantic response schema. Array positions correspond exactly to the frozen ordered association; return every required slot, no missing or extra slots. Do not return fixed IDs, hashes or memberships omitted from that schema. Code attaches these exact associations; that does not establish understanding, Skill use, evidence relevance, permission or success. Choose all dispositions, actual-use evidence, source quotations and verdicts yourself. Preserve unknowns. New proposals receive controller identities; do not invent record IDs. The original context remains evidence, not additional output assignments.'}
    if 'judgment_corrections' in record['association']:
        payload['semantic_response']['judgment_resolution']={
            'instruction':'For judgment_resolve return args={correction_ref: zero-based correction_inputs index, evidence_refs: nonempty distinct zero-based indices into that correction eligible_evidence, reason: your substantive correction explanation}. Read original_sources via original_source_key and the actual result evidence. Code attaches exact review/version/operation IDs. Eligibility is not relevance or proof that a criticism is resolved; the normal independent review remains required. Other operation args are unchanged.',
            'corrections_pointer':'/web_correction_context/correction_inputs'}
    if record.get('wire_revision') == ADMISSION_REFERENCES:
        payload['semantic_response']['wire_revision'] = ADMISSION_REFERENCES
        payload['semantic_response']['admission_references'] = {
            'slots': [dict(slot=index, **item) for index, item in enumerate(record['association']['admission_references'])],
            'instruction': 'For every source_refs or evidence_refs item, select reference_slot of kind evidence. '
                'For addressed_findings and dependency finding_ids select kind finding; for prerequisite_id select kind prerequisite. '
                'These are selections, not positional coverage assignments. Read the referenced original evidence and retained '
                'findings/checks; this table does not replace their full content. Code restores exact captured identifiers and '
                'checks namespace membership. Supply all semantic reasons, alternative causes, new findings, opinions and '
                'required preparation yourself. A valid reference does not establish relevance, readiness, permission or success.'}
    # Historical captures take precisely the unchanged branch above. This
    # explicit input marker creates its own message/receipt identity.
    from .learning_projection import active, learning_view
    if active(payload):
        if 'required_idea_groups' in record['association']:
            payload['required_ideas'] = [dict(g['idea'], original_ids=g['members'])
                                        for g in record['association']['required_idea_groups']]
        grounded = record.get('wire_revision') in {LEARNING_BINDINGS, LEARNING_TARGETS} and record['canonical_schema'] in {'Review', 'Disposition'}
        # Quoted pointers address original array positions, including duplicate
        # meanings. Only candidate-decision outputs use grouped display slots.
        if not grounded:
            if 'learning' in payload:payload['learning'] = learning_view(payload['learning'])
            if isinstance(payload.get('bundle'),dict) and 'learning' in payload['bundle']:
                payload['bundle']['learning']=learning_view(payload['bundle']['learning'])
        payload['semantic_response']['candidate_semantics'] = (
            'An exact repeated candidate meaning is one current decision slot with all original_ids preserved. '
            'Different rationale, disposition or next operation remains a separate choice. '
            'Assess every supplied target and thinking value. A worthwhile unchanged candidate may be proposed again; '
            'code reuses its captured identity without inventing another logical improvement. No empty assessment is allowed.')
        if record['canonical_schema']=='Disposition' and payload.get('learning_correction_scope'):
            payload['semantic_response']['revision_scope'] = (
                'This acquisition has no editable Learning. Choose none for proceed/hold, or governed for revise. '
                'A prepared request stays unexecuted until corrected; an acquired result stays held without refetch. '
                'Address every independent opinion with reasons. Do not rewrite captured facts or fabricate a correction.'
                if not payload['learning_correction_scope']['editable'] else
                'Choose none for proceed/hold. For revise choose learning only for changes to the uncommitted Learning, '
                'or governed when actual diagnosis/correction of a different target is necessary. Governed parks this '
                'Learning and selects normal reviewed work followed by judgment_resolve; it never regenerates Learning '
                'as a substitute for that work. Reject unsupported immutable-fact objections with reasons; do not fabricate a correction.')
    if record.get('wire_revision') in {LEARNING_BINDINGS, REFERENCED_ASSESSMENT_DEFAULTS, ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        payload['semantic_response']['wire_revision'] = record['wire_revision']
        if record['canonical_schema'] in {'Assessment', 'AssessmentBatch'}:
            slots = []
            visible = [(f'/{key}/{i}', item) for key in ('required_ideas',)
                       for i,item in enumerate(payload.get(key, []))]
            visible += [(f'/learning/ideas/{i}', item) for i,item in enumerate(payload.get('learning', {}).get('ideas', []))]
            for index, group in enumerate(record['association'].get('known_ideas', [])):
                if record.get('presentation_revision') in {ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
                    idea = group['idea']
                    slots.append({'slot': index, 'target': idea['target'], 'proposal': idea['proposal'],
                                  'disposition': idea['disposition'], 'rationale': idea['rationale'],
                                  'next_operation': deepcopy(idea['next_operation'])})
                    continue
                pointer = next((p for p,item in visible if item.get('id') == group['idea']['id']
                                and all(item.get(k) == v for k,v in group['idea'].items())), None)
                slots.append({'slot': index, **({'pointer': pointer} if pointer else
                              {'idea': group['idea'], 'original_ids': group['members']})})
            payload['semantic_response']['candidate_slots'] = slots
            if record.get('presentation_revision') in {ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
                payload['semantic_response']['current_decision_shape'] = {
                    'assessment_rows': len(record['association']['target_ids']),
                    'thinking_targets_in_order': deepcopy(record['association']['thinking_targets']),
                    'candidate_slot_count': len(slots),
                    'meaning': 'The counts and ordered slots are code-owned. Supply substantive values for each thinking target, and decide whether each candidate applies. A source request is provisional: the controller supplies omitted original pages and retains the original response; any judgment affected by new pages must be reconsidered. Missing source selections or mixed existing/new idea fields cannot be inferred by code.'}
            payload['semantic_response']['candidate_semantics'] = (
                'For each target, supply all thinking values and at least one concrete candidate. '
                'To consider an unchanged supplied candidate, use candidate_slot with your specific current consideration: '
                'explain applicability, alternatives and whether any new target or substantive change is warranted. '
                'Code restores its exact captured identity and complete meaning. Put current target reasoning in consideration; '
                'do not retranscribe or rename an unchanged candidate just to reconsider it. '
                'Use a complete NewIdea for a genuinely new or changed candidate. If both the unchanged candidate '
                'and a changed proposal matter, return two separate ideas: one candidate_slot with consideration '
                'and one complete NewIdea. Never combine their fields in one idea. Neither choice waives concrete thought '
                'or candidate derivation; empty/no-idea statements do not satisfy the policy.')
        else:
            payload['semantic_response']['reference_source'] = {
                'sha256': record['association']['reference_source_sha256'],
                'roots': record['association']['reference_roots'],
                'instruction': 'Select JSON pointers into the captured original source (the original source_packet for bounded pages). '
                    'Quote exact text from that scalar or empty collection. Do not cite an entire container or locate a quote '
                    'at another field. Code binds each quote to its exact source, stage and editability. Evidence from a before '
                    'snapshot describes that time; it is not a current-state assertion. Review opinions still need semantic '
                    'assessment. For Disposition, revision_targets must be empty for proceed/hold and nonempty for revise. '
                    'Every learning revision target must lie inside current Learning; a governed revision requires a target '
                    'outside it and follows the existing governed route. Immutable evidence cannot be rewritten by Learning.'}
    if record.get('wire_revision') in {LEARNING_TARGETS, APPLIED_TARGETS}:
        payload['semantic_response']['wire_revision'] = record['wire_revision']
        payload['semantic_response']['reference_source'] = {
            'sha256': record['association']['reference_source_sha256'],
            'slots': [dict(slot=index, **{k:v for k,v in item.items() if k != 'value_sha256'})
                      for index, item in enumerate(record['association']['source_slots'])],
            'instruction': 'Choose source_slot from this table for each opinion evidence reference or revision target. '
                'Read the corresponding original field or identified record in full; the table does not replace it. '
                'Return your semantic observation and rationale, without transcribing pointers, quotes, IDs or versions. '
                'Code binds the exact original value, source, temporal stage and editability. A before snapshot is '
                'evidence about that time, not a current-state assertion. This binding does not prove a claim correct. '
                'For Disposition, revision_targets is empty for proceed/hold and nonempty for revise. Learning '
                'targets must all be editable; governed requires a target outside Learning and uses the existing '
                'reviewed correction route. An immutable historical target cannot be rewritten by Learning.'}
        if record['wire_revision'] == APPLIED_TARGETS:
            payload['semantic_response']['reference_source']['instruction'] = (
                'Choose source_slot for every opinion and revision target, and read its exact original value. '
                'Code binds the original bytes, temporal stage, permissions and current assessment target. '
                'The Knowledge outcome is already committed; all references are immutable evidence. '
                'An accepted expected_hash describes the before version, while transition.after records the '
                'committed version; different versions alone are not a contradiction. Judge the actual '
                'relationship and every assessment against its own target and rationale. Parent operation '
                'requirements are not discharged by a Knowledge commit. Preserve genuine new criticism and '
                'respond to every opinion. Reference validity does not establish semantic correctness. '
                'For Disposition use no revision_targets for proceed/hold, and exact nonempty targets for revise. '
                'A revise selects the existing governed correction and judgment_resolve route; it cannot '
                'rewrite this capture, regenerate accepted Learning or apply the committed mutation again.')
        if record.get('presentation_revision') == REFERENCE_TREE:
            reference = payload['semantic_response']['reference_source']
            del reference['slots']
            reference.update(revision=REFERENCE_TREE, roots=_reference_tree(record['association']))
            reference['instruction'] += (' The root tree factors that same table: each dictionary key is an already '
                'RFC6901-escaped path token (including array indices); join tokens to the root pointer with /. '
                'Each integer leaf is the original global source_slot, including a root-valued leaf. Every leaf '
                'inherits its root stage and editable value. Decode ~1 as / and ~0 as ~ only when reading the '
                'original value, never when joining the pointer. All original content is still supplied.')
    if record.get('presentation_revision') == APPLICATION_CONTRACT:
        _present_application_contract(record, payload)
    if record.get('presentation_revision') in {ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        evidence = _assessment_evidence(record['canonical_input'])
        if evidence is None: _proof('captured assessment evidence cannot be reproduced')
        if record.get('presentation_revision') == ASSESSMENT_EVIDENCE:
            payload['result']['data']['response_analysis']['source']['text'] = evidence
        payload['semantic_response']['source_evidence'] = {
            'revision': record['presentation_revision'],
            'instruction': 'The source text above contains exact original UTF-8 byte pages. Omitted ranges are unread in this judgment. Cite exact supplied byte subranges through source_citations. If another original range is needed, put it in requested_source_ranges on the assessment row; the controller holds this judgment and supplies that original range in this stage. A citation proves only the quoted original bytes, not relevance, full-document review, source adoption, or success. The independent reviewer judges the original evidence separately.',
            'example': 'assessments[0].requested_source_ranges=[{source_id,source_sha256,start_byte,end_byte}]; assessments[0].source_citations holds only supplied bytes; ideas may select {candidate_slot,consideration} or a complete NewIdea.'
            if record.get('presentation_revision') in {ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES} else None}
        if record.get('presentation_revision') == ASSESSMENT_REFERENCE_SLOTS:
            slots = _assessment_source_slots(record['canonical_input'])
            if slots is None: _proof('captured source slots cannot be reproduced')
            payload['semantic_response']['source_evidence'] = {
                'revision': ASSESSMENT_REFERENCE_SLOTS,
                'source_citation_slots': slots['citation_slots'],
                'requested_source_slots': slots['request_slots'],
                'instruction': 'Select supplied original pages by source_citation_slots. Select unread original pages by requested_source_slots only when their contents are necessary; the controller will supply them before accepting this judgment. Code binds the exact source identity, hash and byte ranges. A supplied page does not prove relevance or full-document review. Independent review still judges the actual source and conclusions.'}
            payload['semantic_response']['candidate_semantics'] = (
                'Put unchanged candidate selections with current applicability reasons in existing_idea_decisions. '
                'Put genuinely new or changed proposals in new_ideas; never mix the two. '
                'Code binds existing candidate bodies and identities. At least one concrete candidate is required. '
                'Keep substantive thinking and genuinely new criticism; do not create an idea merely to fill a field.')
    if record.get('presentation_revision') in {CONTROLLER_CONTEXT, LEARNING_EVIDENCE, APPLICATION_CONTRACT, REFERENCE_TREE, ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        shared = _controller_shared_fields(payload)
        for item in shared:
            rows = payload
            for token in item['array_pointer'][1:].split('/'):
                token = token.replace('~1', '/').replace('~0', '~')
                rows = rows[int(token)] if isinstance(rows, list) else rows[token]
            for row in rows: del row[item['field']]
        if shared or record.get('presentation_revision') == CONTROLLER_CONTEXT:
            payload['semantic_response']['inherited_fields'] = {
                'revision': CONTROLLER_CONTEXT, 'arrays': shared,
                'instruction': 'Every row at each array_pointer inherits the stated field and its exact value. '
                    'All row identities, order and other values remain explicit. Read the common value as part of every row; '
                    'original source pointers and evidence selections refer to that fully expanded meaning. '
                    'This factors only identical fields in captured plan_task context; it omits no policy condition or judgment.'}
    if (record.get('presentation_revision') == LEARNING_EVIDENCE
            or record.get('presentation_revision') in {APPLICATION_CONTRACT, REFERENCE_TREE} and _learning_feedback_roots(payload)):
        _present_learning_values(payload)
    return payload


def _application_source(payload, store, binding):
    if payload.get('learning_anchor_ref'):
        packet = store.record_get('bounded_input', payload['source_packet_id'])
        if (not packet or packet['id'] != digest({k: v for k, v in packet.items() if k != 'id'})
                or packet['task_id'] != binding['task_id'] or packet['source_hash'] != binding['source_hash']
                or packet['policy_hash'] != binding['policy_hash']): _proof('Application original packet differs')
        return packet['value']
    return payload


def _application_context(payload, original):
    """Capture one shared witness definition for the current message.

    Canonical input/catalog/history remains unchanged. A nonselectable value is
    still supplied in that original result; it is never converted to absence.
    """
    result = original.get('result', {})
    context = {'result_evidence': [], 'unavailable_result_evidence': []}
    for entry in payload.get('application_contract', {}).get('result_evidence', []):
        try:value = m.application_evidence_value(result, entry['pointer'])
        except m.PolicyError as error:
            context['unavailable_result_evidence'].append(dict(entry, reason=str(error)))
            continue
        if entry['sha256'] != digest(value):_proof('Application evidence catalog differs from the original result')
        context['result_evidence'].append(dict(entry, observed_value='recorded-empty' if value in ('', [], {}) else 'recorded-content'))
    return context


def _present_application_contract(record, payload):
    """Current response instructions come from the real wire schema only."""
    schema = wire_schema(record['canonical_schema'], record['canonical_input'],
                         revision=record.get('wire_revision')).model_json_schema()
    def inline(value):
        if isinstance(value, list):return [inline(item) for item in value]
        if not isinstance(value, dict):return value
        if '$ref' in value:
            value = {**schema['$defs'][value['$ref'].rsplit('/', 1)[-1]], **{k:v for k,v in value.items() if k != '$ref'}}
        return {key:inline(item) for key,item in value.items()}
    properties = schema['properties']; current = record['association']['application_context']
    contract = payload.setdefault('application_contract', {})
    contract.update(schema=APPLICATION_CONTRACT, allowed_output_fields=list(properties),
        result_evidence=deepcopy(current['result_evidence']),
        unavailable_result_evidence=deepcopy(current['unavailable_result_evidence']),
        evidence_scope='Only recorded output values of a known succeeded/failed result are selectable. '
            'An explicit empty collection/text supports absence at that exact scope, never Skill use, prevention '
            'or benefit by itself. Null, absent pointers and unknown execution are not observations. '
            'Nonselectable markers remain in the full original result; their omission from selectable witnesses '
            'does not turn them into empty or successful values. Every application still needs substantive '
            'reasoning, exact selected-procedure/result binding and the required independent review.')
    if 'applications' in properties:
        contract['applications'] = inline(properties['applications'])
        contract['instruction'] = ('Return only the actual wire output fields. Each application selects skill_slot '
            'and evidence containing pointer and explanation only. Code supplies every Skill/procedure/operation/'
            'result ID and hash; never return those fixed fields, including evidence.sha256. Result-evidence hashes '
            'below describe the captured input, not output fields. Use applications=[] when procedure use is unobserved.')
    else:
        contract.pop('applications', None);contract.pop('unobserved_use', None)
        contract['instruction'] = 'This response does not own applications. Return only allowed_output_fields; the original application evidence is context, not an additional output assignment.'
    feedback = payload.get('actual_format_feedback')
    if isinstance(feedback, dict) and 'rejected_response' in feedback:
        feedback['canonical_or_diagnostic_history'] = feedback.pop('rejected_response')
        feedback['history_semantics'] = ('Immutable rejected canonical output or original bounded provider diagnostic, '
            'preserved in full as supplied. Controller-added fields in canonical history are not a response template. '
            'Use the actual wire schema for every output field. All original judgments and Ideas remain in this '
            'history and the current input. This history is not acceptance, use or retry authority.')
        feedback['instruction'] = ('Resolve the actual validation diagnostic using the current response schema. '
            'Return a complete new phase response in its exact allowed fields, retaining required Ideas and unknowns. '
            'Do not copy controller IDs/hashes from canonical history, invent use/benefit, or claim any target action occurred.')


def _learning_feedback_roots(payload):
    """Locate only explicitly versioned current Learning feedback."""
    roots = []
    def walk(value, pointer):
        if isinstance(value, dict):
            if value.get('version') == 'learning-current-v1' and value.get('fact_projection') == 'current-frontier-v2':
                roots.append((pointer, value)); return
            for key, item in value.items():walk(item, pointer+'/'+key.replace('~','~0').replace('/','~1'))
        elif isinstance(value, list):
            for index, item in enumerate(value):walk(item, pointer+'/'+str(index))
    walk(payload, '')
    return roots


def _present_learning_values(payload):
    """Reuse exact values actually delivered in this message, never bare archives.

    This runs after the existing wire projections: a canonical value removed
    or changed by those projections is not a usable current-message anchor.
    Only the known feedback value fields change; source aliases stay intact.
    """
    roots = _learning_feedback_roots(payload)
    candidates = []
    for root, feedback in roots:
        for collection in ('facts', 'stage_judgments'):
            for index, row in enumerate(feedback.get(collection, [])):
                if 'value' in row:candidates.append((root+'/'+collection+'/'+str(index)+'/value', row, 'value'))
        for index, row in enumerate(feedback.get('opinion_frontier', [])):
            for field in ('opinion', 'response'):
                if row.get(field) is not None:
                    candidates.append((root+'/opinion_frontier/'+str(index)+'/'+field, row, field))
        for key, row in feedback.get('opinion_evidence', {}).items():
            candidates.append((root+'/opinion_evidence/'+key+'/value', row, 'value'))
    wanted = {digest(row[key]) for _, row, key in candidates}
    anchors = {}; excluded = {pointer for pointer, _ in roots}
    def index(value, pointer):
        if pointer in excluded or pointer == '/semantic_response':return
        # An ancestor of a replaced field is not a stable original either.
        if pointer and not any(root.startswith(pointer+'/') for root in excluded):
            sha = digest(value)
            if sha in wanted:anchors.setdefault(sha, []).append((pointer, value))
        if isinstance(value, dict):
            for key, item in value.items():index(item, pointer+'/'+key.replace('~','~0').replace('/','~1'))
        elif isinstance(value, list):
            for position, item in enumerate(value):index(item, pointer+'/'+str(position))
    index(payload, '')
    references = []
    for pointer, row, field in candidates:
        value = row[field]; sha = digest(value)
        match = next((path for path, original in anchors.get(sha, [])
                      if type(original) is type(value) and original == value), None)
        if match is None:
            index(value, pointer)
            continue
        row[field] = {'delivered_value_ref': match, 'value_sha256': sha}
        references.append({'pointer': pointer, 'value_pointer': match, 'value_sha256': sha})
    payload['semantic_response']['shared_learning_values'] = {
        'revision': LEARNING_EVIDENCE, 'references': references,
        'instruction': 'Every delivered_value_ref expands to the exact complete value at value_pointer in this same '
            'message, authenticated by value_sha256. Read that supplied value in full for every referencing origin; '
            'this is not an external archive lookup. All provenance aliases, source scopes, opinions and responses '
            'remain distinct. Different values remain supplied. References address stable supplied values, not other '
            'replacements. Canonical evidence selection and editability still use the original captured input. '
            'Equal content does not prove semantic agreement, correction, current applicability or permission.'}


def _controller_shared_fields(payload):
    """Two repeated fields in typed plan operations, not general text compression."""
    shared = []
    def common(rows, field, pointer, expected=None):
        if not isinstance(rows, list) or len(rows) < 2 or not all(isinstance(row, dict) for row in rows): return
        value = rows[0].get(field)
        if not isinstance(value, str) or (expected is not None and value != expected): return
        if all(row.get(field) == value for row in rows):
            shared.append({'array_pointer': pointer, 'field': field, 'value': value})
    def walk(value, pointer):
        if isinstance(value, dict):
            if value.get('kind') == 'plan_task' and isinstance(value.get('args'), dict):
                original = value['args'].get('plan')
                try: plan = m.TaskPlan.model_validate(original, strict=True)
                except (ValueError, TypeError): pass
                else:
                    common(original.get('source_coverage'), 'binding', pointer + '/args/plan/source_coverage')
                    common(value.get('decisions'), 'rationale', pointer + '/decisions', plan.rationale)
            for key, item in value.items(): walk(item, pointer + '/' + key.replace('~', '~0').replace('/', '~1'))
        elif isinstance(value, list):
            for index, item in enumerate(value): walk(item, pointer + '/' + str(index))
    walk(payload, '')
    return shared


def mint(record, raw, path):
    return 'wire-' + uuid5(UUID(record['nonce']), digest(raw) + ':' + path).hex


def extract_ideas(store, capture_ref, raw):
    """Independently valid proposals from a complete, still-rejected response.

    The caller authenticates the protected response. This function never admits
    its other fields or fills missing judgments. IDs match the normal decoder.
    """
    record = store.record_get('semantic_wire_request', capture_ref.get('id'))
    if not record or record.get('canonical_schema') not in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
        _proof('Idea extraction has no Learning capture')
    schema = getattr(m, record['canonical_schema'])
    load(store, capture_ref, record['canonical_input'], schema, record['role'], record['phase'], current=False)
    if not isinstance(raw, dict): return {'status': 'partial', 'ideas': [], 'invalid_idea_indexes': ['response']}
    output = []; invalid = []
    required = record['association']['required_ideas']
    old = raw.get('existing_idea_decisions')
    grouped = record['association'].get('required_idea_groups')
    if grouped is not None and isinstance(old,list) and len(old)==len(grouped):
        by_id = {i['id']:i for i in required}
        for index,item in enumerate(old):
            try:
                decision = IdeaDecision.model_validate(item,strict=True).model_dump()
                output.extend(m.Idea.model_validate(dict(by_id[identity],**decision),strict=True).model_dump()
                              for identity in grouped[index]['members'])
            except (ValueError,TypeError):invalid.append('existing_idea_decisions/'+str(index))
    elif grouped is None and isinstance(old, list) and len(old) == len(required):
        for index, item in enumerate(old):
            try: output.append(m.Idea.model_validate(dict(required[index], **IdeaDecision.model_validate(item, strict=True).model_dump()), strict=True).model_dump())
            except (ValueError, TypeError): invalid.append('existing_idea_decisions/' + str(index))
    else: invalid.append('existing_idea_decisions')
    proposed = raw.get('new_ideas')
    if isinstance(proposed, list):
        for index, item in enumerate(proposed):
            try:
                value = NewIdea.model_validate(item, strict=True).model_dump()
                output.append(m.Idea.model_validate(dict(value, id=mint(record, raw, 'ideas/' + str(index))), strict=True).model_dump())
            except (ValueError, TypeError): invalid.append('new_ideas/' + str(index))
    else: invalid.append('new_ideas')
    return {'status': 'partial' if invalid else 'complete', 'ideas': output,
            'invalid_idea_indexes': invalid, 'semantic_wire_capture': capture_ref}


def _pointer(value, pointer):
    if not m.application_evidence_pointer(pointer): raise WireShapeError('applications.evidence.pointer', 'actual output pointer', pointer, 'foreign_selector')
    try:
        for part in pointer[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (ValueError, KeyError, TypeError, IndexError):
        raise WireShapeError('applications.evidence.pointer', 'observed pointer', pointer, 'foreign_selector') from None


def _assessment_value(record, raw, value, path, *, current_defaults=False):
    """One shared row decoder; raw and path always belong to the original send."""
    fixed = record['association']
    value = deepcopy(value)
    correction = record['canonical_input'].get('assessment_correction', {})
    retained_rows = correction.get('retained_idea_rows', [])
    if retained_rows:
        try: row_number = int(path.rsplit('/', 1)[1])
        except (ValueError, IndexError): row_number = -1
        if not 0 <= row_number < len(retained_rows):
            raise WireShapeError(path + '.ideas', 'bound correction row', row_number, 'foreign_selector')
        saved = retained_rows[row_number]
        if saved is not None:
            missing = saved['unresolved_idea_indexes']
            if len(value['ideas']) != len(missing):
                raise WireShapeError(path + '.ideas', len(missing), len(value['ideas']), 'cardinality')
            combined = [None] * saved['total_ideas']
            for item in saved['retained_ideas']:
                combined[item['index']] = deepcopy(item['value'])
            for index, idea in zip(missing, value['ideas']):
                if combined[index] is not None:
                    raise WireShapeError(path + '.ideas', 'unresolved idea position', index, 'foreign_selector')
                combined[index] = idea
            if any(item is None for item in combined):
                raise WireShapeError(path + '.ideas', 'complete original idea positions', missing, 'cardinality')
            value['ideas'] = combined
    if record.get('wire_revision') in {ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        evidence = _assessment_evidence(record['canonical_input'])
        if evidence is None: _proof('assessment source evidence differs')
        source = record['canonical_input']['result']['data']['response_analysis']['source']
        source_bytes = source['text'].encode('utf-8')
        requests = value.pop('requested_source_ranges')
        citations = value.pop('source_citations')
        def valid(item, *, supplied):
            field = (path + '.requested_source_ranges' if not supplied
                     and record.get('wire_revision') in {ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}
                     else path + '.source_citations')
            if item['source_id'] != evidence['source_id'] or item['source_sha256'] != evidence['source_sha256']:
                raise WireShapeError(field, 'captured source identity', item, 'foreign_source')
            start, end = item['start_byte'], item['end_byte']
            if not 0 <= start < end <= len(source_bytes):
                raise WireShapeError(field, 'original byte range', item, 'foreign_range')
            intervals = ([(e['start_byte'], e['end_byte']) for e in evidence['excerpts']]
                         if supplied else evidence['omitted_ranges'])
            if not any(a <= start < end <= b for a, b in intervals):
                raise WireShapeError(field,
                                     'supplied original range' if supplied else 'omitted original range',
                                     item, 'foreign_range')
            return source_bytes[start:end]
        for request in requests: valid(request, supplied=False)
        if requests:
            raise WireShapeError(path + '.requested_source_ranges', 'additional original pages required',
                                 requests, 'source_needed')
        if not citations:
            raise WireShapeError(path + '.source_citations', 'at least one supplied original range',
                                 citations, 'source_citation_missing')
        for citation in citations:
            quoted = valid(citation, supplied=True)
            try: text = quoted.decode('utf-8')
            except UnicodeDecodeError:
                raise WireShapeError(path + '.source_citations', 'UTF-8 character boundaries',
                                     citation, 'foreign_range') from None
            value['evidence_refs'].append(json.dumps({**citation, 'quoted_text': text,
                'quoted_sha256': hashlib.sha256(quoted).hexdigest()}, ensure_ascii=False, separators=(',', ':')))
    values = value.pop('thinking_values'); _count(values, fixed['thinking_targets'], path + '.thinking_values')
    if any(not v.strip() for v in values): raise WireShapeError(path + '.thinking_values', 'substantive values', values, 'empty_judgment')
    value['thinking_targets'] = dict(zip(fixed['thinking_targets'], values))
    known_ids = {digest({k:v for k,v in g['idea'].items() if k!='id'}):g['idea']['id']
                 for g in fixed.get('known_ideas', [])}
    output = []; considerations = []; selected = set()
    for index, item in enumerate(value['ideas']):
        if 'candidate_slot' not in item:
            output.append(dict(item, id=known_ids.get(digest(item), mint(record, raw, path + '/ideas/' + str(index)))))
            continue
        slot = item['candidate_slot']; known = fixed.get('known_ideas', [])
        if slot >= len(known): raise WireShapeError(path + '.candidate_slot', list(range(len(known))), slot, 'foreign_selector')
        if slot in selected: raise WireShapeError(path + '.candidate_slot', 'each selected candidate once', slot, 'duplicate_selector')
        selected.add(slot)
        if not item['consideration'].strip(): raise WireShapeError(path + '.consideration', 'current concrete consideration', item['consideration'], 'empty_judgment')
        group = known[slot]
        output.extend(dict(group['idea'], id=identity) for identity in group['members'])
        considerations.append('Candidate ' + group['idea']['id'] + ': ' + item['consideration'])
    if considerations: value['rationale'] += '\n' + '\n'.join(considerations)
    value['ideas'] = output
    if current_defaults or record.get('wire_revision') in {ASSESSMENT_DEFAULTS, REFERENCED_ASSESSMENT_DEFAULTS, ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}:
        for index, item in enumerate(output):
            try: m.idea_deferral(item)
            except m.PolicyError as error:
                raise WireShapeError(path + f'.ideas/{index}.next_operation',
                    'complete explicit optional-deferral decision', str(error), 'semantic_contract') from error
    return value


def _slot_assessment_value(record, value, path):
    """Bind v4 selectors to captured ideas and original bytes without inference."""
    slots = _assessment_source_slots(record['canonical_input'])
    if slots is None: _proof('assessment source slots differ')
    value = deepcopy(value)
    existing = value.pop('existing_idea_decisions')
    proposed = value.pop('new_ideas')
    if not existing and not proposed:
        raise WireShapeError(path + '.new_ideas', 'at least one concrete candidate', [], 'empty_judgment')
    value['ideas'] = existing + proposed
    for field, catalog, output in (
            ('source_citation_slots', slots['citation_slots'], 'source_citations'),
            ('requested_source_slots', slots['request_slots'], 'requested_source_ranges')):
        selected = value.pop(field)
        if len(set(selected)) != len(selected):
            raise WireShapeError(path + '.' + field, 'distinct page slots', selected, 'duplicate_selector')
        if any(type(index) is not int or index < 0 or index >= len(catalog) for index in selected):
            raise WireShapeError(path + '.' + field, list(range(len(catalog))), selected, 'foreign_selector')
        value[output] = [dict(source_id=slots['source_id'], source_sha256=slots['source_sha256'],
                              start_byte=catalog[index]['start_byte'], end_byte=catalog[index]['end_byte'])
                         for index in selected]
    return value


def _assessment_response_view(record, raw):
    """Lossless presentation adapter for new captures; old receipts keep old rules.

    A target/value object is only another spelling of the exact ordered
    thinking slot. Missing decisions, source selections and mixed candidate
    proposals are never supplied by this adapter.
    """
    if record.get('wire_revision') not in {ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS} or not isinstance(raw, dict):
        return raw
    view = deepcopy(raw)
    if view.get('assessments_note', object()) is None:
        view.pop('assessments_note')
    rows = view.get('assessments')
    if not isinstance(rows, list):
        return view
    expected = record['association']['thinking_targets']
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('thinking_values'), list):
            continue
        values = row['thinking_values']
        if (len(values) == len(expected) and all(isinstance(item, dict)
                and set(item) == {'target', 'value'} and item['target'] == target
                and isinstance(item['value'], str)
                for item, target in zip(values, expected))):
            row['thinking_values'] = [item['value'] for item in values]
    return view


def partial_assessments(record, raw, *, current_defaults=False, allow_unresolved_only=False,
                        allow_note_only=False):
    """Classify complete exact-position rows; never infer a missing association.

    The caller authenticates the protected response and current capture first.
    Bad outer membership or selectors cannot establish a reusable subset.
    Semantic correctness still requires the ordinary recomposed review.
    """
    original_raw = raw
    raw = _assessment_response_view(record, raw)
    fixed = record['association']; targets = fixed['target_ids']
    note_only = (allow_note_only and isinstance(raw, dict)
        and set(raw) == {'assessments', 'assessments_note'}
        and raw['assessments_note'] in ('', 'one exchange judged'))
    if (record.get('canonical_schema') != 'AssessmentBatch' or not isinstance(raw, dict)
            or (set(raw) != {'assessments'} and not note_only)
            or not isinstance(raw['assessments'], list)
            or len(raw['assessments']) != len(targets) or len(set(targets)) != len(targets)):
        return None
    rows = raw['assessments']; retained = []; unresolved = []
    revision = record.get('wire_revision')
    referenced = revision in {LEARNING_BINDINGS, REFERENCED_ASSESSMENT_DEFAULTS, ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET, ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES, ASSESSMENT_REFERENCE_SLOTS}
    if current_defaults:
        revision = REFERENCED_ASSESSMENT_DEFAULTS if referenced else ASSESSMENT_DEFAULTS
    row_schema = wire_schema('Assessment', record['canonical_input'], revision=revision)
    for index, item in enumerate(rows):
        # Examine selectors even in a row with another schema fault. An invalid
        # literal must not hide a foreign or repeated candidate association.
        selected = set()
        idea_rows = (item.get('existing_idea_decisions', []) if revision == ASSESSMENT_REFERENCE_SLOTS
                     else item.get('ideas', [])) if isinstance(item, dict) else []
        for idea in idea_rows if isinstance(idea_rows, list) else []:
            if not isinstance(idea, dict) or 'candidate_slot' not in idea: continue
            slot = idea['candidate_slot']
            if (not referenced or type(slot) is not int
                    or not 0 <= slot < len(fixed.get('known_ideas', [])) or slot in selected):
                return None
            selected.add(slot)
        try:
            value = row_schema.model_validate(item, strict=True).model_dump()
            if revision == ASSESSMENT_REFERENCE_SLOTS:
                value = _slot_assessment_value(record, value, f'assessments/{index}')
            value = m.Assessment.model_validate(_assessment_value(record, raw, value, f'assessments/{index}',
                current_defaults=current_defaults), strict=True)
        except ValidationError as error:
            diagnostic = error.errors(include_url=False, include_input=False, include_context=False)
        except WireShapeError as error:
            diagnostic = error.diagnostic
        else:
            retained.append({'index': index, 'value': {'target_id': targets[index], 'assessment': value.model_dump()}})
            continue
        unresolved_row = {'index': index, 'target_id': targets[index],
                          'raw': deepcopy(original_raw['assessments'][index]),
                          'validation': json.loads(json.dumps(diagnostic, ensure_ascii=False))}
        # A bad idea does not erase other decisions in the same target. Keep
        # them only when the remaining row really validates against this exact
        # capture; no candidate meaning or missing judgment is inferred.
        if not record['canonical_input'].get('assessment_correction') and isinstance(item, dict):
            if revision == ASSESSMENT_REFERENCE_SLOTS:
                existing, proposed = item.get('existing_idea_decisions'), item.get('new_ideas')
                idea_entries = ([(i, idea, ExistingCandidate) for i, idea in enumerate(existing)]
                    + [(len(existing) + i, idea, NewIdea) for i, idea in enumerate(proposed)]
                    if isinstance(existing, list) and isinstance(proposed, list) else [])
            else:
                ideas = item.get('ideas')
                idea_entries = ([(i, idea, ExistingCandidate if isinstance(idea, dict) and 'candidate_slot' in idea else NewIdea)
                                 for i, idea in enumerate(ideas)] if isinstance(ideas, list) else [])
            good = []; bad = []
            for idea_index, idea, model in idea_entries:
                try:
                    normalized = model.model_validate(idea, strict=True).model_dump()
                    good.append({'index': idea_index, 'value': normalized, 'kind': 'existing' if model is ExistingCandidate else 'new'})
                except (ValidationError, ValueError, TypeError):
                    bad.append(idea_index)
            if good and bad:
                try:
                    if revision == ASSESSMENT_REFERENCE_SLOTS:
                        candidate_input = {**item,
                            'existing_idea_decisions': [entry['value'] for entry in good if entry['kind'] == 'existing'],
                            'new_ideas': [entry['value'] for entry in good if entry['kind'] == 'new']}
                    else:
                        candidate_input = {**item, 'ideas': [entry['value'] for entry in good]}
                    candidate = row_schema.model_validate(candidate_input, strict=True).model_dump()
                    if revision == ASSESSMENT_REFERENCE_SLOTS:
                        candidate = _slot_assessment_value(record, candidate, f'assessments/{index}')
                    _assessment_value(record, raw, candidate, f'assessments/{index}',
                                      current_defaults=current_defaults)
                except (ValidationError, WireShapeError, m.PolicyError):
                    pass
                else:
                    unresolved_row['retained_ideas'] = [{k: entry[k] for k in ('index', 'value')} for entry in good]
                    unresolved_row['unresolved_idea_indexes'] = bad
                    unresolved_row['total_ideas'] = len(idea_entries)
        unresolved.append(unresolved_row)
    if not unresolved and not note_only: return None
    if note_only and unresolved: return None
    if not retained:
        # This narrow recovery is entered only after the authenticated call
        # repeated its defect and became held. A mixed old/new proposal is a
        # judgment still to be made, not an accepted old or new candidate.
        if not (allow_unresolved_only and len(targets) == len(unresolved) == 1
                and ('retained_ideas' in unresolved[0] or
                     isinstance(rows[0], dict) and isinstance(rows[0].get('ideas'), list)
                     and any(isinstance(idea, dict)
                         and 'candidate_slot' in idea and (set(idea) - {'candidate_slot', 'consideration'})
                         for idea in rows[0]['ideas']))):
            return None
    return {'version': 'assessment-subset-v2' if any('retained_ideas' in row for row in unresolved)
            else 'assessment-subset-v1', 'raw_sha256': digest(original_raw),
            'retained': retained, 'unresolved': unresolved,
            **({'outer_note': raw['assessments_note'],
                'assessments_sha256': digest(raw['assessments'])} if note_only else {})}


def decode(store, record, schema, raw):
    raw = _assessment_response_view(record, raw)
    name = schema.__name__; fixed = record['association']; original = record['canonical_input']
    semantic = wire_schema(name,original,revision=record.get('wire_revision')).model_validate(raw, strict=True).model_dump()
    if name == 'AssessmentBatch' and record.get('wire_revision') == ASSESSMENT_REFERENCE_SLOTS:
        semantic['assessments'] = [_slot_assessment_value(record, value, f'assessments/{index}')
                                   for index, value in enumerate(semantic['assessments'])]
    if record.get('wire_revision') == ADMISSION_REFERENCES:
        semantic = _admission_reference_values(semantic, fixed['admission_references'])
    def new(items, path, offset=0):
        known = {digest({k:v for k,v in g['idea'].items() if k!='id'}):g['idea']['id']
                 for g in fixed.get('known_ideas', [])}
        return [dict(item, id=known.get(digest(item), mint(record, raw, path + '/' + str(i + offset))))
                for i,item in enumerate(items)]
    def ideas(value):
        decisions = value.pop('existing_idea_decisions')
        if 'required_idea_groups' in fixed:
            _count(decisions, fixed['required_idea_groups'], 'existing_idea_decisions')
            by_id = {identity:d for g,d in zip(fixed['required_idea_groups'],decisions) for identity in g['members']}
            required = [dict(idea, **by_id[idea['id']]) for idea in fixed['required_ideas']]
        else:
            _count(decisions, fixed['required_ideas'], 'existing_idea_decisions')
            required = [dict(fixed['required_ideas'][i], **d) for i, d in enumerate(decisions)]
        return required + new(value.pop('new_ideas'), 'ideas')
    def applications(values):
        source = _application_source(original, store, record)
        def evidence_hash(pointer):
            if record.get('presentation_revision') != APPLICATION_CONTRACT:
                return digest(_pointer(source['result'], pointer))
            try:return digest(m.application_evidence_value(source['result'], pointer))
            except m.PolicyError as error:
                raise WireShapeError('applications.evidence.pointer', 'recorded output value from a known result', pointer, 'foreign_selector') from error
        output = []
        for application in values:
            slot = application['skill_slot']
            if slot >= len(fixed['selected_skills']): raise WireShapeError('applications.skill_slot', list(range(len(fixed['selected_skills']))), slot, 'foreign_selector')
            skill = fixed['selected_skills'][slot]
            output.append({'skill_id': skill['id'], 'skill_hash': skill['hash'],
                'procedure_clause': skill['procedure_clause'], 'procedure_sha256': skill['procedure_sha256'],
                'operation_id': source['operation']['id'], 'operation_sha256': digest(source['operation']),
                'result_sha256': digest(source['result']), 'evidence': [dict(e, sha256=evidence_hash(e['pointer'])) for e in application['evidence']]})
        return output
    if name == 'Disposition':
        if 'revision_targets' in semantic:
            targets = [_source_reference(record, store, item) for item in semantic.pop('revision_targets')]
            scope = semantic.get('revision_scope', 'governed' if semantic['verdict'] == 'revise' else 'none')
            if ((semantic['verdict'] == 'revise') != bool(targets)
                    or (scope == 'learning' and any(not item['editable'] for item in targets))
                    or (scope == 'governed' and not any(not item['editable'] for item in targets))):
                raise WireShapeError('revision_targets', 'nonempty targets owned by the selected correction route', targets, 'foreign_revision_target')
            if targets:
                semantic['rationale'] += '\nCorrection references: ' + json.dumps(targets, ensure_ascii=False, separators=(',', ':'))
        if 'revision_scope' in semantic:
            scope=semantic.pop('revision_scope')
            if (semantic['verdict']=='revise') != (scope in {'learning','governed'}):
                raise WireShapeError('revision_scope','revise requires learning or governed; proceed/hold requires none',scope,'unreachable_revision')
        values = semantic.pop('opinion_decisions'); _count(values, fixed['opinion_ids'], 'opinion_decisions')
        semantic.update(opinion_responses=[dict(v, opinion_id=fixed['opinion_ids'][i]) for i, v in enumerate(values)], web_refs=fixed['web_refs'])
    elif name == 'Assessment': semantic = _assessment_value(record, raw, semantic, 'assessment')
    elif name == 'AssessmentBatch':
        values = semantic['assessments']; _count(values, fixed['target_ids'], 'assessments')
        semantic['assessments'] = [{'target_id': fixed['target_ids'][i], 'assessment': _assessment_value(record, raw, v, f'assessments/{i}')} for i, v in enumerate(values)]
    elif name == 'SkillSelection':
        values = semantic.pop('decisions'); _count(values, fixed['skills'], 'decisions')
        selected = []; rejected = []
        for skill, value in zip(fixed['skills'], values):
            if value['choice'] == 'select':
                clause = value['procedure_clause']
                if not value['application'] or not clause or clause not in skill['content']:
                    raise WireShapeError('decisions.procedure_clause', 'selected exact clause and intended application', clause, 'foreign_selector')
                selected.append({'id': skill['id'], 'hash': skill['hash'], 'application': value['application'],
                    'reason': value['reason'], 'procedure_clause': clause, 'procedure_sha256': hashlib.sha256(clause.encode()).hexdigest()})
            else:
                if value['application'] is not None or value['procedure_clause'] is not None:
                    raise WireShapeError('decisions', 'reject has null application/clause', value, 'conflicting_choice')
                rejected.append({'id': skill['id'], 'reason': value['reason']})
        semantic.update(selected=selected, rejected=rejected)
    elif name == 'Cleanup':
        _count(semantic['decisions'], fixed['skills'], 'decisions')
        semantic['decisions'] = [dict(d, id=fixed['skills'][i]['id']) for i, d in enumerate(semantic['decisions'])]
    elif name in {'Learning', 'LearningIdeas', 'LearningApplications', 'LearningSynthesis'}:
        # Independent selectors/actions must not be hidden behind an earlier
        # outer count error. Their ordinary rejection still owns the hold.
        for update in semantic.get('skill_updates', []):
            if 'expected_hash' in update or 'merge_hashes' in update:
                raise WireShapeError('skill_updates', 'semantic update fields only', sorted(update), 'fixed_field')
            action = update.get('action')
            if action in {'improve', 'organize', 'merge', 'retire'}:
                identity = update.get('id')
                if identity not in fixed['skill_versions']:
                    raise WireShapeError('skill_updates.id', list(fixed['skill_versions']), identity, 'foreign_selector')
                update['expected_hash'] = fixed['skill_versions'][identity]
                if action == 'merge':
                    chosen = update.get('merge_ids')
                    if not isinstance(chosen, list) or any(i not in fixed['skill_versions'] for i in chosen):
                        raise WireShapeError('skill_updates.merge_ids', list(fixed['skill_versions']), chosen, 'foreign_selector')
                    update['merge_hashes'] = {i:fixed['skill_versions'][i] for i in chosen}
        if 'skill_updates' in semantic:
            try:m.validate_learning_updates(semantic['skill_updates'])
            except m.LearningContractError as error:
                raise WireShapeError('skill_updates', 'exact semantic action shape', str(error), 'update_shape') from error
        if 'applications' in semantic: semantic['applications'] = applications(semantic['applications'])
        if name == 'LearningApplications': semantic['considered_skill_ids'] = [s['id'] for s in fixed['selected_skills']]
        semantic['ideas'] = ideas(semantic)
    elif name == 'Completion':
        _count(semantic['acceptance'], fixed['acceptance'], 'acceptance')
        semantic['acceptance'] = [dict(d, criterion=fixed['acceptance'][i], independent_refs=[fixed['independent_id']]) for i, d in enumerate(semantic['acceptance'])]
    elif name == 'TaskPlan':
        sources = semantic.pop('source_decisions'); prior = semantic.pop('prior_acceptance_decisions')
        added = semantic.pop('added_acceptance'); deferred = semantic.pop('deferred_decisions')
        _count(sources, fixed['pending_ids'], 'source_decisions'); _count(prior, fixed['prior_acceptance'], 'prior_acceptance_decisions')
        _count(deferred, fixed['deferred'], 'deferred_decisions')
        dispositions = []
        for old, decision in zip(fixed['prior_acceptance'], prior):
            if decision['disposition'] == 'retain':
                if decision['criterion'] is not None: raise WireShapeError('prior_acceptance_decisions.criterion', None, decision['criterion'], 'fixed_field')
                decision['criterion'] = old['criterion']
            dispositions.append(dict(decision, index=old['index'], old_hash=old['hash']))
        dispositions.extend(dict(d, index=None, disposition='add') for d in added)
        semantic.update(source_hash=fixed['source_hash'], source_coverage=[],
            source_dispositions=[dict(d, source_id=fixed['pending_ids'][i]) for i, d in enumerate(sources)],
            acceptance_dispositions=dispositions,
            acceptance=[d['criterion'] for d in dispositions if d['disposition'] != 'withdraw'],
            deferred_index_hash=fixed['deferred_index_hash'],
            deferred_considerations=[dict(d, **fixed['deferred'][i]) for i, d in enumerate(deferred)])
    elif name == 'ContextObservation': semantic['coverage_ids'] = fixed['coverage_ids']
    elif name == 'EngineeringInvestigation':
        _count(semantic['checks'], fixed['work_items'], 'checks')
        semantic['checks'] = [dict(d, work_item=fixed['work_items'][i]) for i, d in enumerate(semantic['checks'])]
        semantic['findings'] = new(semantic['findings'], 'findings')
    elif name == 'EngineeringScenarios': semantic['scenarios'] = new(semantic['scenarios'], 'scenarios')
    elif name in {'Review', 'EngineeringAdmission'}:
        if record.get('wire_revision') in {LEARNING_BINDINGS, LEARNING_TARGETS, APPLIED_TARGETS}:
            for opinion in semantic['opinions']:
                opinion['evidence_refs'] = [json.dumps(_source_reference(record, store, item), ensure_ascii=False, separators=(',', ':'))
                                            for item in opinion['evidence_refs']]
        semantic['opinions'] = new(semantic['opinions'], 'opinions')
    elif name == 'Operation':
        if semantic['kind']=='judgment_resolve' and 'judgment_corrections' in fixed:
            args=semantic['args'];rows=fixed['judgment_corrections']
            if set(args)!={'correction_ref','evidence_refs','reason'}:
                raise WireShapeError('args','correction_ref, evidence_refs, reason',list(args),'field_set')
            if not isinstance(args['reason'],str) or not args['reason'].strip():
                raise WireShapeError('args.reason','nonempty judgment reason',args['reason'],'reason_required')
            slot=args['correction_ref']
            if type(slot) is not int or not 0<=slot<len(rows):
                raise WireShapeError('args.correction_ref',list(range(len(rows))),slot,'foreign_selector')
            source=rows[slot];evidence=args['evidence_refs']
            if (not isinstance(evidence,list) or not evidence
                    or any(type(i) is not int or not 0<=i<len(source['evidence_operation_ids']) for i in evidence)
                    or len(set(evidence))!=len(evidence)):
                raise WireShapeError('args.evidence_refs','distinct nonempty eligible_evidence indices',evidence,'foreign_selector')
            semantic['args']={'review_id':source['review_id'],'expected_hash':source['expected_hash'],
                              'reason':args['reason'],'evidence_operation_ids':[source['evidence_operation_ids'][i] for i in evidence]}
        semantic.update(id=mint(record, raw, 'operation'), decisions=new(semantic['decisions'], 'decisions'))
    return schema.model_validate(semantic, strict=True)


def _admission_reference_values(value, references, path=''):
    """Expand only the typed selections; ordinary admission validation stays shared."""
    def selected(item, kind, field):
        slot = item['reference_slot']
        if slot >= len(references) or references[slot]['kind'] != kind:
            allowed = [i for i, ref in enumerate(references) if ref['kind'] == kind]
            raise WireShapeError(field, allowed, slot, 'foreign_selector')
        return references[slot]['id']
    if isinstance(value, list):
        return [_admission_reference_values(item, references, f'{path}/{index}') for index, item in enumerate(value)]
    if not isinstance(value, dict): return value
    output = {}
    for key, item in value.items():
        field = path + '/' + key
        if key in {'source_refs', 'evidence_refs', 'addressed_findings', 'finding_ids'}:
            kind = 'evidence' if key in {'source_refs', 'evidence_refs'} else 'finding'
            output[key] = [selected(ref, kind, field) for ref in item]
        elif key == 'prerequisite_id': output[key] = selected(item, 'prerequisite', field)
        else: output[key] = _admission_reference_values(item, references, field)
    return output


def save_output(store, capture_ref, record, schema, raw, canonical, metadata):
    response = metadata.get('response_record')
    body = {'version': VERSION, 'capture': capture_ref, 'raw': deepcopy(raw), 'canonical': canonical.model_dump(),
            'response': deepcopy(response), 'task_id': record['task_id'], 'role': record['role'], 'phase': record['phase'],
            'source_hash': record['source_hash'], 'policy_hash': record['policy_hash'],
            'schema': schema.__name__, 'raw_sha256': digest(raw), 'canonical_sha256': digest(canonical.model_dump())}
    body['id'] = digest(body)
    old = store.record_get('semantic_wire_output', body['id'])
    if old is not None and old != body: _proof('output identity collision')
    store.record('semantic_wire_output', body['id'], body)
    return {'id': body['id'], 'sha256': digest(body)}


def validate_wire_receipt(store, call, event, schema, *, rejected_input=None):
    """Additional provenance gate at bounded reuse AND saved permit consumers."""
    try:return _validate_wire_receipt(store, call, event, schema, rejected_input=rejected_input)
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        _proof('wire/canonical receipt is malformed: ' + type(error).__name__)


def _validate_wire_receipt(store, call, event, schema, *, rejected_input=None):
    initial = call.get('measurement', {}).get('semantic_wire')
    detail = event.get('detail', {})
    rejected = rejected_input is not None
    if rejected and (event.get('status') != 'rejected_model_output'
            or detail.get('target_effect') != 'none; proposal not admitted'):
        _proof('rejected proposal has no exact unadmitted event')
    usage = detail.get('usage', {}) if rejected else detail.get('measurement', {}).get('usage', {})
    result = detail.get('rejected_response') if rejected else detail.get('result')
    output_ref = usage.get('semantic_wire_output')
    if initial is None:
        if rejected: _proof('rejected proposal has no captured wire request')
        if output_ref is not None: _proof('new output cannot acquire an old request identity')
        return None
    first = store.record_get('semantic_wire_request', initial.get('id'))
    if not first or first.get('model_input_sha256') != call['measurement']['model_input_sha256']:
        _proof('initial capture is absent or foreign')
    load(store, initial, first['canonical_input'], schema, first['role'], call['phase'])
    output = store.record_get('semantic_wire_output', output_ref.get('id')) if isinstance(output_ref, dict) else None
    if (not output or digest(output) != output_ref.get('sha256')
            or output['id'] != digest({k: v for k, v in output.items() if k != 'id'})):
        _proof('raw/canonical output receipt is absent or changed')
    current = store.record_get('semantic_wire_request', output['capture'].get('id'))
    if not current: _proof('final capture is absent')
    load(store, output['capture'], current['canonical_input'], schema, first['role'], call['phase'])
    if (current['task_id'] != call['task_id'] or current['source_hash'] != call['source_hash']
            or current['policy_hash'] != call['policy_hash'] or output['response'] != usage.get('response_record')
            or any(output.get(k) != current[k] for k in ('task_id', 'source_hash', 'policy_hash', 'role', 'phase'))
            or output.get('schema') != schema.__name__
            or output['raw_sha256'] != digest(output['raw']) or output['canonical_sha256'] != digest(output['canonical'])
            or decode(store, current, schema, output['raw']).model_dump() != output['canonical']
            or output['canonical'] != result
            or (not rejected and output['canonical'] != call.get('raw_result', call.get('result')))):
        _proof('raw/association/canonical result or response binding differs')
    actual = rejected_input if rejected else next((call[k].get('actual_model_input') for k in ('wire_trace', 'admission_trace', 'learning_trace', 'disposition_trace') if k in call), None)
    if actual is not None and current['canonical_input'] != actual: _proof('final captured input differs from actual correction')
    response_ref = output['response'] or {}
    response = store.record_get('model_response', response_ref.get('id')) if response_ref.get('id') else None
    if (not response or digest(response) != response_ref.get('record_sha256')
            or response.get('semantic_wire_capture') != output['capture']
            or any(response.get(k) != current[k] or response_ref.get(k) != current[k]
                   for k in ('task_id', 'role', 'phase', 'source_hash', 'policy_hash'))
            or response.get('http_status') != 200 or not response.get('request_sha256')
            or response.get('request_sha256') != response_ref.get('request_sha256')):
        _proof('actual transport response is not bound to the semantic output')
    return output_ref
