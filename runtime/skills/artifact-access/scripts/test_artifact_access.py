import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import artifact_access as a


class Access(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name)
        self.root=self.r/'tree';self.root.mkdir();(self.root/'one.txt').write_bytes(b'one\ntwo\n')
        self.index=self.r/'index.json';a.inventory(self.root,self.index)
    def tearDown(self):self.t.cleanup()
    def test_exact_existing_manifest_and_eof(self):
        first=a.read_member(self.index,'one.txt',lines=1)
        self.assertEqual(first['text'],'one\n');self.assertEqual(first['next_line'],2)
        self.assertFalse(first['complete_file_in_this_view'])
        last=a.read_member(self.index,'one.txt',start=2);self.assertIsNone(last['next_line'])
        self.assertTrue(a.read_member(self.index,'one.txt')['complete_file_in_this_view'])
    def test_missing_has_no_version_fallback(self):
        with self.assertRaises(FileNotFoundError):a.read_member(self.index,'absent.txt')
    def test_stale_and_alias_rejected(self):
        (self.root/'one.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'STALE_MEMBER'):a.read_member(self.index,'one.txt')
        data=json.loads(self.index.read_bytes());data['files'].append({**data['files'][0],'path':'ONE.txt'})
        self.index.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'case-alias'):a.read_member(self.index,'one.txt')
    def test_no_escape_or_inventory_overwrite(self):
        for path in ['../one.txt','/one.txt','a/../one.txt','a//one.txt','one.txt:stream']:
            with self.assertRaises(ValueError):a.read_member(self.index,path)
        with self.assertRaises(ValueError):a.inventory(self.root,self.index)
        with self.assertRaises(ValueError):a.inventory(self.root,self.root/'new.json')
    def test_json_projection_replaces_single_line_dump(self):
        p=self.r/'large.json';p.write_text(json.dumps({'current':{'a':1},'history':'x'*200000}))
        self.assertEqual(a.json_view(p,'/current',True)['value'],['a'])
        self.assertEqual(a.json_view(p,'/current/a')['value'],1)
        with self.assertRaises(ValueError):a.json_view(p)
        with self.assertRaises(KeyError):a.json_view(p,'/absent')
        with self.assertRaises(ValueError):a.json_view(p,'/current/~2')
    def test_duplicate_json_and_nonfinite_rejected(self):
        for raw in ['{"a":1,"a":2}','{"a":NaN}']:
            with self.assertRaises(ValueError):a.parse(raw)
    def test_diff_is_bounded_and_source_preserved(self):
        other=self.r/'other';other.write_text('different\n')
        result=a.difference(self.root/'one.txt',other,1)
        self.assertTrue(result['truncated']);self.assertEqual((self.root/'one.txt').read_text(),'one\ntwo\n')
    def test_crlf_preserved(self):
        (self.root/'one.txt').write_bytes(b'one\r\ntwo\r\n')
        index=self.r/'crlf.json';a.inventory(self.root,index)
        self.assertEqual(a.read_member(index,'one.txt',lines=1)['text'],'one\r\n')
    def test_invalid_shape_cli_retains_typed_failure(self):
        original=json.loads(self.index.read_bytes())
        cases=[[],None,1,'scalar',{'files':None},{'files':[None]}]
        for bad in [None,1,[],{},'z'*64]:
            cases.append({**original,'files':[{'path':'one.txt','sha256':bad}]})
        for number,value in enumerate(cases):
            with self.subTest(value=value):
                path=self.r/f'bad-{number}.json';path.write_text(json.dumps(value))
                cp=subprocess.run([sys.executable,'-B',a.__file__,'read','--manifest',str(path),'--root',str(self.root),'--path','one.txt'],capture_output=True)
                self.assertEqual(cp.returncode,2);out=json.loads(cp.stdout)
                self.assertEqual(out['schema'],'artifact-access-result-v1');self.assertEqual(out['status'],'failed')
                self.assertFalse(out['fallback_used']);self.assertEqual(cp.stderr,b'')


if __name__=='__main__':unittest.main()
