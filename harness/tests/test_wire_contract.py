"""Wire-format acceptance must never change a judgment or weaken validation."""
import pytest
from pydantic import ValidationError
from policy_harness.models import AssessmentBatch,Completion

def assessment():
    return {'target_id':'actual-request','objective_link':'Source goal','rationale':'Not yet executed',
        'success':'unknown','mistakes':['No result observed'],'recurrence':'unknown','efficiency':'Unmeasured',
        'interactions':'No target effect','thinking_targets':{'execution':'Not dispatched'},
        'ideas':[{'id':'idea','target':'task','proposal':'Check the actual next result','disposition':'investigate','rationale':'Unknown effect is not success'}]}

def test_flat_wire_preserves_every_declared_judgment_without_fabrication():
    raw=assessment();normalized=AssessmentBatch.model_validate({'assessments':[raw]},strict=True).assessments[0]
    assert normalized.target_id==raw['target_id']
    assert normalized.assessment.model_dump(exclude_unset=True)=={k:v for k,v in raw.items() if k!='target_id'}
    assert normalized.assessment.success=='unknown' and normalized.assessment.ideas[0].disposition=='investigate'

@pytest.mark.parametrize('change',[{'authorization':'bypass'},{'ideas':[]},{'success':True}])
def test_flat_wire_does_not_accept_extra_authority_empty_thinking_or_wrong_outcome(change):
    with pytest.raises(ValidationError):AssessmentBatch.model_validate({'assessments':[{**assessment(),**change}]},strict=True)

def test_string_false_stays_invalid_and_boolean_false_stays_false():
    value={'achieved':False,'acceptance':[],'evidence_refs':[],'unresolved':['No result'],'summary':'Unfinished'}
    assert Completion.model_validate(value,strict=True).achieved is False
    with pytest.raises(ValidationError):Completion.model_validate(dict(value,achieved='false'),strict=True)
