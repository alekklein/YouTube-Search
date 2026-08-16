import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import isodate
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CACHE_DB_PATH", APP_DIR / "cache.sqlite3"))
CACHE_TTL = int(os.getenv("SEARCH_CACHE_TTL_HOURS", "24")) * 3600
RAW_KEYS = os.getenv("YOUTUBE_API_KEYS", "") or os.getenv("YOUTUBE_API_KEY", "")
API_KEYS = [k.strip() for k in re.split(r"[,\s]+", RAW_KEYS) if k.strip()]

app = FastAPI(title="TubeScout", version="5.0")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS searches (cache_key TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL)")
    return con


def cache_key(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_cache(key: str) -> list[dict[str, Any]] | None:
    with db() as con:
        row = con.execute("SELECT payload FROM searches WHERE cache_key=? AND created_at>=?", (key, int(time.time()) - CACHE_TTL)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def save_cache(key: str, payload: list[dict[str, Any]]) -> None:
    now = int(time.time())
    with db() as con:
        con.execute("INSERT OR REPLACE INTO searches(cache_key,created_at,payload) VALUES(?,?,?)", (key, now, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
        con.execute("DELETE FROM searches WHERE created_at<?", (now - CACHE_TTL * 2,))


def pretty_duration(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def normalize(video: dict[str, Any]) -> dict[str, Any]:
    snippet = video.get("snippet", {})
    details = video.get("contentDetails", {})
    stats = video.get("statistics", {})
    live = video.get("liveStreamingDetails", {})
    try:
        seconds = int(isodate.parse_duration(details.get("duration", "PT0S")).total_seconds())
    except Exception:
        seconds = 0
    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    return {
        "id": video.get("id", ""),
        "title": snippet.get("title", ""),
        "channel": snippet.get("channelTitle", ""),
        "published": snippet.get("publishedAt", "")[:10],
        "views": int(stats.get("viewCount", 0) or 0),
        "seconds": seconds,
        "duration": pretty_duration(seconds),
        "live": bool(live.get("actualStartTime") or live.get("scheduledStartTime")),
        "age": details.get("contentRating", {}).get("ytRating") == "ytAgeRestricted",
        "thumb": thumb,
        "description": (snippet.get("description") or "")[:300],
        "url": f"https://www.youtube.com/watch?v={video.get('id', '')}",
        "tags": snippet.get("tags", [])[:20],
    }


def youtube_search(api_key: str, query: str, order: str, pages: int, after: str, before: str) -> list[dict[str, Any]]:
    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    ids: list[str] = []
    token = None
    for _ in range(max(1, min(5, pages))):
        params: dict[str, Any] = {
            "q": query,
            "part": "id,snippet",
            "type": "video",
            "order": order if order in {"relevance", "date", "viewCount"} else "relevance",
            "maxResults": 50,
            "safeSearch": "none",
        }
        if after:
            params["publishedAfter"] = after
        if before:
            params["publishedBefore"] = before
        if token:
            params["pageToken"] = token
        response = youtube.search().list(**params).execute()
        for item in response.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in ids:
                ids.append(vid)
        token = response.get("nextPageToken")
        if not token:
            break

    results: list[dict[str, Any]] = []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        details = youtube.videos().list(part="snippet,statistics,contentDetails,liveStreamingDetails", id=",".join(batch), maxResults=50).execute()
        results.extend(normalize(item) for item in details.get("items", []))
    return results


def score_results(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    tokens = [t for t in re.findall(r"[\w]+", query.casefold()) if len(t) > 1]
    output = []
    for item in items:
        title = item["title"].casefold()
        text = f"{item['title']} {item['description']} {' '.join(item.get('tags', []))}".casefold()
        score = (120 if query.casefold() in title else 0)
        score += sum(8 for token in tokens if re.search(rf"\b{re.escape(token)}\b", title))
        score += sum(3 for token in tokens if re.search(rf"\b{re.escape(token)}\b", text))
        clone = dict(item)
        clone["score"] = score
        output.append(clone)
    return sorted(output, key=lambda x: (-x["score"], -x["views"], x["title"].casefold()))


def search_with_keys(query: str, order: str, pages: int, after: str, before: str) -> list[dict[str, Any]]:
    if not API_KEYS:
        raise RuntimeError("No YouTube API key configured. Set YOUTUBE_API_KEYS in Render.")
    last_error: Exception | None = None
    for key in API_KEYS:
        try:
            # Search broadly, then use videos.list contentDetails to enforce the age restriction.
            raw = youtube_search(key, query, order, pages, after, before)
            restricted = [v for v in raw if v.get("age") is True]
            return score_results(restricted, query)
        except HttpError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"YouTube API request failed: {last_error}")


INDEX = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090d12"><title>TubeScout</title>
<style>
:root{--bg:#090d12;--panel:#111821;--line:#263241;--text:#f2f5f8;--muted:#8d99a8;--accent:#58d6bd}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}.wrap{width:min(1080px,calc(100% - 24px));margin:auto}.top{padding:22px 0 12px;display:flex;align-items:center;justify-content:space-between}.brand{font-weight:800;font-size:18px}.sub{color:var(--muted);font-size:12px;margin-top:2px}.search{display:grid;grid-template-columns:1fr auto;gap:8px;padding:8px;background:var(--panel);border:1px solid var(--line);border-radius:14px}.search input{min-width:0;background:transparent;color:var(--text);border:0;outline:0;padding:10px 11px;font-size:16px}.search button{border:0;border-radius:10px;background:var(--accent);color:#06110f;font-weight:800;padding:0 18px;cursor:pointer}.search button:disabled{opacity:.55}.bar{display:flex;align-items:center;gap:8px;margin:9px 0 14px;color:var(--muted);font-size:12px}.pill{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:6px 9px}.controls{margin-left:auto;display:flex;gap:6px}.controls select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px}.status{color:var(--muted);margin:10px 2px}.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:11px;padding-bottom:30px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}.thumb{display:block;position:relative;background:#050709}.thumb img{width:100%;aspect-ratio:16/9;display:block;object-fit:cover}.duration{position:absolute;right:7px;bottom:7px;background:#000d;color:#fff;border-radius:5px;padding:3px 5px;font-size:11px}.body{padding:10px}.title{font-weight:700;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.meta{color:var(--muted);font-size:11px;margin-top:6px}.empty{border:1px dashed var(--line);border-radius:12px;padding:34px 18px;text-align:center;color:var(--muted);grid-column:1/-1}.hint{font-size:11px;color:var(--muted)}@media(max-width:600px){.search{grid-template-columns:1fr}.search button{height:42px}.results{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.body{padding:8px}.controls{display:none}}@media(max-width:380px){.results{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<header class="top"><div><div class="brand">TubeScout</div><div class="sub">Age-restricted videos only</div></div><div class="hint">Results stay cached while you refine them</div></header>
<form id="form"><div class="search"><input id="q" name="query" autocomplete="off" placeholder="Search YouTube…"><button id="go">Search</button></div><div class="bar"><span class="pill">18+ only</span><span id="found">Ready</span><div class="controls"><select id="order"><option value="relevance">Relevance</option><option value="date">Newest</option><option value="viewCount">Views</option></select><select id="duration"><option value="all">Any length</option><option value="short">Under 10 min</option><option value="medium">10–30 min</option><option value="long">Over 30 min</option></select></div></div></form>
<div id="results" class="results"><div class="empty">Search for age-restricted YouTube videos.</div></div></div>
<script>
const form=document.querySelector('#form'),q=document.querySelector('#q'),go=document.querySelector('#go'),order=document.querySelector('#order'),duration=document.querySelector('#duration'),results=document.querySelector('#results'),found=document.querySelector('#found');let latest=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(){let a=latest.slice();const d=duration.value;if(d==='short')a=a.filter(v=>v.seconds<600);if(d==='medium')a=a.filter(v=>v.seconds>=600&&v.seconds<=1800);if(d==='long')a=a.filter(v=>v.seconds>1800);found.textContent=`${a.length} result${a.length===1?'':'s'}`;results.innerHTML=a.length?a.map(v=>`<article class="card"><a class="thumb" href="${v.url}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(v.thumb)}" alt=""><span class="duration">${esc(v.duration)}</span></a><div class="body"><div class="title">${esc(v.title)}</div><div class="meta">${esc(v.channel)} · ${Number(v.views).toLocaleString()} views · ${esc(v.published)}</div></div></article>`).join(''):`<div class="empty">No age-restricted videos matched those filters.</div>`}
form.addEventListener('submit',async e=>{e.preventDefault();const query=q.value.trim();if(!query)return;go.disabled=true;found.textContent='Searching…';try{const body=new URLSearchParams({query,order:order.value,pages:'3',published_after:'',published_before:''});const r=await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});const data=await r.json();if(!r.ok)throw new Error(data.error||'Search failed');latest=data.results||[];render()}catch(err){found.textContent=err.message;results.innerHTML='<div class="empty">Search failed. Check the API key or try again.</div>'}finally{go.disabled=false}});
duration.addEventListener('change',render);order.addEventListener('change',()=>{if(latest.length){latest=[...latest].sort((a,b)=>order.value==='date'?b.published.localeCompare(a.published):order.value==='viewCount'?b.views-a.views:(b.score||0)-(a.score||0));render()}});
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX


@app.post("/api/search")
def api_search(
    query: str = Form(""),
    order: str = Form("relevance"),
    pages: int = Form(3),
    published_after: str = Form(""),
    published_before: str = Form(""),
):
    query = re.sub(r"\s+", " ", query.strip())
    if not query:
        return JSONResponse({"error": "Enter a search term."}, status_code=400)
    params = {"query": query.casefold(), "order": order, "pages": max(1, min(5, pages)), "published_after": published_after, "published_before": published_before, "age_only": True}
    key = cache_key(params)
    cached = load_cache(key)
    if cached is not None:
        return {"results": cached, "cached": True}
    try:
        results = search_with_keys(query, order, params["pages"], published_after, published_before)
        save_cache(key, results)
        return {"results": results, "cached": False}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "age_restricted_only": True, "api_keys_configured": bool(API_KEYS)}
