#!/usr/bin/env python3
import html, json, os, re, time, unicodedata, urllib.request, urllib.error, itertools, threading
from pathlib import Path
from html.parser import HTMLParser
from agnes_json_client import parse_object

ROOT=Path(__file__).resolve().parents[1]
BASE=os.getenv('AGNES_API_BASE','https://apihub.agnes-ai.com/v1').rstrip('/')
TOKENS=[];TOKEN_SLOTS={}
# Supports AGNES_API_KEY plus AGNES_API_KEY2..AGNES_API_KEY50 (25 configured).
for _name in ['AGNES_API_KEY',*[f'AGNES_API_KEY{i}' for i in range(2,51)]]:
    _value=os.environ.get(_name,'').strip()
    if _value and _value not in TOKENS:TOKENS.append(_value);TOKEN_SLOTS[_value]=_name
TOKEN=TOKENS[0] if TOKENS else ''
_TOKEN_COUNTER=itertools.count();_TOKEN_LOCK=threading.Lock()
# Keys rejected with 401/403 are skipped for the rest of this run so one bad
# secret cannot fail every Nth request. Only the secret *name* is logged.
BAD_TOKENS=set()
def active_tokens():
    return [t for t in TOKENS if t not in BAD_TOKENS]
def next_token():
    if not TOKENS:raise RuntimeError('No Agnes API key is configured')
    with _TOKEN_LOCK:
        pool=active_tokens()
        if not pool:raise RuntimeError('All Agnes API keys were rejected (401/403): '+', '.join(sorted(TOKEN_SLOTS[t] for t in BAD_TOKENS)))
        return pool[next(_TOKEN_COUNTER)%len(pool)]
def disable_token(token,code):
    with _TOKEN_LOCK:
        if token not in TOKENS or token in BAD_TOKENS:return
        BAD_TOKENS.add(token)
    print(f'agnes_key_disabled secret={TOKEN_SLOTS.get(token,"?")} http={code} active={len(active_tokens())}/{len(TOKENS)}',flush=True)
print(f'agnes_keys_loaded={len(TOKENS)}',flush=True)
MODEL=os.getenv('AGNES_MODEL','agnes-3.0-flash').strip()
LIMIT=max(1,int(os.getenv('MAX_ARTICLES','1')))
STATE=ROOT/'state/progress.json'
SOURCE_IDS=ROOT/'data/source_ids.txt'
PERSIAN=re.compile(r'[\u0600-\u06ff]')

class Tags(HTMLParser):
    def __init__(self): super().__init__(); self.counts={}
    def handle_starttag(self,t,a): self.counts[t]=self.counts.get(t,0)+1
    def handle_startendtag(self,t,a): self.counts[t]=self.counts.get(t,0)+1

def counts(s):
    p=Tags(); p.feed(s); return p.counts

def slugify(s):
    s=unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+','-',s).strip('-')[:90] or 'translated-article'

def request_json(url,payload=None,authenticated=False):
    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
    headers={'Accept':'application/json','User-Agent':'navar-abyari-actions'}
    if payload is not None: headers['Content-Type']='application/json'
    token=next_token() if authenticated else ''
    if authenticated: headers['Authorization']=f'Bearer {token}'
    attempts=3 if authenticated else 1
    last=None
    attempt=0;key_swaps=0
    while attempt<attempts:
        attempt+=1
        req=urllib.request.Request(url,data=data,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=240) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode('utf-8','replace');last=exc
            print(f'HTTP {exc.code} on {url}: {detail[:300]}',flush=True)
            if authenticated and exc.code in (401,403):
                # Invalid/unauthorized key: quarantine it and retry at once with
                # another key without consuming a normal retry attempt.
                disable_token(token,exc.code)
                if active_tokens() and key_swaps<len(TOKENS):
                    key_swaps+=1;attempt-=1
                    token=next_token();headers['Authorization']=f'Bearer {token}'
                    continue
                raise
            if exc.code not in (408,429,500,502,503,504) or attempt>=attempts:raise
            retry_after=exc.headers.get('Retry-After') if exc.headers else None
            delay=int(retry_after) if retry_after and retry_after.isdigit() else 45*attempt
            print(f'API backoff {delay}s before retry {attempt+1}/{attempts}',flush=True)
            time.sleep(min(180,delay))
        except (TimeoutError,urllib.error.URLError,ConnectionError,OSError) as exc:
            last=exc
            if attempt>=attempts:raise
            delay=20*attempt
            print(f'API transport retry {attempt+1}/{attempts} after {delay}s: {type(exc).__name__}',flush=True)
            time.sleep(delay)
    raise RuntimeError(f'API request failed after {attempts} attempts: {last}')

def fetch_source(post_id):
    data=request_json(f'https://navar-abyari.ir/wp-json/wp/v2/posts/{post_id}')
    title=html.unescape(data['title']['rendered'])
    content=data['content']['rendered']
    if not title or not content: raise RuntimeError(f'WordPress REST returned empty source for {post_id}')
    return {'id':post_id,'title':title,'slug':data.get('slug',''),'date':data.get('date',''),'content':content}

def call_chat_raw(messages,max_tokens=12000):
    payload={'model':MODEL,'messages':messages,'temperature':0.1,'max_tokens':max_tokens}
    result=request_json(BASE+'/chat/completions',payload,authenticated=True)
    raw=result['choices'][0]['message']['content']
    if isinstance(raw,list):
        raw=''.join(str(x.get('text','')) for x in raw if isinstance(x,dict))
    return str(raw).strip()

def call_chat(messages,max_tokens=12000):
    raw=call_chat_raw(messages,max_tokens=max_tokens)
    return parse_object(raw)

def translate_single_text(text,lang):
    """Last-resort non-JSON path for one segment.

    Long translations are still split and validated normally. This fallback
    only avoids starving the queue when the model returns good prose wrapped
    in malformed JSON for a single small segment.
    """
    label=language_label(lang)
    messages=[
        {'role':'system','content':'You are a precise agricultural localization editor.'},
        {'role':'user','content':(
            f'Translate the following Persian text into {label}. Return only the translated '
            'text: no JSON, Markdown fence, label, explanation or quotation marks. Preserve '
            f'numbers, URLs, product names and AFP.\n\n{text}'
        )},
    ]
    raw=call_chat_raw(messages,max_tokens=min(6000,max(1200,len(text)*3)))
    raw=re.sub(r'^```(?:text|markdown)?\s*|\s*```$','',raw,flags=re.I|re.S).strip()
    if len(raw)>=2 and raw[0]==raw[-1] and raw[0] in ('"',"'"):
        raw=raw[1:-1].strip()
    if not raw:
        raise ValueError('Plain-text fallback returned an empty translation')
    return raw

def language_label(lang):
    return {
        'ar-IQ':'Iraqi Arabic in clear professional Modern Standard Arabic suitable for Iraqi farmers',
        'tg-TJ':'natural Tajik in Cyrillic script suitable for Tajik farmers',
        'en-US':'clear international technical English suitable for farmers and irrigation professionals',
        'ru-RU':'natural professional Russian in Cyrillic script suitable for farmers and irrigation professionals',
    }[lang]

class PreserveHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False); self.parts=[]; self.items=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        self.parts.append(self.get_starttag_text())
        if tag in ('script','style'): self.skip+=1
    def handle_startendtag(self,tag,attrs): self.parts.append(self.get_starttag_text())
    def handle_endtag(self,tag):
        self.parts.append(f'</{tag}>')
        if tag in ('script','style') and self.skip: self.skip-=1
    def handle_entityref(self,name): self.parts.append(f'&{name};')
    def handle_charref(self,name): self.parts.append(f'&#{name};')
    def handle_comment(self,data): self.parts.append(f'<!--{data}-->')
    def handle_decl(self,decl): self.parts.append(f'<!{decl}>')
    def handle_pi(self,data): self.parts.append(f'<?{data}>')
    def handle_data(self,data):
        if self.skip or not PERSIAN.search(data): self.parts.append(data); return
        m=re.match(r'^(\s*)(.*?)(\s*)$',data,re.S); pre,core,post=m.groups()
        item={'id':len(self.items),'text':core}; self.items.append(item)
        self.parts.append({'id':item['id'],'pre':pre,'post':post})

def translate_segment_batch(items,lang):
    label=language_label(lang)
    prompt=f'''Translate each Persian text segment into {label}.
Return valid JSON only as {{"translations":[{{"id":0,"text":"..."}}]}}.
Return exactly one item for every input id and preserve their order. Translate text only; do not add HTML, commentary, facts, or Markdown. Preserve numbers, URLs, product names, and AFP.
INPUT:\n{json.dumps(items,ensure_ascii=False)}'''
    messages=[{'role':'system','content':'You are a precise agricultural localization editor. Output valid JSON only.'},{'role':'user','content':prompt}]
    last=None
    for attempt in range(3):
        try:
            data=call_chat(messages,max_tokens=min(12000,max(2500,sum(len(x['text']) for x in items)*3)))
            rows=data['translations'] if isinstance(data,dict) else data
            mapping={int(x['id']):str(x['text']) for x in rows}
            expected={int(x['id']) for x in items}
            if set(mapping)!=expected: raise ValueError(f'Segment IDs changed: expected {expected}, got {set(mapping)}')
            return mapping
        except Exception as exc:
            last=exc; print(f'Segment attempt {attempt+1} failed for {lang}: {type(exc).__name__}: {exc}',flush=True)
            if attempt<2: time.sleep(4*(attempt+1))
    if len(items)>1:
        mid=len(items)//2
        left=translate_segment_batch(items[:mid],lang); right=translate_segment_batch(items[mid:],lang)
        return {**left,**right}
    try:
        translated=translate_single_text(str(items[0]['text']),lang)
        print(f'Plain-text fallback succeeded for segment {items[0]["id"]} in {lang}',flush=True)
        return {int(items[0]['id']):translated}
    except Exception as fallback_exc:
        raise RuntimeError(
            f'Segment translation failed for {lang}: JSON={last}; plain={fallback_exc}'
        ) from fallback_exc

def translate_html(source_html,lang):
    parser=PreserveHTML(); parser.feed(source_html); parser.close()
    translated={}
    batch=[]; chars=0
    for item in parser.items:
        n=len(item['text'])
        if batch and (len(batch)>=8 or chars+n>1600):
            translated.update(translate_segment_batch(batch,lang)); batch=[]; chars=0
        batch.append(item); chars+=n
    if batch: translated.update(translate_segment_batch(batch,lang))
    out=[]
    for part in parser.parts:
        if isinstance(part,str): out.append(part)
        else: out.append(part['pre']+html.escape(translated[part['id']],quote=False)+part['post'])
    return ''.join(out)

def translate_metadata(source,lang):
    label=language_label(lang)
    prompt=f'''Translate this Persian WordPress article metadata into {label}.
Return JSON only with keys title, slug, seo_title, seo_description. Use a unique readable Latin slug. Keep AFP and technical meaning. No HTML or Markdown.
TITLE: {source['title']}'''
    messages=[{'role':'system','content':'You are a precise SEO localization editor. Output valid JSON only.'},{'role':'user','content':prompt}]
    last=None
    for attempt in range(3):
        try:
            result=call_chat(messages,max_tokens=1200)
            if not {'title','slug','seo_title','seo_description'}.issubset(result): raise ValueError('Metadata keys missing')
            result['slug']=slugify(result['slug']); return result
        except Exception as exc:
            last=exc; print(f'Metadata attempt {attempt+1} failed for {lang}: {type(exc).__name__}: {exc}',flush=True)
            if attempt<2: time.sleep(4*(attempt+1))
    raise RuntimeError(f'Metadata translation failed for {lang}: {last}')

def translate_validated(source,lang):
    result=translate_metadata(source,lang)
    result['html']=translate_html(source['content'],lang)
    a,b=counts(source['content']),counts(result['html'])
    for tag in ('h1','h2','h3','h4','ul','ol','li','a','img','table','tr','td'):
        if a.get(tag,0)!=b.get(tag,0): raise ValueError(f'HTML count changed for {tag}: {a.get(tag,0)} -> {b.get(tag,0)}')
    if lang=='ar-IQ' and len(re.findall(r'[ء-ي]',result['html']))<100: raise ValueError('Arabic script validation failed')
    if lang=='tg-TJ' and len(re.findall(r'[А-Яа-яҚқҒғҲҳҶҷӢӣӮӯ]',result['html']))<100: raise ValueError('Tajik script validation failed')
    if lang=='en-US' and len(re.findall(r'\b[A-Za-z]{2,}\b',result['html']))<100: raise ValueError('English text validation failed')
    if lang=='ru-RU':
        text=re.sub(r'<[^>]+>',' ',result['html'])
        if len(re.findall(r'[А-Яа-яЁё]',text))<100: raise ValueError('Russian script validation failed')
        if len(re.findall(r'[ҚқҒғҲҳҶҷӢӣӮӯ]',text))>10: raise ValueError('Russian validation failed: Tajik-specific letters found')
        if len(PERSIAN.findall(text))>20: raise ValueError('Russian validation failed: untranslated Persian text remains')
    return result

def main():
    if not TOKEN: raise RuntimeError('AGNES_API_KEY is empty')
    ids=[int(x.strip()) for x in SOURCE_IDS.read_text().splitlines() if x.strip()]
    if len(ids)!=len(set(ids)): raise RuntimeError('Duplicate source IDs in queue')
    state=json.loads(STATE.read_text(encoding='utf-8'))
    languages=('ar-IQ','tg-TJ','en-US')
    state['total_units']=len(ids)*len(languages)
    valid_keys={f'{post_id}:{lang}' for post_id in ids for lang in languages}
    completed=set(state.get('completed_keys',[])) & valid_keys; done_articles=0
    for post_id in ids:
        needed=[lang for lang in languages if f'{post_id}:{lang}' not in completed]
        if not needed: continue
        source=fetch_source(post_id)
        print(f'Processing source {post_id}: {source["title"]} -> {needed}',flush=True)
        for lang in needed:
            result=translate_validated(source,lang)
            folder=ROOT/'translations'/str(post_id); folder.mkdir(parents=True,exist_ok=True)
            (folder/f'{lang}.html').write_text(result['html'],encoding='utf-8')
            manifest={k:result[k] for k in ('title','slug','seo_title','seo_description')}
            manifest.update({'source_id':post_id,'language':lang,'source_title':source['title'],'source_slug':source['slug'],'source_date':source['date'],'provider':'Agnes AI','model':MODEL,'validated':True,'translation_mode':'structure-preserving-segments'})
            (folder/f'{lang}.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            completed.add(f'{post_id}:{lang}'); print(f'Validated {post_id}:{lang}',flush=True)
        done_articles+=1
        if done_articles>=LIMIT: break
    state['completed_keys']=sorted(completed); state['completed_units']=len(completed); state['remaining_units']=max(0,state['total_units']-len(completed))
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    pct=100*state['completed_units']/state['total_units']
    (ROOT/'docs/live-status.md').write_text(f"# وضعیت ترجمه دیتابیس نوار آبیاری\n\nاین فایل توسط GitHub Actions و Agnes AI به‌روزرسانی می‌شود.\n\n| مورد | مقدار |\n|---|---:|\n| کل واحدها | {state['total_units']} |\n| تکمیل‌شده | {state['completed_units']} |\n| باقی‌مانده | {state['remaining_units']} |\n| پیشرفت | {pct:.2f}% |\n| آخرین مدل | `{MODEL}` |\n\nترجمه مقاله‌های بلند به‌صورت قطعه‌ای و با حفظ کامل ساختار HTML انجام می‌شود.\n",encoding='utf-8')
if __name__=='__main__': main()
