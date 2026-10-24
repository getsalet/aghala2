"""Immutable queues + separate, atomic state; never touches legacy queue."""
import contextlib,datetime,fcntl,hashlib,json,os,re,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'data/agri-v2';OUT=ROOT/'artifacts/agri-v2'
def load_json(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def atomic_json(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 fd,name=tempfile.mkstemp(dir=path.parent,prefix='.tmp-')
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(obj,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
  os.replace(name,path)
 finally:
  if os.path.exists(name):os.unlink(name)
def safe_id(ident):
 if not re.fullmatch(r'agri-[a-f0-9]{12}',ident):raise ValueError('Invalid topic ID')
 return ident
def items(kind,data=DATA):
 if kind not in {'new','merge','hold'}:raise ValueError('Unknown queue')
 manifest=load_json(data/'manifest.json');result=[]
 for ent in manifest['files']:
  if ent['queue']!=kind:continue
  rel=Path(ent['path']);parts=rel.parts
  if parts[:2]!=('data','agri-v2') or '..' in parts:raise ValueError('Unsafe manifest path')
  path=data/Path(*parts[2:]);blob=path.read_bytes()
  if hashlib.sha256(blob).hexdigest()!=ent['sha256']:raise ValueError('Queue shard checksum mismatch')
  batch=[json.loads(line) for line in blob.decode().splitlines() if line.strip()]
  if len(batch)!=ent['count']:raise ValueError('Queue shard row mismatch')
  result.extend(batch)
 if len(result)!=manifest['counts'][kind]:raise ValueError('Queue total mismatch')
 if len({x['id'] for x in result})!=len(result):raise ValueError('Duplicate topic ID')
 return result
def get_topic(kind,ident,data=DATA):
 safe_id(ident)
 for x in items(kind,data):
  if x['id']==ident:return x
 raise ValueError('Topic not found in requested queue')
@contextlib.contextmanager
def locked(out=OUT):
 out=Path(out);out.mkdir(parents=True,exist_ok=True)
 with (out/'.writer.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('Another writer holds the v2 lock')
  try:yield
  finally:fcntl.flock(lock,fcntl.LOCK_UN)
def utcnow():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def age_hours(timestamp,now=None):
 d=datetime.datetime.fromisoformat(timestamp.replace('Z','+00:00'))
 if d.tzinfo is None:raise ValueError('Timestamp must include timezone')
 age=((now or datetime.datetime.now(datetime.timezone.utc))-d).total_seconds()/3600
 if age<-.1:raise ValueError('Future timestamp is invalid')
 return age
