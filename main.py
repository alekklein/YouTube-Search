import asyncio, hashlib, json, os, re, sqlite3, time
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import isodate

APP_DIR=Path(__file__).resolve().parent
DB_PATH=Path(os.getenv('CACHE_DB_PATH', APP_DIR/'cache.sqlite3'))
TTL=int(os.getenv('SEARCH_CACHE_TTL_HOURS','24'))*3600
KEYS=[x.strip() for x in re.split(r'[,\s]+',os.getenv('YOUTUBE_API_KEYS','') or os.getenv('YOUTUBE_API_KEY','')) if x.strip()]
app=FastAPI(title='VOD Finder')

def conn():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE IF NOT EXISTS searches(k TEXT PRIMARY KEY, t INTEGER NOT NULL, payload TEXT NOT NULL)'); return c
conn().close()

def key_for(p): return hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def duration(s):
 h,r=divmod(max(0,s),3600); m,s=divmod(r,60); return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
def normalize(v):
 sn=v.get('snippet',{}); cd=v.get('contentDetails',{}); st=v.get('statistics',{}); live=v.get('liveStreamingDetails',{})
 try: sec=int(isodate.parse_duration(cd.get('duration','PT0S')).total_seconds())
 except: sec=0
 th=sn.get('thumbnails',{}); thumb=(th.get('medium') or th.get('high') or th.get('default') or {}).get('url','')
 return {'id':v.get('id',''),'title':sn.get('title',''),'channel':sn.get('channelTitle',''),'published':sn.get('publishedAt','')[:10],'views':int(st.get('viewCount',0) or 0),'seconds':sec,'duration':duration(sec),'live':bool(live.get('actualStartTime') or live.get('scheduledStartTime')),'age':cd.get('contentRating',{}).get('ytRating')=='ytAgeRestricted','thumb':thumb,'description':(sn.get('description') or '')[:280],'url':'https://www.youtube.com/watch?v='+v.get('id','')}

def youtube_search(api,q,mode,order,pages,after,before):
 y=build('youtube','v3',developerKey=api,cache_discovery=False); ids=[]; token=None
 for _ in range(max(1,min(5,pages))):
  p={'q':q,'part':'id,snippet','type':'video','order':order if order in ('relevance','date','viewCount') else 'relevance','maxResults':50,'safeSearch':'none'}
  if mode=='past_live': p['eventType']='completed'
  if after:p['publishedAfter']=after
  if before:p['publishedBefore']=before
  if token:p['pageToken']=token
  r=y.search().list(**p).execute()
  for x in r.get('items',[]):
   i=x.get('id',{}).get('videoId')
   if i and i not in ids:ids.append(i)
  token=r.get('nextPageToken')
  if not token:break
 out=[]
 for i in range(0,len(ids),50):
  r=y.videos().list(part='snippet,statistics,contentDetails,liveStreamingDetails',id=','.join(ids[i:i+50]),maxResults=50).execute(); out += [normalize(x) for x in r.get('items',[])]
 return out

def load(k):
 with conn() as c:
  r=c.execute('SELECT payload FROM searches WHERE k=? AND t>=?',(k,int(time.time())-TTL)).fetchone()
  return json.loads(r['payload']) if r else None

def save(k,data):
 with conn() as c:
  c.execute('INSERT OR REPLACE INTO searches VALUES(?,?,?)',(k,int(time.time()),json.dumps(data,ensure_ascii=False)))
  c.execute('DELETE FROM searches WHERE t<?',(int(time.time())-TTL*2,))

def filter_results(items,q,minv,maxv,mind,maxd,age,live):
 toks=[x for x in re.findall(r'[\w]+',q.lower()) if len(x)>1]; out=[]
 for v in items:
  if v['views']<minv or maxv and v['views']>maxv or v['seconds']<mind or maxd and v['seconds']>maxd or age and not v['age'] or live and not v['live']:continue
  text=(v['title']+' '+v['description']).lower(); title=v['title'].lower(); score=(100 if q.lower() in title else 0)+sum(10 for t in toks if re.search(r'\b'+re.escape(t)+r'\b',text))
  v=dict(v);v['score']=score;out.append(v)
 return sorted(out,key=lambda x:(-x['score'],-x['views']))

HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>VOD Finder</title><style>*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8ecf7;font:15px system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:1200px;margin:auto;padding:28px 18px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:22px}.muted{color:#929bb2}.search{display:grid;grid-template-columns:1fr auto;gap:10px}.box,.btn,select{border:1px solid #2a3552;background:#121a2d;color:#fff;border-radius:12px;padding:12px}.box{width:100%}.btn{cursor:pointer;background:#6d5dfc;border:0;font-weight:700}.panel{background:#11182a;border:1px solid #26314b;border-radius:16px;padding:14px;margin:14px 0}.filters{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}.card{overflow:hidden;background:#11182a;border:1px solid #26314b;border-radius:16px}.card img{width:100%;aspect-ratio:16/9;object-fit:cover}.pad{padding:13px}.title{font-weight:700;line-height:1.3}.meta{font-size:12px;color:#929bb2;margin-top:7px}.desc{font-size:13px;color:#b8c0d2;margin-top:8px}.status{min-height:22px;margin:10px 0;color:#aeb8ce}@media(max-width:750px){.hero{display:block}.search{grid-template-columns:1fr}.filters{grid-template-columns:1fr 1fr}}@media(max-width:450px){.filters{grid-template-columns:1fr}}</style></head><body><main><div class="hero"><div><h1>VOD Finder</h1><div class="muted">Fast YouTube search with cached results and instant filters.</div></div><div id="count" class="muted"></div></div><form id="search"><div class="search"><input class="box" name="query" placeholder="Search videos, streams, channels…" autocomplete="off"><button class="btn">Search</button></div><div class="panel"><div class="filters"><select name="mode"><option value="all">All videos</option><option value="past_live">Past livestreams</option></select><select name="order"><option value="relevance">Most relevant</option><option value="date">Newest</option><option value="viewCount">Most viewed</option></select><select name="pages"><option value="1">~50 results</option><option value="2" selected>~100 results</option><option value="3">~150 results</option><option value="5">~250 results</option></select><input class="box" name="min_duration" type="number" min="0" placeholder="Min duration (sec)"></div><div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><input class="box" style="max-width:220px" name="max_duration" type="number" min="0" placeholder="Max duration (sec)"><input class="box" style="max-width:220px" name="min_views" type="number" min="0" placeholder="Min views"><label><input name="only_live" type="checkbox"> Live/VOD only</label><label><input name="only_age" type="checkbox"> Age restricted</label></div></div></form><div id="status" class="status"></div><section id="results" class="results"></section></main><script>const f=document.querySelector('#search'),r=document.querySelector('#results'),s=document.querySelector('#status'),c=document.querySelector('#count');let busy=false;function card(v){return `<article class="card"><a href="${v.url}" target="_blank" rel="noopener"><img loading="lazy" src="${v.thumb}" alt=""></a><div class="pad"><div class="title">${esc(v.title)}</div><div class="meta">${esc(v.channel)} · ${v.duration} · ${v.views.toLocaleString()} views · ${v.published}</div><div class="desc">${esc(v.description)}</div></div></article>`}function esc(x){return String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}f.addEventListener('submit',async e=>{e.preventDefault();if(busy)return;const q=f.querySelector('[name=query]').value.trim();if(!q)return;s.textContent='Searching…';busy=true;const old=r.innerHTML;try{const res=await fetch('/api/search',{method:'POST',body:new FormData(f)});const data=await res.json();if(!res.ok)throw Error(data.error||'Search failed');r.innerHTML=data.results.map(card).join('');c.textContent=`${data.total_returned} shown · ${data.total_collected} collected${data.cached?' · cached':''}`;s.textContent='Ready';}catch(err){s.textContent=err.message;if(!r.innerHTML)r.innerHTML=old}finally{busy=false}})</script></body></html>'''

@app.get('/',response_class=HTMLResponse)
async def home():return HTML
@app.get('/health')
async def health():return {'ok':True,'keys':len(KEYS)}
@app.post('/api/search')
async def search(query:str=Form(...),mode:str=Form('all'),order:str=Form('relevance'),pages:int=Form(2),published_after:str=Form(''),published_before:str=Form(''),min_views:int=Form(0),max_views:int=Form(0),min_duration:int=Form(0),max_duration:int=Form(0),only_age:bool=Form(False),only_live:bool=Form(False)):
 q=re.sub(r'\s+',' ',query.strip()).casefold()
 if not q:return JSONResponse({'error':'Enter a search term.'},400)
 p={'q':q,'mode':mode,'order':order,'pages':max(1,min(5,pages)),'after':published_after or None,'before':published_before or None}; k=key_for(p); data=load(k); cached=data is not None
 if data is None:
  if not KEYS:return JSONResponse({'error':'Configure YOUTUBE_API_KEYS in Render.'},503)
  try:data=await asyncio.to_thread(youtube_search,KEYS[0],q,mode,order,p['pages'],p['after'],p['before']);save(k,data)
  except HttpError as e:return JSONResponse({'error':f'YouTube API error: {e}'},502)
  except Exception as e:return JSONResponse({'error':str(e)},500)
 out=filter_results(data,q,max(0,min_views),max(0,max_views),max(0,min_duration),max(0,max_duration),only_age,only_live)
 return {'results':out,'total_collected':len(data),'total_returned':len(out),'cached':cached}
