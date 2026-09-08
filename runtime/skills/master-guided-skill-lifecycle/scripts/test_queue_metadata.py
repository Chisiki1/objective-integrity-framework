"""Existing queue consumer: current correction without fabricated promotion."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import learning_queue as queue
from test_learning_queue import base_event

class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'queue.db'
        queue.init_db(self.db)
        self.event=base_event()
        queue.upsert_candidate(self.db,self.event)

    def amend(self):
        event=copy.deepcopy(self.event)
        event.update(event_id='amend-1',expected_revision=1,change_kind='metadata',
                     reason='evidence://corrected-consumer',next_consumer_ref='consumer://actual-next',
                     next_use_trigger='trigger://actual-next')
        event['evidence_refs'].append('evidence://corrected-consumer')
        return event

    def test_current_replaced_history_and_idempotence_preserved(self):
        change=self.amend()
        result=queue.upsert_candidate(self.db,change)
        self.assertEqual(result,queue.upsert_candidate(self.db,change))
        current=queue.get_status(self.db,'candidate-1')
        self.assertEqual(current['revision'],2)
        self.assertEqual(current['state'],'discovered')
        self.assertEqual(current['payload']['next_consumer_ref'],'consumer://actual-next')
        history=queue.get_history(self.db,'candidate-1')
        self.assertIn('event-1',json.dumps(history))
        self.assertIn('amend-1',json.dumps(history))

    def test_rejects_transition_new_identity_proof_loss_and_stale(self):
        variants=[{'state':'deferred','reason':'evidence://why','return_trigger':'trigger://later'},
                  {'candidate_id':'new'}, {'owner_ref':'owner://other'},
                  {'artifact_ref':{'path':str(self.db),'sha256':'A'*64}},
                  {'evidence_refs':['evidence://new-only']}, {'expected_revision':0}]
        for changes in variants:
            with self.subTest(changes=changes):
                event=self.amend();event.update(changes)
                with self.assertRaises(queue.QueueError):queue.upsert_candidate(self.db,event)
                self.assertEqual(queue.get_status(self.db,'candidate-1')['revision'],1)

    def test_old_same_state_remains_rejected(self):
        event=self.amend();event.pop('change_kind')
        with self.assertRaises(queue.QueueError):queue.upsert_candidate(self.db,event)

if __name__=='__main__':unittest.main()
