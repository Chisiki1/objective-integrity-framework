from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .models import PolicyError
from .store import canonical, digest, redact


class PolicyCatalog:
    def __init__(self, policy_path: Path, *, include_role_supplement=True):
        self.path=Path(policy_path).resolve(strict=True)
        self.raw=self.path.read_bytes()
        self.hash=hashlib.sha256(self.raw).hexdigest()
        self.document=json.loads(self.raw)
        self.conditions={row['id']:row for row in self.document['conditions']}
        self.rules={row['id']:row for row in self.document['rules']}
        if self.document.get('schema') != 'oif-harness-policy-v1' or len(self.conditions) != 236 or set(self.rules) != {f'R{x:02d}' for x in range(1, 19)}:
            raise PolicyError('Public policy inventory differs')
        self.supplement_path=self.path.parent/'answer-Q18.json'
        self.supplement_raw=None
        self.supplement=None
        self.pre_role_supplement_hash=self.hash
        self.role_supplement_path=self.path.parent/'approved-U34.json'
        self.role_supplement_raw=None
        self.role_supplement=None
        self.role_scoped_enabled=True
        self.base_effective_hash=self.hash
        self.overlays=[]
        self._prompt=None

    def verify_current(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest()!=hashlib.sha256(self.raw).hexdigest():
            raise PolicyError('Policy source changed during execution; reconcile exact version before continuing')
        if self.supplement_raw is not None and self.supplement_path.read_bytes()!=self.supplement_raw:
            raise PolicyError('Approved policy supplement changed during execution')
        if self.role_supplement_raw is not None and self.role_supplement_path.read_bytes()!=self.role_supplement_raw:
            raise PolicyError('Approved role responsibility supplement changed during execution')

    def _fineprint(self):
        """Remove only byte-identical contained source excerpts, not obligations.

        All source-unit IDs retain a route to an included complete containing text.
        Historic clauses remain subordinate to their explicit current replacements.
        """
        blocks=[]
        origins=[]
        referenced={identity for condition in self.conditions.values() for identity in condition['refs']}
        for identity,text in sorted(self.document['historical_source_unit_text'].items(),key=lambda x:len(x[1]),reverse=True):
            if identity not in referenced:
                continue  # Unmapped archive context remains in the full JSON source.
            found=next((i for i,(_,body) in enumerate(blocks) if text in body),None)
            if found is None:
                blocks.append((identity,text));origins.append([identity])
            else:
                origins[found].append(identity)
        return '\n\n'.join(f'SOURCE_UNITS {",".join(ids)}\n{text}' for (_,text),ids in zip(blocks,origins))

    def prompt(self):
        self.verify_current()
        if self._prompt is None:
            current='\n'.join(f"{x['id']} {x['title']}\n{x['body']}\nsource refs: {','.join(x['refs'])}" for x in self.conditions.values())
            rules='\n\n'.join(f"{x['id']} {x['title']}\n{x['text']}" for x in self.rules.values())
            fine=self._fineprint()
            self._prompt=redact(
                '完全作業方針。以下の現行条文と承認済み全回答が必須。原文の未変更細則も維持する。'
                '歴史資料の置換済み指示は現行条文を上書きしない。P節の別タスクの権限は現在の権限ではない。'
                'モデル内部の私的思考の開示は要求しない。出力する判断・理由・証拠を審査する。'
                f'\nPOLICY_SHA256 {self.hash}\nCURRENT ANSWER RULES\n{rules}\nCURRENT CONDITIONS\n{current}'
                f'\nSOURCE FINE PRINT (superseded portions are history, not parallel current authority)\n{fine}')
            if self.role_supplement:
                self._prompt+=('\nLATEST APPROVED CORRECTION U34 (takes precedence only over conflicting clauses above):\n'
                    +self.role_supplement['approved_text'])
        return self._prompt

    def phase_prompt(self, role, phase, schema_name, payload):
        """Exact applicable clauses for one judgment; the root parent owns the full cut.

        This replaces common blanket injection. It never removes a controller gate,
        adopts evidence, or declares semantic compliance. Original task/operation/
        evidence inputs remain available to the responsible judgment and reviewer.
        """
        self.verify_current()
        if not self.role_scoped_enabled:
            return self.prompt()
        context=payload.get('role_context',{})
        root=context.get('parent_task_id') is None and not payload.get('delegation_lease')
        if role=='parent' and root and schema_name in {'TaskPlan','Operation','Completion'}:
            return self.prompt()
        # Scope follows the existing phase contracts, not a model's self-declared
        # applicability. Full cross-stage coverage remains with the coordinating
        # parent; missing target evidence must be reported, never invented.
        sections=set()
        ids={'A-04','A-05','A-09','A-10','B-02','B-03','B-08','B-09','I-06','O-03','O-05','O-09'}
        if schema_name in {'TaskPlan','Operation','Completion'}:
            sections.update('BCDIJLMO')
        elif schema_name.startswith('Engineering'):
            sections.update('BKL')
        elif schema_name=='SkillSelection':
            ids.update({'F-01','F-02','F-03','F-04','F-05','F-06','F-08','F-13','F-16'})
        elif schema_name=='Cleanup':
            ids.update({'E-14','E-15','E-17','F-07','F-12','F-13','F-14','H-02','H-13','H-14'})
        elif schema_name in {'Learning','LearningIdeas','LearningApplications','LearningSynthesis','IdeaResolution'}:
            sections.update('EF')
            ids.update({'G-01','G-03','G-04','G-05','G-06','G-17','H-02','H-06','H-09','H-11','H-13','H-14'})
        elif schema_name in {'Assessment','AssessmentBatch','Review','Disposition'}:
            sections.update('CDE')
            ids.update({'K-03','K-04','K-08','K-11','K-16','K-17','K-19','K-20','K-23'})
            bundle=payload.get('bundle',{})
            has_learning=any(key in value for value in (payload,bundle) if isinstance(value,dict)
                for key in ('learning','required_ideas','application_contract','learning_projection'))
            # Split post-review/disposition pages keep the canonical phase even
            # when the Learning body is delivered as source fragments.
            if 'learning' in phase or has_learning or phase.startswith(('post_review','post_disposition')):
                sections.update('FGH')
            if 'completion' in phase or 'final' in phase or phase.startswith('independent_refutation'):
                sections.update('BFHJO')
            if 'cleanup' in phase:sections.update('FGH')
        elif schema_name=='ResearchQuery' or role=='web':
            sections.update('D')
        elif schema_name=='ContextObservation':
            ids.update({'D-06','D-11','H-13','I-09','K-09','K-10'})
        else:
            raise PolicyError('No applicable phase policy contract for '+schema_name)
        operation=payload.get('operation',payload.get('parent_operation',{}))
        kind=operation.get('kind') if isinstance(operation,dict) else None
        if kind in {'policy_amendment','adopt_policy','prepare_update','verify_update','activate_update','rollback_update'}:
            sections.update('N')
        if kind=='finish':sections.update('BFHJO')
        if kind in {'delegate','child_integrate','child_scope','child_reconcile'}:sections.update('J')
        selected=[row for row in self.conditions.values() if row['id'] in ids or row['section'] in sections]
        rules={'R01','R03','R12','R16','R18'}
        rules.update(rule for row in selected for rule in row.get('current_rule_refs',[]))
        clauses='\n\n'.join(f"{row['id']} {row['title']}\n{row['body']}" for row in selected)
        answers='\n\n'.join(f"{row['id']} {row['title']}\n{row['text']}" for row in self.rules.values() if row['id'] in rules)
        return redact('ROLE-SCOPED PUBLIC POLICY\n'
            'The coordinating parent reads and integrates the complete policy. You judge only this phase from its target, '
            'applicable exact clauses and supplied original evidence. Code owns identity, state, permissions and progression. '
            'Do not reconstruct the whole history, create another manager, or demand whole-policy reading by phase workers. '
            'Return the requested judgment, reasons and uncertainties; request the specific missing original evidence through '
            'the existing uncertainty/opinion/needs_evidence fields. Finish after this response; it does not execute or complete the task. '
            'Independent review must assess originals and conditions, not merely agree with the parent.\n'
            'PROPORTIONAL WORK: Considering an improvement does not adopt its implementation. Reasonedly reject/defer optional '
            'improvements unnecessary to current mandatory completion. Auxiliary Skill updates cannot become new prerequisites '
            'delaying the deliverable; this supersedes legacy automatic auxiliary-Skill creation requirements. '
            'Preserve required thought, review, correction and verified use of actually adopted changes. '
            'Never defer a mandatory user outcome or erase unknown effects. R03/R16/R18 endpoints apply; no review of private '
            'reviewer thought or recursive Web just to inspect this same collection.\n'
            +answers+'\nAPPLICABLE CURRENT CONDITIONS\n'+clauses)

    def summary(self):
        return {'version':self.document['version'],'hash':self.hash,'condition_count':len(self.conditions),
                'rule_count':len(self.rules),'source_units':len(self.document['source_units']),
                'conditions':list(self.conditions.values()),'rules':list(self.rules.values()),
                'prompt_source_units':len({x for c in self.conditions.values() for x in c['refs']}),
                'limits':self.document['limits'],'amendments':self.overlays,'approved_Q18':self.supplement,
                'approved_U34':self.role_supplement,
                'runtime_claim':'工程の制御と意味・成果の検証を区別する。全判断の無謬性は保証しない。'}

    def amendment(self, proposal):
        required={'condition_id','old_text','new_text','reason','impact','recovery'}
        if set(proposal)!=required or not all(isinstance(v,str) and v.strip() for v in proposal.values()):
            raise PolicyError('Policy change needs exact old/new text, reason, impact and recovery')
        row=self.conditions.get(proposal['condition_id'])
        if row is None or row['body']!=proposal['old_text']:
            raise PolicyError('Policy amendment old text differs from effective condition')
        return dict(proposal,old_policy_hash=self.hash,proposal_hash=digest({'policy_hash':self.hash,**proposal}))

    def apply_amendment(self, approved):
        """Controller calls only after exact UI approval and independent review."""
        proposal={k:approved[k] for k in ('condition_id','old_text','new_text','reason','impact','recovery')}
        checked=self.amendment(proposal)
        if checked['proposal_hash']!=approved['proposal_hash'] or not approved.get('user_approved'):
            raise PolicyError('Exact user approval is absent or stale')
        self.conditions[proposal['condition_id']]=dict(self.conditions[proposal['condition_id']],body=proposal['new_text'])
        # Only the exact immutable approved delta defines the policy version.
        # UI delivery/status timestamps must not change the version on reload.
        self.overlays.append(deepcopy({**checked,'user_approved':True}))
        self.hash=digest({'baseline':self.base_effective_hash,'amendments':self.overlays})
        self._prompt=None
