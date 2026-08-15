import asyncio
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

app = FastAPI(title="TubeScout", version="4.0")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS searches (cache_key TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    return con


def cache_key(params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def load_cache(key: str) -> list[dict[str, Any]] | None:
    with db() as con:
        row = con.execute(
            "SELECT payload FROM searches WHERE cache_key=? AND created_at>=?",
            (key, int(time.time()) - CACHE_TTL),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def save_cache(key: str, payload: list[dict[str, Any]]) -> None:
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO searches(cache_key,created_at,payload) VALUES(?,?,?)",
            (key, now, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        con.execute("DELETE FROM searches WHERE created_at<?", (now - CACHE_TTL * 2,))


def compact_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).casefold()


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
        "description": (snippet.get("description") or "")[:360],
        "url": f"https://www.youtube.com/watch?v={video.get('id', '')}",
        "tags": snippet.get("tags", [])[:20],
    }


def youtube_search(api_key: str, query: str, mode: str, order: str, pages: int, after: str, before: str) -> list[dict[str, Any]]:
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
        if mode == "past_live":
            params["eventType"] = "completed"
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
        batch = ids[start : start + 50]
        details = youtube.videos().list(
            part="snippet,statistics,contentDetails,liveStreamingDetails",
            id=",".join(batch),
            maxResults=50,
        ).execute()
        results.extend(normalize(item) for item in details.get("items", []))
    return results


def score_results(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    tokens = [t for t in re.findall(r"[\w]+", query.casefold()) if len(t) > 1]
    output = []
    for item in items:
        text = f"{item['title']} {item['description']} {' '.join(item.get('tags', []))}".casefold()
        title = item["title"].casefold()
        whole_phrase = 120 if query.casefold() in title else 0
        title_hits = sum(8 for token in tokens if re.search(rf"\b{re.escape(token)}\b", title))
        body_hits = sum(3 for token in tokens if re.search(rf"\b{re.escape(token)}\b", text))
        exact_words = sum(1 for token in tokens if re.search(rf"\b{re.escape(token)}\b", title))
        clone = dict(item)
        clone["score"] = whole_phrase + title_hits + body_hits + exact_words
        output.append(clone)
    return sorted(output, key=lambda x: (-x["score"], -x["views"], x["title"].casefold()))


INDEX = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d1117">
<title>TubeScout — Find the videos that matter</title>
<style>
:root{--bg:#080b10;--panel:#10151d;--panel2:#151c26;--line:#253142;--text:#eef3f8;--muted:#8e9aaa;--accent:#55d6be;--accent2:#6aa8ff;--danger:#ff7c8d;--shadow:0 18px 50px rgba(0,0,0,.28)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#15202b 0,#080b10 42%);color:var(--text);font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}a{color:inherit;text-decoration:none}
.wrap{width:min(1260px,calc(100% - 32px));margin:auto}.top{padding:44px 0 28px}.brand{display:flex;align-items:center;gap:12px}.mark{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#061016;font-weight:900;font-size:19px;box-shadow:0 12px 30px rgba(85,214,190,.18)}h1{margin:0;font-size:clamp(32px,6vw,56px);letter-spacing:-.04em;line-height:1}.tag{margin:12px 0 0;color:var(--muted);max-width:720px;font-size:16px}.hero{padding:18px 0 18px}.searchbar{display:grid;grid-template-columns:1fr auto;gap:10px;background:rgba(16,21,29,.82);border:1px solid var(--line);padding:10px;border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}input,select,button{font:inherit}.input,.select{width:100%;border:1px solid transparent;background:var(--panel2);color:var(--text);border-radius:11px;padding:13px 14px;outline:none}.input:focus,.select:focus{border-color:#42637c}.searchbtn{border:0;border-radius:11px;padding:0 20px;background:var(--accent);color:#061016;font-weight:800;cursor:pointer;min-width:104px}.searchbtn:disabled{opacity:.6;cursor:wait}.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 8px}.chip{display:flex;align-items:center;gap:7px;padding:8px 11px;border:1px solid var(--line);border-radius:999px;background:rgba(16,21,29,.8);color:var(--muted)}.chip input{accent-color:var(--accent)}.advanced{border:1px solid var(--line);background:rgba(16,21,29,.75);border-radius:16px;margin-top:10px}.advanced summary{padding:11px 13px;color:var(--muted);cursor:pointer}.advanced-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:0 13px 13px}.status{display:flex;justify-content:space-between;gap:12px;min-height:25px;margin:20px 0 12px;color:var(--muted)}.count{color:var(--text)}.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;padding-bottom:50px}.card{overflow:hidden;background:linear-gradient(180deg,#121923,#0e141c);border:1px solid var(--line);border-radius:18px;box-shadow:0 12px 36px rgba(0,0,0,.12);transition:.18s transform,.18s border-color}.card:hover{transform:translateY(-2px);border-color:#34465c}.thumb{display:block;position:relative;background:#05070a}.thumb img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}.badge{position:absolute;left:10px;bottom:10px;padding:4px 7px;border-radius:7px;background:rgba(3,6,9,.86);font-size:12px}.body{padding:14px}.title{font-weight:800;line-height:1.34;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.meta{margin-top:8px;color:var(--muted);font-size:12px}.desc{margin-top:9px;color:#b5c0cc;font-size:13px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.empty{padding:52px 20px;text-align:center;border:1px dashed var(--line);border-radius:18px;color:var(--muted)}.hint{font-size:12px;color:#748091}
@media(max-width:780px){.searchbar{grid-template-columns:1fr}.searchbtn{height:48px}.advanced-grid{grid-template-columns:1fr 1fr}.results{grid-template-columns:1fr}.top{padding-top:28px}}
@media(max-width:460px){.advanced-grid{grid-template-columns:1fr}.wrap{width:min(100% - 20px,1260px)}}
</style></head>
<body><div class="wrap">
<header class="top"><div class="brand"><div class="mark">TS</div><div><strong>TubeScout</strong><div class="hint">YouTube research, without the clutter.</div></div></div></header>
<section class="hero"><h1>Find the videos that matter.</h1><p class="tag">Search deeply, keep your results, and refine them instantly. TubeScout caches expensive searches so changing filters does not burn more API quota.</p></section>
<form id="searchForm"><div class="searchbar"><input class="input" id="query" name="query" autocomplete="off" placeholder="Try: 2026 F1 analysis, long interview, old live stream…"><button class="searchbtn" id="searchBtn">Search</button></div>
<div class="toolbar"><label class="chip"><input type="radio" name="mode" value="all" checked> Everything</label><label class="chip"><input type="radio" name="mode" value="past_live"> Past live streams</label><label class="chip"><input type="checkbox" id="instantSort"> Sort by relevance</label></div>
<details class="advanced"><summary>Search controls</summary><div class="advanced-grid"><select class="select" name="order"><option value="relevance">YouTube relevance</option><option value="date">Newest first</option><option value="viewCount">Most viewed</option></select><select class="select" name="pages"><option value="1">50 results</option><option value="2" selected>100 results</option><option value="3">150 results</option><option value="5">250 results</option></select><input class="input" name="published_after" type="date"><input class="input" name="published_before" type="date"></div></details>
</form>
<div class="status"><span id="status">Ready.</span><span class="count" id="count"></span></div>
<div id="results" class="results"><div class="empty">Search for something above. Your current results will stay on screen while a new search runs.</div></div>
<section class="advanced" style="margin-bottom:40px"><details><summary>Instant filters</summary><div class="advanced-grid" id="filters"><input class="input" id="minDuration" type="number" min="0" placeholder="Min duration (sec)"><input class="input" id="maxDuration" type="number" min="0" placeholder="Max duration (sec)"><input class="input" id="minViews" type="number" min="0" placeholder="Min views"><select class="select" id="kind"><option value="all">Any type</option><option value="video">Normal videos</option><option value="live">Livestream recordings</option><option value="age">Age restricted</option></select></div></details></section>
</div>
<script>
const form=document.querySelector('#searchForm'), resultsEl=document.querySelector('#results'), statusEl=document.querySelector('#status'), countEl=document.querySelector('#count'), btn=document.querySelector('#searchBtn');
let latest=[], busy=false;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const card=v=>`<article class="card"><a class="thumb" href="${v.url}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(v.thumb)}" alt=""><span class="badge">${esc(v.duration)}</span></a><div class="body"><div class="title">${esc(v.title)}</div><div class="meta">${esc(v.channel)} · ${Number(v.views).toLocaleString()} views · ${v.published || 'date unknown'}</div><div class="desc">${esc(v.description)}</div></div></article>`;
function localFilter(){const minD=+document.querySelector('#minDuration').value||0,maxD=+document.querySelector('#maxDuration').value||0,minV=+document.querySelector('#minViews').value||0,kind=document.querySelector('#kind').value;let shown=latest.filter(v=>v.seconds>=minD&&(!maxD||v.seconds<=maxD)&&v.views>=minV&&(kind==='all'||kind==='video'&&!v.live||kind==='live'&&v.live||kind==='age'&&v.age));resultsEl.innerHTML=shown.length?shown.map(card).join(''):`<div class="empty">No results match those filters. The original search stays cached, so changing these filters costs no API quota.</div>`;countEl.textContent=`${shown.length} shown · ${latest.length} collected`;}
['minDuration','maxDuration','minViews','kind'].forEach(id=>document.querySelector('#'+id).addEventListener('input',localFilter));
form.addEventListener('submit',async e=>{e.preventDefault();const q=document.querySelector('#query').value.trim();if(!q)return;busy=true;btn.disabled=true;statusEl.textContent='Searching YouTube…';const previous=resultsEl.innerHTML;try{const body=new FormData(form);const res=await fetch('/api/search',{method:'POST',body});const data=await res.json();if(!res.ok)throw new Error(data.error||'Search failed');latest=data.results||[];localFilter();statusEl.textContent=data.cached?'Loaded from cache.':'Fresh search complete.';countEl.textContent=`${data.total_returned} shown · ${data.total_collected} collected${data.cached?' · cached':''}`;}catch(err){resultsEl.innerHTML=previous;statusEl.textContent=err.message;}finally{busy=false;btn.disabled=false;}});
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(INDEX)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "configured": bool(API_KEYS), "cache_hours": CACHE_TTL // 3600}


@app.post("/api/search")
async def search(
    query: str = Form(...),
    mode: str = Form("all"),
    order: str = Form("relevance"),
    pages: int = Form(2),
    published_after: str = Form(""),
    published_before: str = Form(""),
):
    query = compact_query(query)
    if not query:
        return JSONResponse({"error": "Enter a search term."}, status_code=400)

    params = {
        "q": query,
        "mode": mode if mode in {"all", "past_live"} else "all",
        "order": order if order in {"relevance", "date", "viewCount"} else "relevance",
        "pages": max(1, min(5, pages)),
        "after": published_after or None,
        "before": published_before or None,
    }
    key = cache_key(params)
    data = load_cache(key)
    cached = data is not None

    if data is None:
        if not API_KEYS:
            return JSONResponse(
                {"error": "YouTube access is not configured on the server yet."},
                status_code=503,
            )
        last_error: Exception | None = None
        for api_key in API_KEYS:
            try:
                data = await asyncio.to_thread(
                    youtube_search,
                    api_key,
                    query,
                    params["mode"],
                    params["order"],
                    params["pages"],
                    params["after"],
                    params["before"],
                )
                save_cache(key, data)
                last_error = None
                break
            except HttpError as exc:
                last_error = exc
                continue
        if last_error is not None:
            return JSONResponse({"error": f"YouTube API error: {last_error}"}, status_code=502)

    ranked = score_results(data or [], query)
    return {
        "results": ranked,
        "total_collected": len(ranked),
        "total_returned": len(ranked),
        "cached": cached,
    }
