"""Small semantic responses. Models never manufacture controller identities."""
from typing import Any, Literal
from pydantic import Field, ValidationError, model_validator
from .models import StrictModel


class ToolRequest(StrictModel):
    name: Literal['file_read', 'file_write', 'file_list', 'run_python', 'run_tests', 'run_command',
                  'web_fetch', 'history_read', 'knowledge_read', 'attachment_read', 'harness_info', 'harness_read', 'harness_propose', 'harness_verify', 'harness_apply', 'harness_rollback']
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1)


class CriterionEvidence(StrictModel):
    criterion: int = Field(ge=0)
    evidence: str = Field(min_length=1)


class LearningNote(StrictModel):
    action: Literal['create', 'improve', 'merge', 'retire', 'reject', 'defer']
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    operation_indices: list[int] = Field(default_factory=list, max_length=12)
    skill: int | None = Field(default=None, ge=0)
    merge_skills: list[int] = Field(default_factory=list, max_length=8)
    applies_when: str = Field(default='', max_length=1200)
    procedure: str = Field(default='', max_length=4000)
    limits: str = Field(default='', max_length=1200)
    next_trigger: str = Field(default='', max_length=1200)
    share_scope: Literal['local', 'folder', 'general'] = 'local'
    sharing_reason: str = Field(default='', max_length=800)


class ContextSummary(StrictModel):
    summary: str = Field(min_length=1, max_length=18000)
    next_action: str = Field(min_length=1, max_length=2000)
    evidence_indices: list[int] = Field(min_length=1, max_length=128)


class SkillUse(StrictModel):
    skill: int | None = Field(default=None, ge=0)
    new_lesson: int | None = Field(default=None, ge=0)
    tool_index: int = Field(ge=0)
    adaptation: str = Field(min_length=1, max_length=1200)

    @model_validator(mode='after')
    def one_source(self):
        if (self.skill is None) == (self.new_lesson is None):
            raise ValueError('Select either skills[index] or this response learning[new_lesson]')
        return self


class LearningAssessment(StrictModel):
    operation_index: int = Field(ge=0)
    judgment: Literal['helpful', 'no_change', 'harmful', 'inconclusive']
    reason: str = Field(min_length=1, max_length=1600)


class PracticalStep(StrictModel):
    action: Literal['tools', 'complete', 'blocked']
    tools: list[ToolRequest] = Field(default_factory=list, max_length=16)
    message: str = Field(min_length=1)
    acceptance: list[CriterionEvidence] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    learning: list[LearningNote] = Field(default_factory=list, max_length=8)
    skill_uses: list[SkillUse] = Field(default_factory=list, max_length=16)
    learning_assessments: list[LearningAssessment] = Field(default_factory=list, max_length=16)
    update_assessments: list[LearningAssessment] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def consistent(self):
        if (self.action == 'tools') != bool(self.tools):
            raise ValueError('tools action requires tools; complete/blocked cannot execute tools')
        if any(tool.name in {'harness_apply', 'harness_rollback'} for tool in self.tools) and len(self.tools) != 1:
            raise ValueError('harness_apply/harness_rollback requires its own single-tool step because it restarts the process')
        return self


def normalize_practical_step(value):
    """Reuse an explicit single-operation message, without inventing intent.

    This only relocates model-authored text. Multiple operations, empty purpose,
    malformed arguments and absent messages still require model correction.
    """
    if (isinstance(value, dict) and value.get('action') == 'tools'
            and isinstance(value.get('tools'), list) and len(value['tools']) == 1
            and isinstance(value['tools'][0], dict) and 'purpose' not in value['tools'][0]
            and isinstance(value.get('message'), str) and value['message'].strip()):
        return {**value, 'tools': [{**value['tools'][0], 'purpose': value['message']}]}, {
            'field': 'tools.0.purpose', 'source': 'message',
            'reason': 'Single operation uses the explicit message from the same original response; arguments and authority unchanged.'}
    return value, None


def parse_practical_step(value, *, strict=True):
    """Defer malformed optional learning without relaxing the action schema.

    The gateway retains the original response. Rejected metadata cannot become
    adopted knowledge or count as a use, and never authorizes an operation.
    """
    try:
        return PracticalStep.model_validate(value, strict=strict), None
    except ValidationError as error:
        optional = {'learning', 'skill_uses', 'learning_assessments', 'update_assessments'}
        errors = error.errors(include_url=False, include_input=False, include_context=False)
        if not isinstance(value, dict) or any(not e['loc'] or e['loc'][0] not in optional for e in errors):
            raise
        core = {k: v for k, v in value.items() if k not in optional}
        answer = PracticalStep.model_validate(core, strict=strict)
        return answer, {'proposal': {k: value[k] for k in optional if k in value},
                        'reason': '学習メモの形式を確認できなかったため、知識には採用していません。',
                        'diagnostic': errors}


class SourceChoice(StrictModel):
    source: int = Field(ge=0)
    classification: Literal['add', 'clarify', 'correct', 'replace', 'withdraw']
    reason: str = Field(min_length=1)


class AcceptanceChoice(StrictModel):
    original: int | None
    disposition: Literal['retain', 'replace', 'withdraw', 'add']
    criterion: str
    sources: list[int] = Field(default_factory=list)
    quote: str = ''
    reason: str = Field(min_length=1)


class PracticalSourcePlan(StrictModel):
    objective: str = Field(min_length=1)
    sources: list[SourceChoice]
    criteria: list[AcceptanceChoice]


class PracticalReview(StrictModel):
    verdict: Literal['accept', 'revise']
    findings: list[str] = Field(default_factory=list, description='Concrete mandatory corrections needed to meet the original request, quality or safety requirements. When accepting, return an empty list. Put supporting evidence and optional suggestions in rationale.')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def findings_required(self):
        if self.verdict == 'revise' and not self.findings:
            raise ValueError('revise requires concrete mandatory findings')
        return self


class FindingDisposition(StrictModel):
    finding: int = Field(ge=0)
    decision: Literal['accept', 'reject']
    reason: str = Field(min_length=1, max_length=2000)


class PracticalDisposition(StrictModel):
    findings: list[FindingDisposition]
    rationale: str = Field(min_length=1, max_length=4000)
