import collections,copy,datetime,hashlib,json,tempfile,unittest
from pathlib import Path
from automation.agri_v2 import queue as q,pipeline as p
from automation.agri_v2.client import ModelClient,CapabilityError
from automation.agri_v2.healthcheck import TEXT_MODEL,IMAGE_MODEL,API_BASE
class PipelineTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.t=q.items('new')[0]
  self.b={'topic_id':self.t['id'],'approved_for_draft':True,'semantic_review_done':True,'overlap_review_done':True,'sources_verified':True,'editor':'fixture-editor','agronomy_reviewer':'fixture-reviewer','reviewed_at':q.utcnow(),'novelty_note':'Fixture-only overlap review, never production approval','slug':'fixture-crop-guide','outline':['A','B','C'],'sources':[{'url':'https://example.org/research','locator':'fixture section','excerpt':'fixture source data '*10}],'approved_facts':[{'claim':'fixture qualitative statement','conditions':'fixture only','source_url':'https://example.org/research'}]}
 def tearDown(self):self.tmp.cleanup()
 def article(self):return {'title':self.t['title'],'content_html':'<p>'+'راهنمای کشاورزی '*300+'</p><p><a href="https://example.org/research">منبع</a></p>','excerpt':'این خلاصه فقط داده آزمون است و مقاله واقعی نیست.'}
 def write(self,name,obj):x=self.root/name;q.atomic_json(x,obj);return x
 def test_exact_counts_and_origins(self):
  self.assertEqual([len(q.items(k)) for k in ['new','merge','hold']],[2905,593,370]);self.assertEqual(collections.Counter(x['origin'] for x in q.items('new')),{'NEW':948,'REVIEW':1957});self.assertEqual(sum(not x['candidate_target_ids'] for x in q.items('merge')),5)
 def test_public_index_only_published(self):
  self.assertTrue(all(x['status']=='publish' for x in q.load_json(q.DATA/'existing-title-index.json')['records']))
 def test_hold_cannot_draft(self):
  with self.assertRaises(ValueError):q.get_topic('new',q.items('hold')[0]['id'])
 def test_root(self):self.assertTrue((q.ROOT/'automation/agri_v2/queue.py').exists())
 def test_unapproved_and_overlap_blocked(self):
  b=copy.deepcopy(self.b);b['approved_for_draft']=False
  with self.assertRaises(ValueError):p.check_brief(self.t,b)
  b=copy.deepcopy(self.b);b['overlap_review_done']=False
  with self.assertRaises(ValueError):p.check_brief(self.t,b)
 def test_numeric_and_script_and_links(self):
  a=self.article();p.validate_article(a,self.t,self.b)
  for bad in ['<script>alert(1)</script>','<p>100</p>','<a href="https://evil.invalid">link</a>','<p onclick="x">bad</p>']:
   b=copy.deepcopy(a);b['content_html']+=bad
   with self.assertRaises(ValueError):p.validate_article(b,self.t,self.b)
 def test_inventory_fail_closed(self):
  for obj in [{'source_generated_at':'2000-01-01T00:00:00Z','records':[{}]},{'source_generated_at':q.utcnow(),'records':[]}]:
   with self.assertRaises(ValueError):p.fresh_inventory(self.write('inv.json',obj))
 def test_client_no_calls_without_confirmation_or_keys(self):
  r={'checked_at':q.utcnow(),'text_model':TEXT_MODEL,'image_model':IMAGE_MODEL,'api_base':API_BASE,'eligible_text_slots':[],'eligible_image_slots':[]}
  with self.assertRaises(CapabilityError):ModelClient(r,{},False)
  with self.assertRaises(CapabilityError):ModelClient(r,{},True)
  r.update(eligible_text_slots=['AGNES_API_KEY16'])
  with self.assertRaises(CapabilityError):ModelClient(r,{'AGNES_API_KEY16':'fixture'},True)
 def test_partial_resume_and_completed_preserved(self):
  b=self.write('brief.json',self.b);inv=self.write('inventory.json',{'source_generated_at':q.utcnow(),'records':[{'id':1,'title':'unrelated','url':'https://example.org/unrelated/','status':'publish'}]});h=self.write('health.json',{});out=self.root/'artifacts';calls={'text':0,'image':0};article=self.article()
  class Fake:
   def __init__(self,*a,**k):pass
   def text(self,prompt):calls['text']+=1;return article
   def image(self,prompt):
    calls['image']+=1
    if calls['image']==2:raise CapabilityError('fixture failure')
    return b'fixture-image'
  with self.assertRaises(CapabilityError):p.draft(self.t['id'],b,inv,h,True,out,Fake)
  self.assertEqual(calls,{'text':1,'image':2});p.draft(self.t['id'],b,inv,h,True,out,Fake)
  self.assertEqual(calls,{'text':1,'image':4});self.assertEqual(p.draft(self.t['id'],b,inv,h,True,out,Fake)['status'],'already_drafted');self.assertEqual(calls,{'text':1,'image':4})
  payload=q.load_json(out/'new'/self.t['id']/'wordpress-draft-payload.json');self.assertEqual(payload['status'],'draft')
 def test_same_title_blocks_before_model(self):
  inv=self.write('inventory.json',{'source_generated_at':q.utcnow(),'records':[{'id':1,'title':self.t['title'],'url':'https://example.org/same/','status':'publish'}]})
  with self.assertRaises(ValueError):p.draft(self.t['id'],self.write('b.json',self.b),inv,'nonexistent',True,self.root/'out')
 def test_queue_checksum_corruption(self):
  data=self.root/'data';data.mkdir();(data/'new').mkdir();f=data/'new/part.jsonl';f.write_text('{}\n');q.atomic_json(data/'manifest.json',{'counts':{'new':1},'files':[{'queue':'new','path':'data/agri-v2/new/part.jsonl','count':1,'sha256':'bad'}]})
  with self.assertRaises(ValueError):q.items('new',data)
 def test_merge_revision_and_no_apply(self):
  t=next(x for x in q.items('merge') if x['candidate_target_ids']);target=t['candidate_target_ids'][0];b=copy.deepcopy(self.b);b['topic_id']=t['id'];snap={'id':target,'url':'https://example.org/target/','modified':q.utcnow(),'fetched_at':q.utcnow(),'content_html':'existing original','content_sha256':hashlib.sha256(b'existing original').hexdigest()}
  bp=self.write('mb.json',b);sp=self.write('snap.json',snap);out=self.root/'merge';r=p.merge_plan(t['id'],target,sp,bp,out);self.assertFalse(r['wordpress_updated']);proposal=q.load_json(out/'merge'/t['id']/'proposal.json');self.assertFalse(proposal['apply_implemented']);self.assertEqual(proposal['operation'],'enrich_existing')
  snap['content_sha256']='bad';self.write('snap.json',snap)
  with self.assertRaises(ValueError):p.merge_plan(t['id'],target,sp,bp,out)
 def test_configuration_no_publication(self):
  c=q.load_json(q.DATA/'config.json');self.assertFalse(c['publication_enabled']);self.assertFalse(c['automatic_generation']);self.assertEqual(c['max_key_slot'],15);self.assertEqual(c['text_model'],TEXT_MODEL);self.assertEqual(c['image_model'],IMAGE_MODEL)
if __name__=='__main__':unittest.main()
