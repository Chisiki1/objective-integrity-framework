"""Whole disposition and real history readback, including rejection paths."""
from pathlib import Path
import base64
import copy
import json
import runpy
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/master_reconcile.py'
m=runpy.run_path(str(SCRIPT)); mi=m['mi']


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.master=self.root/'master.md';self.history=self.root/'history.md'
        self.prior=self.root/'prior.md';self.prior.write_bytes(b'# Prior\n\nUnresolved X\n')
        marker=b'<!-- master-history-v1 '+mi.canonical({'path':str(self.prior),'sha256':mi.sha(self.prior.read_bytes()),'bytes':len(self.prior.read_bytes())})+b' -->\n'
        self.master.write_bytes(b'# Master\n\n## CURRENT CONTROL\n\n### Work\nOld next work; O1 open.\n\n## History\nIncident A\n'+marker)
        self.body=self.root/'body.md';self.body.write_bytes(b'# Master\n\n## CURRENT CONTROL\n\n### Work\nRead the owner source for O1 and next work.\n\n### Historical frontier\nOld unresolved state is retained, not closed.\n')
        self.source=self.root/'source.txt';self.source.write_bytes(b'Organize this master preserving O1, authority and history.\n')
        def ref(path):return {'path':str(path),'sha256':mi.sha(path.read_bytes())}
        self.ref=ref
        inv=m['inventory'](str(self.master)); groups=[]
        for s in inv['sections']:
            keep=s['level']==1
            groups.append({'section_ids':[s['section_id']],'disposition':'keep' if keep else 'replace' if s['current_control_region'] else 'archive',
                'reason':'Exact unchanged heading' if keep else 'Preserve old pending state and replace duplicate current value with source routing.',
                'target_heading':None if keep else 'Work' if s['current_control_region'] else 'Historical frontier',
                'evidence_refs':[] if keep else [ref(self.source)]})
        self.plan={'schema':m['SCHEMA'],'owner':'test-owner','master_ref':ref(self.master),'replacement_ref':ref(self.body),'history_path':str(self.history),'source_refs':[ref(self.source)],'groups':groups}

    def tearDown(self):self.tmp.cleanup()

    def test_exact_publication_and_transitive_readback(self):
        old=self.master.read_bytes();p=m['propose'](self.plan)
        self.assertFalse(self.history.exists());self.assertEqual(old,self.master.read_bytes())
        self.assertFalse(p['permission_granted']);self.assertFalse(p['publication_observed'])
        self.history.write_bytes(base64.b64decode(p['backing_base64']))
        self.master.write_bytes(base64.b64decode(p['replacement_base64']))
        self.assertEqual(self.history.read_bytes(),old)
        r=m['readback'](p);self.assertTrue(r['exact_publication_observed']);self.assertEqual(r['prior_sources_verified'],2)
        self.assertEqual(len(mi.history_markers(self.master.read_bytes())),1)
        self.prior.write_bytes(b'changed')
        with self.assertRaises(ValueError):m['readback'](p)

    def test_partial_scope_cannot_claim_reconciliation(self):
        self.plan['groups'].pop()
        with self.assertRaisesRegex(ValueError,'undisposed'):m['propose'](self.plan)

    def test_duplicate_disposition_and_bool_id_rejected(self):
        for ids in ([1,1],[True]):
            p=copy.deepcopy(self.plan);p['groups'][1]['section_ids']=ids
            with self.assertRaises(ValueError):m['propose'](p)

    def test_current_needs_evidence_and_unique_live_target(self):
        for key,value in [('evidence_refs',[]),('target_heading',None),('target_heading','absent')]:
            p=copy.deepcopy(self.plan);p['groups'][1][key]=value
            with self.assertRaises(ValueError):m['propose'](p)
        self.body.write_bytes(self.body.read_bytes()+b'\n## Duplicate\n\n### Work\nnot current\n')
        self.plan['replacement_ref']=self.ref(self.body)
        with self.assertRaises(ValueError):m['propose'](self.plan)

    def test_changed_source_and_conflicting_history_preserved(self):
        self.source.write_bytes(b'new authority')
        with self.assertRaisesRegex(ValueError,'STALE_SOURCE'):m['propose'](self.plan)
        self.plan['source_refs']=[self.ref(self.source)]
        for g in self.plan['groups']:
            if g['evidence_refs']:g['evidence_refs']=[self.ref(self.source)]
        self.history.write_bytes(b'other owner')
        with self.assertRaisesRegex(ValueError,'different bytes'):m['propose'](self.plan)
        self.assertEqual(self.history.read_bytes(),b'other owner')

    def test_kept_bytes_and_parent_meaning_cannot_disappear(self):
        p=copy.deepcopy(self.plan);p['groups'][2]['disposition']='keep'
        with self.assertRaisesRegex(ValueError,'kept section'):m['propose'](p)
        old=self.master.read_bytes();replacement=old.replace(b'## CURRENT CONTROL',b'## CURRENT CONTROL updated')
        # Keep child exact, but explicitly replace parent: this needs its own
        # child disposition even when its bytes still appear.
        replacement=replacement[:replacement.index(b'<!-- master-history-v1')]
        self.body.write_bytes(replacement);p['replacement_ref']=self.ref(self.body)
        p['groups'][1]['target_heading']='Work'
        p['groups'][3]['target_heading']='History'
        with self.assertRaisesRegex(ValueError,'changed parent'):m['propose'](p)

    def keep_plan(self, old, new):
        self.master.write_bytes(old);self.body.write_bytes(new)
        p=copy.deepcopy(self.plan)
        p.update(master_ref=self.ref(self.master),replacement_ref=self.ref(self.body))
        p['groups']=[dict(section_ids=[s['section_id']],disposition='keep',reason='Retain exact occurrence and owner.',target_heading=None,evidence_refs=[]) for s in m['inventory'](str(self.master))['sections']]
        return p

    def test_unchanged_keep_and_explicit_owner_relocation(self):
        old=b'# Master\n\n## Owner A\n\n### Rule\nOpen X.\n\n## Owner B\n'
        moved=b'# Master\n\n## Owner A\n\n## Owner B\n### Rule\nOpen X.\n\n'
        p=self.keep_plan(old,old)
        self.assertFalse(m['propose'](p)['writes_performed'])
        # Retain both parent section bytes as well as the child bytes.
        moved=b'# Master\n\n## Owner A\n\n## Owner B\n### Rule\nOpen X.\n\n'
        p=self.keep_plan(old,moved)
        with self.assertRaisesRegex(ValueError,'ancestry'):m['propose'](p)
        p['groups'][2].update(disposition='replace',target_heading='Rule',evidence_refs=[self.ref(self.source)])
        self.assertFalse(m['propose'](p)['permission_granted'])

    def test_keep_cannot_turn_current_into_history(self):
        old=b'# Master\n\n## CURRENT CONTROL\n\n### Rule\nOpen X.\n\n## History\n'
        moved=b'# Master\n\n## CURRENT CONTROL\n\n## History\n### Rule\nOpen X.\n\n'
        p=self.keep_plan(old,moved)
        with self.assertRaisesRegex(ValueError,'ancestry|current role'):m['propose'](p)
        p['groups'][2].update(disposition='archive',target_heading='Rule',evidence_refs=[self.ref(self.source)])
        self.assertEqual(m['propose'](p)['changed_current_sections'],1)

    def test_duplicate_kept_occurrence_cannot_collapse(self):
        old=b'# Master\n\n## Rule\nOpen X.\n\n## Rule\nOpen X.\n\n'
        p=self.keep_plan(old,b'# Master\n\n## Rule\nOpen X.\n\n')
        with self.assertRaisesRegex(ValueError,'occurrence'):m['propose'](p)

    def test_fenced_copy_is_not_a_kept_section(self):
        old=b'# Master\n\n## Rule\nOpen X.\n\n'
        p=self.keep_plan(old,b'# Master\n\n## Archive\n```markdown\n## Rule\nOpen X.\n\n```\n')
        with self.assertRaisesRegex(ValueError,'occurrence'):m['propose'](p)

    def test_replacement_cannot_add_or_duplicate_history_edges(self):
        self.body.write_bytes(self.body.read_bytes()+b'<!-- master-history-v1 '+mi.canonical({'path':str(self.prior),'sha256':mi.sha(self.prior.read_bytes()),'bytes':len(self.prior.read_bytes())})+b' -->\n')
        self.plan['replacement_ref']=self.ref(self.body)
        with self.assertRaisesRegex(ValueError,'duplicate history'):m['propose'](self.plan)

    def test_bom_crlf_unicode_and_missing_source(self):
        self.body.write_bytes(b'\xef\xbb\xbf'+self.body.read_bytes().replace(b'\n',b'\r\n')+'\n\u6ce8\u8a18: \u672a\u89e3\u6c7a\n'.encode())
        self.plan['replacement_ref']=self.ref(self.body)
        # The declared kept original heading bytes are not silently normalized.
        with self.assertRaisesRegex(ValueError,'kept section'):m['propose'](self.plan)
        self.source.unlink()
        with self.assertRaises((OSError,ValueError)):m['propose'](self.plan)


if __name__=='__main__':unittest.main()
