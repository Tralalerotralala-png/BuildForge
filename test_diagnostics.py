import json,unittest
from diagnostics import analyze,target_lines

class DiagnosticTests(unittest.TestCase):
    def test_slow_selection(self):
        events=[{'event':'unit','name':'fast.cpp','state':'compiled','seconds':10},{'event':'progress','done':1,'total':3,'active':2,'active_units':[{'name':'slow.cpp','seconds':1800,'cpu_seconds':1700},{'name':'quick.cpp','seconds':20}]}]
        r=analyze(map(json.dumps,events),900);self.assertEqual(target_lines(r),'slow.cpp | 0 | timing');self.assertEqual(r['candidates'][0]['relative_to_median'],180)
    def test_completed_removed(self):
        r=analyze(map(json.dumps,[{'event':'progress','active_units':[{'name':'a.cpp','seconds':1000}]},{'event':'unit','name':'a.cpp','state':'compiled'}]));self.assertEqual(r['candidates'],[])
    def test_invalid_names_and_o0(self):
        r=analyze(['not JSON',json.dumps({'event':'progress','active_units':[{'name':'../x.cpp','seconds':1000},{'name':'good.cpp','seconds':1000}]})],900,'0');self.assertEqual(target_lines(r),'good.cpp | same | timing');self.assertEqual(r['malformed_lines'],1)

    def test_legacy_exact_tail_inference(self):
        e=[{'event':'unit','name':'a.cpp','state':'cached'},{'event':'progress','done':1,'total':3,'active':2,'queued':0,'seconds':10},{'event':'progress','done':1,'total':3,'active':2,'queued':0,'seconds':1010}]
        r=analyze(map(json.dumps,e),900,expected_names=['a.cpp','b.cpp','c.cpp']);self.assertEqual(target_lines(r),'b.cpp | 0 | timing\nc.cpp | 0 | timing');self.assertIn('lower bound',r['candidates'][0]['evidence'])
        self.assertEqual(analyze(map(json.dumps,e),900)['candidates'],[])
    def test_legacy_inference_refuses_inconsistent_count(self):
        e=[{'event':'unit','name':'a.cpp','state':'cached'},{'event':'progress','done':2,'total':3,'active':1,'queued':0,'seconds':10},{'event':'progress','done':2,'total':3,'active':1,'queued':0,'seconds':1010}]
        self.assertEqual(analyze(map(json.dumps,e),900,expected_names=['a.cpp','b.cpp','c.cpp'])['candidates'],[])
    def test_legacy_inference_resets_on_new_session(self):
        e=[{'event':'unit','name':'a.cpp','state':'cached'},{'event':'progress','done':1,'total':2,'active':1,'queued':0,'seconds':10},{'event':'phase','message':'Checking toolchain and hashing headers'},{'event':'progress','done':1,'total':2,'active':1,'queued':0,'seconds':1010}]
        self.assertEqual(analyze(map(json.dumps,e),900,expected_names=['a.cpp','b.cpp'])['candidates'],[])
