"""Lossless source dictionary; no ledger mutations in representation checks."""
from pathlib import Path
import copy
import importlib.util
import unittest

P=Path(__file__).resolve().parents[1]/'objective_ledger.py'
spec=importlib.util.spec_from_file_location('projection_runtime',P)
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class ProjectionTest(unittest.TestCase):
    def state(self):
        clauses=[dict(clause_id='C'+str(i),source_event_id='SRC1',source_ref='sources/A.txt',source_sha256='A'*64,locator='exact line '+str(i)) for i in range(52)]
        clauses.append(dict(clause_id='LAST',source_event_id='SRC2',source_ref='sources/B.txt',source_sha256='B'*64,locator='other full source'))
        return dict(contracts={'C':dict(primary_objective='Do actual outcome',authority='No target operation',clauses=clauses)},
            current_contract_id='C',identity=dict(namespace='test',logical_chat_id='own'),revision=1,head_hash='H',
            open_outcomes={'O':dict(outcome_id='O',status='OPEN',description='required'),'DONE':dict(outcome_id='DONE',status='SATISFIED')},
            unclassified_source_ids=['SRC3'],sources={'SRC3':dict(source_ref='sources/C.txt',source_sha256='C'*64)},
            unresolved_capture_gap_ids=['GAP'],capture_gaps={'GAP':dict(status='OPEN',reason_family='missing',source_ref='gap/source')},
            orphan_capture_gap_resolution_ids=[],unknown_effect_action_ids=['ACT'],actions={'ACT':dict(status='UNKNOWN',description='pending effect')},
            latest_progress=dict(contract_id='C',next_eligible_work='complete required connection'),unproven_source_ids=[],proof_ceiling='structure only')

    def test_all_bindings_locators_authority_open_unknown_and_history_retained(self):
        state=self.state();before=copy.deepcopy(state)
        rendered=r.render_projection(state)
        self.assertEqual(state,before)
        for clause in state['contracts']['C']['clauses']:
            for key in ('clause_id','locator','source_event_id','source_ref','source_sha256'):
                self.assertIn(clause[key],rendered)
        self.assertEqual(rendered.count('sources/A.txt'),1)
        self.assertIn('- C51 | S1 | locator=exact line 51',rendered)
        self.assertIn('- LAST | S2 | locator=other full source',rendered)
        for value in ['No target operation','- O | OPEN','- ACT | UNKNOWN','- GAP | OPEN','- SRC3 |','complete required connection','journal.jsonl']:
            self.assertIn(value,rendered)
        self.assertNotIn('- DONE |',rendered)

    def test_no_contract_does_not_hide_frontiers(self):
        state=self.state();state['current_contract_id']=None
        rendered=r.render_projection(state)
        self.assertIn('UNCLASSIFIED OR WITHDRAWN',rendered)
        self.assertIn('- ACT | UNKNOWN',rendered)
        self.assertIn('- SRC3 |',rendered)

if __name__=='__main__':unittest.main()
