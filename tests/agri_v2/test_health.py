import json,unittest
from automation.agri_v2 import healthcheck as h
class HealthTests(unittest.TestCase):
 def test_exactly_first_fifteen(self):
  self.assertEqual(len(h.SLOTS),15);self.assertEqual(h.SLOTS[-1],'AGNES_API_KEY15');self.assertNotIn('AGNES_API_KEY16',h.SLOTS)
 def test_dedup_no_secrets(self):
  calls=[]
  def probe(key):calls.append(key);return {'text':{'state':'healthy'},'image':{'state':'healthy'}}
  result=h.audit({'AGNES_API_KEY':'TEST_SENTINEL_NEVER_LOG','AGNES_API_KEY2':'TEST_SENTINEL_NEVER_LOG','AGNES_API_KEY16':'IGNORE_ME'},probe)
  self.assertEqual(len(calls),1);self.assertEqual(result['unique_credentials_tested'],1);self.assertEqual(result['slots'][1]['duplicate_of'],'AGNES_API_KEY');self.assertNotIn('TEST_SENTINEL',json.dumps(result));self.assertNotIn('IGNORE_ME',json.dumps(result))
 def test_error_categories(self):
  self.assertEqual(h.classify(401,b'key-reflected-do-not-output'),'invalid_auth');self.assertEqual(h.classify(429,b'quota exhausted'),'quota_limited');self.assertEqual(h.classify(429,b''),'rate_limited');self.assertEqual(h.classify(503),'provider_unavailable')
 def test_missing_never_probed(self):
  r=h.audit({},lambda key:self.fail('must not probe'));self.assertEqual(r['configured_slots'],0)
 def test_failure_no_raw_exception(self):
  def fail(key):raise ValueError('TEST_SECRET_SENTINEL')
  r=h.audit({'AGNES_API_KEY':'x'},fail);self.assertNotIn('TEST_SECRET_SENTINEL',json.dumps(r))
if __name__=='__main__':unittest.main()
