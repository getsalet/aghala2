"""Bounded capability audit. Never serialize credentials, hashes or provider text."""
import base64,concurrent.futures,datetime,io,json,os,socket,time,ipaddress,urllib.request,urllib.error
from pathlib import Path
API_BASE='https://apihub.agnes-ai.com/v1'
TEXT_MODEL='agnes-2.5-flash'
IMAGE_MODEL='agnes-image-2.5-flash'
SLOTS=['AGNES_API_KEY']+[f'AGNES_API_KEY{i}' for i in range(2,16)]
MAX_RESPONSE=18*1024*1024

def now():return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
def classify(status,body=b''):
    # Inspect only for whitelisted categories; never return raw error payload.
    text=body[:20000].decode('utf-8','replace').lower()
    if status==401:return 'invalid_auth'
    if status==403:return 'forbidden'
    if status==429:return 'quota_limited' if any(x in text for x in ('quota','balance','credit','billing')) else 'rate_limited'
    if status==402:return 'quota_limited'
    if status in (400,404,422):
        if any(x in text for x in ('model_not_found','model not found','invalid model','unsupported model','model_not_available')):return 'model_unavailable'
        return 'request_rejected'
    if status>=500:return 'provider_unavailable'
    return 'http_error'

def post(endpoint,key,payload,timeout=100):
    req=urllib.request.Request(API_BASE+endpoint,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','User-Agent':'AFP-agri-v2-capability-check'})
    started=time.monotonic()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            blob=r.read(MAX_RESPONSE+1)
            if len(blob)>MAX_RESPONSE:return {'state':'response_too_large','http_status':r.status},None
            try:obj=json.loads(blob)
            except (ValueError,TypeError):return {'state':'invalid_json','http_status':r.status},None
            return {'state':'responded','http_status':r.status,'latency_ms':round((time.monotonic()-started)*1000)},obj
    except urllib.error.HTTPError as e:
        return {'state':classify(e.code,e.read(20000)),'http_status':e.code},None
    except (urllib.error.URLError,TimeoutError,OSError):return {'state':'network_or_timeout','http_status':None},None

def verify_image(obj):
    from PIL import Image
    if not isinstance(obj,dict) or not isinstance(obj.get('data'),list) or not obj['data']:return False
    row=obj['data'][0]
    if not isinstance(row,dict) or not isinstance(row.get('b64_json'),str):return False
    try:
        blob=base64.b64decode(row['b64_json'],validate=True)
        if not (100<=len(blob)<=MAX_RESPONSE):return False
        with Image.open(io.BytesIO(blob)) as im:
            w,h=im.size
            if w<64 or h<64 or w*h>20_000_000:return False
            im.verify()
        return True
    except Exception:return False

def probe(key):
    txt,obj=post('/chat/completions',key,{'model':TEXT_MODEL,'messages':[{'role':'user','content':'Reply with OK only.'}],'temperature':0,'max_tokens':8},timeout=70)
    if obj is not None:
        try:content=obj['choices'][0]['message']['content']
        except (KeyError,IndexError,TypeError):content=None
        model=obj.get('model') if isinstance(obj,dict) else None
        txt['state']='healthy' if isinstance(content,str) and content.strip() else 'invalid_text_response'
        if model and model!=TEXT_MODEL:txt['state']='model_mismatch'
    image={'state':'not_probed_text_not_healthy','http_status':None}
    if txt['state']=='healthy':
        image,imobj=post('/images/generations',key,{'model':IMAGE_MODEL,'prompt':'A plain green leaf on a clean white background. No words, no logo, no people. Capability check.','n':1,'size':'1024x768','return_base64':True,'extra_body':{'response_format':'b64_json'}},timeout=100)
        if imobj is not None:image['state']='healthy' if verify_image(imobj) else 'image_not_verified'
    return {'text':txt,'image':image}

def audit(env=None,probe_fn=None):
    env=os.environ if env is None else env;probe_fn=probe if probe_fn is None else probe_fn
    owners={};unique={};aliases={};report=[]
    for slot in SLOTS:
        key=env.get(slot,'').strip()
        if not key:report.append({'slot':slot,'configured':False,'text':{'state':'missing'},'image':{'state':'missing'}});continue
        if key in owners:aliases[slot]=owners[key]
        else:owners[key]=slot;unique[slot]=key
    results={}
    # Two workers, max one tiny text + one image request per unique configured key.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs={pool.submit(probe_fn,key):slot for slot,key in unique.items()}
        for future in concurrent.futures.as_completed(jobs):
            slot=jobs[future]
            try:results[slot]=future.result()
            except Exception:results[slot]={'text':{'state':'probe_internal_error'},'image':{'state':'not_probed'}}
    byslot={r['slot']:r for r in report}
    for slot in SLOTS:
        if slot in byslot:continue
        owner=aliases.get(slot,slot);entry={'slot':slot,'configured':True,**results[owner]}
        if slot in aliases:entry['duplicate_of']=owner
        byslot[slot]=entry
    ordered=[byslot[s] for s in SLOTS]
    return {'version':1,'checked_at':now(),'text_model':TEXT_MODEL,'image_model':IMAGE_MODEL,'api_base':API_BASE,'slot_limit':15,'configured_slots':sum(x['configured'] for x in ordered),'unique_credentials_tested':len(unique),'slots':ordered,'eligible_text_slots':[s for s in SLOTS if s in results and results[s]['text']['state']=='healthy'],'eligible_image_slots':[s for s in SLOTS if s in results and results[s]['image']['state']=='healthy'],'no_article_generation':True,'no_publication':True,'run_id':str(env.get('GITHUB_RUN_ID','')),'source_commit':str(env.get('GITHUB_SHA',''))}

def main():
    report=audit();path=Path('data/agri-v2/health/latest.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Sanitized health report saved. Slots:',len(report['slots']),'unique:',report['unique_credentials_tested'],'text eligible:',len(report['eligible_text_slots']),'image eligible:',len(report['eligible_image_slots']))
    return 0 if report['eligible_text_slots'] and report['eligible_image_slots'] else 2
if __name__=='__main__':raise SystemExit(main())
