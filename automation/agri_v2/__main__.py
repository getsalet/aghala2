import argparse,json,sys
from .queue import DATA,items,get_topic,atomic_json
from .pipeline import plan,draft,merge_plan

def main():
 p=argparse.ArgumentParser(description='AFP v2: offline queues, manual draft artifacts, no publication')
 s=p.add_subparsers(dest='command',required=True);s.add_parser('validate')
 template=s.add_parser('brief-template');template.add_argument('--topic-id',required=True);template.add_argument('--queue',choices=['new','merge'],default='new');template.add_argument('--output',required=True)
 d=s.add_parser('draft');d.add_argument('--topic-id',required=True);d.add_argument('--brief',required=True);d.add_argument('--inventory',required=True);d.add_argument('--health',default=str(DATA/'health/latest.json'));d.add_argument('--allow-model-calls',action='store_true')
 m=s.add_parser('merge-plan');m.add_argument('--topic-id',required=True);m.add_argument('--target-id',required=True,type=int);m.add_argument('--snapshot',required=True);m.add_argument('--brief',required=True)
 a=p.parse_args()
 try:
  if a.command=='validate':result=plan()
  elif a.command=='draft':result=draft(a.topic_id,a.brief,a.inventory,a.health,a.allow_model_calls)
  elif a.command=='merge-plan':result=merge_plan(a.topic_id,a.target_id,a.snapshot,a.brief)
  else:
   t=get_topic(a.queue,a.topic_id);target=a.output
   from pathlib import Path
   if Path(target).exists():raise ValueError('Template output already exists; not overwriting reviews')
   obj={'topic_id':t['id'],'title':t['title'],'approved_for_draft':False,'semantic_review_done':False,'overlap_review_done':False,'sources_verified':False,'editor':None,'agronomy_reviewer':None,'reviewed_at':None,'novelty_note':None,'slug':None,'outline':[],'sources':[],'approved_facts':[]}
   atomic_json(target,obj);result={'status':'unapproved_template_created'}
  print(json.dumps(result,ensure_ascii=False));return 0
 except Exception as e:
  # Unknown/provider exceptions must never print raw credential-bearing messages.
  if isinstance(e,(ValueError,KeyError,FileNotFoundError)):
   print('Blocked: incomplete/invalid inputs or validation failure. See contract and tests.',file=sys.stderr)
  else:print('Blocked: sanitized model/runtime failure; staged state preserved.',file=sys.stderr)
  return 2
if __name__=='__main__':sys.exit(main())
