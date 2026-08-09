# Copyright (c) 2026 juhan20500-create. All rights reserved.
# 개인 사용만 허용. 재배포·공유·판매 금지. 자세한 내용은 LICENSE 참고.
# Personal use only. Redistribution prohibited. See LICENSE.
import json
import os
import subprocess
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from threading import Timer

from flask import Flask, request, jsonify, Response

PORT = 5054
YT_DLP_PATH = "yt-dlp"
CHROME_BROWSER = "chrome"
CHROME_PROFILE = os.environ.get("YTDLP_CHROME_PROFILE", "Profile 2")
PER_CHANNEL = 30         # 채널당 최근 쇼츠 표본 수 (조회수순 정렬해 인기순처럼 봄)
MIN_VIEWS = 500_000      # 이 조회수 미만 쇼츠는 결과에서 제외
FETCH_TIMEOUT = 60
DELAY_BETWEEN = 2.0

base_dir = os.path.dirname(os.path.abspath(__file__))
channels_path = os.path.join(base_dir, "channels.json")
error_log_path = os.path.join(base_dir, "error.log")

app = Flask(__name__)

# 처음 실행 시 미리 넣어둘 채널 핸들 (사용자 제공 목록에서 @/공백 정리·중복제거)
# 처음 실행할 때 넣어 둘 채널. 비어 있으면 빈 목록으로 시작한다.
# 쓰는 사람이 화면에서 직접 채널을 추가하면 lists.json 에 저장된다.
SEED_RAW = ""


def log_error(context):
    with open(error_log_path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {context} ---\n")
        f.write(traceback.format_exc())


def normalize_handle(name):
    name = name.strip().lstrip("@").strip()
    return name


lists_path = os.path.join(base_dir, "lists.json")
DEFAULT_LIST_NAME = "기본 목록"      # 처음 만들어지는 목록 이름. 화면에서 바꿀 수 있다.


def _seed_channels():
    seen, out = set(), []
    for raw in SEED_RAW.split():
        h = normalize_handle(raw)
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def load_lists():
    try:
        with open(lists_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("lists"):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # 최초 실행: 기존 channels.json이 있으면 그 채널들을, 없으면 시드를 기본 목록으로 이관
    try:
        with open(channels_path, "r", encoding="utf-8") as f:
            chs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        chs = _seed_channels()
    data = {"active": DEFAULT_LIST_NAME, "lists": {DEFAULT_LIST_NAME: chs}}
    save_lists(data)
    return data


def save_lists(data):
    with open(lists_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_channels():
    data = load_lists()
    return data["lists"].get(data["active"], [])


def save_channels(channels):
    data = load_lists()
    data["lists"][data["active"]] = channels
    save_lists(data)


def fetch_channel(handle, limit=PER_CHANNEL):
    """채널의 쇼츠 탭에서 영상 메타데이터를 가져온다. limit=None이면 전체(역대)."""
    url = f"https://www.youtube.com/@{handle}/shorts"
    cmd = [
        YT_DLP_PATH,
        "--cookies-from-browser", f"{CHROME_BROWSER}:{CHROME_PROFILE}",
        "--flat-playlist",
        "--dump-json",
        "--extractor-args", "youtubetab:skip=authcheck",
    ]
    if limit:  # None/0이면 전체 다 받음
        cmd += ["--playlist-end", str(limit)]
    cmd.append(url)
    videos = []
    error = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = d.get("id")
            if not vid:
                continue
            views = d.get("view_count") or 0
            if views < MIN_VIEWS:  # 기준 미만은 아예 제외
                continue
            videos.append({
                "id": vid,
                "title": d.get("title") or "(제목 없음)",
                "views": views,
                "channel": handle,
                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid}",
            })
        videos.sort(key=lambda v: v["views"], reverse=True)  # 조회수순(인기순)
        if not videos and proc.returncode != 0:
            error = "채널을 찾을 수 없음 (핸들 확인 필요)"
    except subprocess.TimeoutExpired:
        error = "시간 초과"
    except FileNotFoundError:
        error = "yt-dlp 없음"
    return videos, error


INDEX_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📡%3C/text%3E%3C/svg%3E">
<title>채널 모니터</title>
<style>
  :root{
    --bg:#0d0f14; --panel:#16191f; --elev:#1d2129; --line:#262b33;
    --txt:#e8eaf0; --muted:#949aa6; --accent:#7c5cff; --accent2:#6366f1;
    --r:14px; --ctrl:10px; --danger:#ff6b6b;
  }
  *{ box-sizing:border-box; }
  body{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg); color:var(--txt); margin:0; padding:0;
    background-image:radial-gradient(1000px 520px at 100% -10%, rgba(124,92,255,.10), transparent 60%); }
  .wrap{ max-width:1280px; margin:0 auto; padding:36px 32px 64px; }
  header{ display:flex; align-items:center; gap:14px; margin-bottom:6px; }
  .logo{ width:44px; height:44px; border-radius:12px; display:grid; place-items:center;
    font-size:22px; background:linear-gradient(135deg,var(--accent),var(--accent2));
    box-shadow:0 6px 20px rgba(124,92,255,.35); }
  h1{ font-size:24px; font-weight:750; margin:0; letter-spacing:-.02em; }
  p.sub{ color:var(--muted); margin:10px 0 22px; font-size:14px; }
  #bar{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:16px;
    background:var(--panel); border:1px solid var(--line); border-radius:var(--r); padding:14px; }
  #chInput{ background:var(--elev); border:1px solid var(--line); color:var(--txt);
    border-radius:var(--ctrl); padding:10px 13px; font-size:14px; width:220px; outline:none; transition:.15s; }
  #chInput:focus{ border-color:var(--accent); box-shadow:0 0 0 3px rgba(124,92,255,.18); }
  button{ color:#fff; border:none; padding:10px 16px; border-radius:var(--ctrl); font-size:14px;
    font-weight:650; cursor:pointer; transition:.15s ease;
    background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 4px 14px rgba(124,92,255,.28); }
  button:hover{ filter:brightness(1.08); }
  button:disabled{ opacity:.5; cursor:default; box-shadow:none; }
  button.ghost{ background:var(--elev); color:var(--txt); border:1px solid var(--line); box-shadow:none; }
  select#depthSel{ background:var(--elev); border:1px solid var(--line); color:var(--txt);
    border-radius:var(--ctrl); padding:10px 12px; font-size:14px; cursor:pointer; }
  #searchWrap{ flex:1; display:flex; justify-content:center; gap:8px; min-width:0; }
  #searchInput,#excludeInput{ background:var(--elev); border:1px solid var(--line); color:var(--txt);
    border-radius:var(--ctrl); padding:10px 13px; font-size:14px; width:100%; max-width:340px;
    outline:none; transition:.15s; }
  #searchInput:focus,#excludeInput:focus{ border-color:var(--accent); box-shadow:0 0 0 3px rgba(124,92,255,.18); }
  /* 제외는 걸러내는 칸이라 눈에 덜 띄게 두고, 값이 있을 때만 붉게 표시한다 */
  #excludeInput{ max-width:260px; }
  #excludeInput.on{ border-color:var(--danger); }
  .tabbtn{ background:var(--elev); color:var(--muted); border:1px solid var(--line); box-shadow:none; }
  .tabbtn.active{ background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; border-color:transparent; }
  .layout{ display:block; }
  .sidebar{ background:var(--panel); border-right:1px solid var(--line);
    padding:16px; position:fixed; top:0; left:0; height:100vh; width:280px; z-index:50;
    overflow-y:auto; transform:translateX(-102%); transition:transform .22s ease;
    box-shadow:6px 0 28px rgba(0,0,0,.45); }
  .sidebar.open{ transform:translateX(0); }
  #sideBackdrop{ position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:40;
    opacity:0; pointer-events:none; transition:opacity .22s ease; }
  #sideBackdrop.open{ opacity:1; pointer-events:auto; }
  .listrow{ display:flex; gap:6px; margin-bottom:10px; }
  .listrow #listSel{ flex:1; min-width:0; background:var(--elev); border:1px solid var(--line);
    color:var(--txt); border-radius:var(--ctrl); padding:9px 10px; font-size:13px; font-weight:600; cursor:pointer; }
  .listrow button{ padding:9px 11px; font-size:14px; }
  .addrow{ display:flex; gap:8px; margin-bottom:12px; }
  .addrow #chInput{ flex:1; width:auto; min-width:0; }
  /* 채널 추가 결과 알림 */
  .chmsg{ font-size:12.5px; line-height:1.45; margin:6px 2px 0; min-height:1px; word-break:break-all; }
  .chmsg.bad{ color:var(--danger); font-weight:600; }
  .chmsg.ok{ color:var(--muted); }
  /* 실패하면 입력칸도 빨갛게 해서 바로 눈에 띄게 한다 */
  #chInput.bad{ border-color:var(--danger); box-shadow:0 0 0 3px rgba(255,107,107,.18); }
  .addrow button{ padding:10px 13px; }
  .side-label{ font-size:12px; color:var(--muted); font-weight:600; margin:0 2px 8px; }
  #channels{ display:flex; flex-direction:column; gap:6px; max-height:66vh; overflow-y:auto; padding-right:4px; }
  #channels::-webkit-scrollbar{ width:8px; }
  #channels::-webkit-scrollbar-thumb{ background:var(--line); border-radius:999px; }
  #channels::-webkit-scrollbar-thumb:hover{ background:#39414f; }
  .chip{ width:100%; justify-content:space-between; background:var(--elev); border:1px solid var(--line);
    border-radius:var(--ctrl); padding:8px 10px 8px 12px; font-size:13px; display:flex; align-items:center; gap:6px; }
  #channels{ max-height:none; }
  .chip button{ background:none; color:var(--muted); border:none; cursor:pointer; font-size:14px; padding:0; box-shadow:none; }
  .chip button:hover{ color:var(--danger); }
  /* 핸들이 잘못돼 가져오지 못한 채널 — 직접 고치도록 눈에 띄게 둔다 */
  .chip.bad{ color:var(--danger); border-color:var(--danger);
    background:rgba(255,107,107,.10); font-weight:600; }
  #badCount{ font-size:12px; color:var(--danger); line-height:1.45; margin:6px 2px 0; }
  .linkbtn{ float:right; background:none; border:none; box-shadow:none; padding:0;
    color:var(--muted); font-size:12px; text-decoration:underline; cursor:pointer; }
  .linkbtn:hover{ color:var(--accent); }
  #status{ color:var(--accent); font-size:14px; margin-bottom:16px; min-height:18px; font-weight:600; }
  .chanblock{ margin-bottom:30px; }
  .chanhead{ font-size:16px; font-weight:700; margin-bottom:12px; display:flex; align-items:center; gap:10px; }
  .chanhead .stat{ font-size:12px; color:var(--muted); font-weight:500;
    background:var(--elev); border:1px solid var(--line); padding:2px 9px; border-radius:999px; }
  .chanhead a{ color:var(--accent); text-decoration:none; font-size:12px; }
  .row{ display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:16px; }
  .card{ background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
    overflow:hidden; text-decoration:none; color:inherit; display:block; transition:.16s ease; }
  .card:hover{ border-color:var(--accent); transform:translateY(-2px); box-shadow:0 12px 28px rgba(0,0,0,.4); }
  .thumb{ position:relative; width:100%; aspect-ratio:16/9; background:#000; }
  .thumb img{ width:100%; height:100%; object-fit:cover; }
  .analyzeBtn{ position:absolute; right:8px; bottom:8px; z-index:2;
    background:rgba(13,15,20,.82); color:var(--txt); border:1px solid var(--line);
    border-radius:8px; padding:5px 10px; font-size:11.5px; font-weight:700; cursor:pointer;
    backdrop-filter:blur(4px); box-shadow:none; opacity:0; transition:.15s ease; }
  .card:hover .analyzeBtn{ opacity:1; }
  .analyzeBtn:hover{ border-color:var(--accent); color:#fff; }
  .analyzeBtn.queued{ opacity:1; background:rgba(124,92,255,.9); border-color:transparent; color:#fff; }
  #queueCount{ font-size:12.5px; color:var(--accent); font-weight:700; }
  .meta{ padding:10px 12px 13px; }
  .title{ font-size:13px; font-weight:600; line-height:1.35; margin-bottom:6px;
           display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .sub2{ font-size:12px; color:var(--muted); }
  table{ border-collapse:collapse; width:100%; max-width:680px;
    background:var(--panel); border:1px solid var(--line); border-radius:var(--r); overflow:hidden; }
  th, td{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--line); font-size:13px; }
  tr:last-child td{ border-bottom:none; }
  th{ color:var(--muted); cursor:pointer; user-select:none; background:var(--elev); }
  th:hover{ color:var(--txt); }
  td.num{ text-align:right; font-variant-numeric:tabular-nums; }
  .err{ color:var(--danger); font-size:12px; }
</style>
</head>
<body>
 <div class="wrap">
  <header>
    <div class="logo">📡</div>
    <h1>채널 모니터</h1>
  </header>
  <p class="sub">등록한 채널들이 요즘 올리는 쇼츠를 한곳에서 보고 비교합니다. · 완전 로컬, yt-dlp</p>

  <div id="bar">
    <button class="ghost" id="sideToggle" title="채널 목록 열기">☰ 채널</button>
    <select id="depthSel">
      <option value="30" selected>최신 트렌드 분석</option>
      <option value="all">전체 분석</option>
    </select>
    <button id="fetchBtn">가져오기</button>
    <span id="queueCount"></span>
    <span id="searchWrap">
      <input id="searchInput" placeholder="가져온 결과에서 검색 (제목 · 채널)" />
      <input id="excludeInput" placeholder="제외할 단어 (쉼표로 여러 개)" />
    </span>
    <button class="tabbtn active" id="tabTop">전체 인기순</button>
    <button class="tabbtn" id="tabCompare">채널 비교표</button>
  </div>

  <div id="sideBackdrop"></div>
  <div class="layout">
    <aside class="sidebar">
      <div class="listrow">
        <select id="listSel"></select>
        <button id="newListBtn" class="ghost" title="새 목록">＋</button>
        <button id="delListBtn" class="ghost" title="이 목록 삭제">🗑</button>
      </div>
      <div class="addrow">
        <input id="chInput" placeholder="채널 핸들 추가" />
        <button id="addBtn">추가</button>
      </div>
      <div id="chMsg" class="chmsg"></div>
      <div class="side-label">등록 채널
        <button id="verifyBtn" class="linkbtn">채널 검사</button>
      </div>
      <div id="badCount"></div>
      <div id="channels"></div>
    </aside>
    <main>
      <div id="status"></div>
      <div id="view"></div>
    </main>
  </div>

<script>
// 채널 목록 서랍 열고닫기
const sideEl = document.querySelector('.sidebar');
const sideBackdrop = document.getElementById('sideBackdrop');
function toggleSide(open){
  const on = open === undefined ? !sideEl.classList.contains('open') : open;
  sideEl.classList.toggle('open', on);
  sideBackdrop.classList.toggle('open', on);
}
document.getElementById('sideToggle').onclick = ()=>toggleSide();
sideBackdrop.onclick = ()=>toggleSide(false);
document.addEventListener('keydown', e=>{ if(e.key==='Escape') toggleSide(false); });

const chInput = document.getElementById('chInput');
const addBtn = document.getElementById('addBtn');
const fetchBtn = document.getElementById('fetchBtn');
const channelsEl = document.getElementById('channels');
const status = document.getElementById('status');
const viewEl = document.getElementById('view');
const tabTop = document.getElementById('tabTop');
const tabCompare = document.getElementById('tabCompare');

let channels = [];
let data = { results: [] };   // [{channel, videos, error}]
let tab = 'top';
let compareSortKey = 'avg';
let query = '';
let exclude = localStorage.getItem('chanmon_exclude') || '';   // 제외 단어는 다음에도 그대로 쓴다

function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtViews(v){
  if (v >= 100000000) return (v/100000000).toFixed(1)+'억';
  if (v >= 10000) return Math.round(v/10000)+'만';
  return (v||0).toLocaleString();
}

// 가져오기에서 실패한 채널. 핸들이 틀렸을 가능성이 커서 빨갛게 표시한다.
let badChannels = JSON.parse(localStorage.getItem('chanmon_bad') || '{}');

function renderChannels() {
  channelsEl.innerHTML = channels.map(c => {
    const why = badChannels[c.toLowerCase()];
    const cls = why ? 'chip bad' : 'chip';
    const tip = why ? ` title="${escapeHtml(why)} — 핸들을 고쳐 주세요"` : '';
    return `<span class="${cls}"${tip}>${escapeHtml(c)}<button data-c="${escapeHtml(c)}">×</button></span>`;
  }).join('');
  channelsEl.querySelectorAll('button').forEach(b => b.onclick = () => removeChannel(b.dataset.c));
  const n = channels.filter(c => badChannels[c.toLowerCase()]).length;
  const warn = document.getElementById('badCount');
  // 검사 직후에는 그쪽 안내를 남겨 둔다
  if (warn && n) warn.textContent = `빨간 채널 ${n}개는 핸들이 잘못됐습니다. ×로 지우고 다시 추가하세요`;
}

// 등록된 채널이 실제로 있는지 한꺼번에 검사한다. 가져오기를 돌리지 않아도 된다.
function verifyChannels(){
  const btn = document.getElementById('verifyBtn');
  const warn = document.getElementById('badCount');
  btn.disabled = true;
  const old = btn.textContent; btn.textContent = '검사 중…';
  warn.textContent = `채널 ${channels.length}개를 확인하고 있습니다…`;
  fetch('/verify', {method:'POST'})
    .then(r=>r.json())
    .then(d=>{
      badChannels = {};
      Object.entries(d.bad || {}).forEach(([k,v])=>{ badChannels[k.toLowerCase()] = v; });
      localStorage.setItem('chanmon_bad', JSON.stringify(badChannels));
      renderChannels();
      const n = Object.keys(d.bad || {}).length;
      warn.textContent = n
        ? `빨간 채널 ${n}개는 핸들이 잘못됐습니다. ×로 지우고 다시 추가하세요`
        : `채널 ${d.checked}개 모두 정상입니다`;
    })
    .catch(e=>{ warn.textContent = '검사 실패: ' + e.message; })
    .finally(()=>{ btn.disabled = false; btn.textContent = old; });
}

// 가져오기 결과를 보고 어느 채널이 잘못됐는지 기록한다.
function markBadChannels(results){
  (results || []).forEach(r => {
    const k = (r.channel || '').toLowerCase();
    if (!k) return;
    if (r.error) badChannels[k] = r.error;
    else delete badChannels[k];        // 이제 잘 되면 표시를 지운다
  });
  localStorage.setItem('chanmon_bad', JSON.stringify(badChannels));
  renderChannels();
}
function loadChannels(){ fetch('/channels').then(r=>r.json()).then(d=>{channels=d.channels; renderChannels();}); }

const listSel = document.getElementById('listSel');
const newListBtn = document.getElementById('newListBtn');
const delListBtn = document.getElementById('delListBtn');
function fillLists(d){
  listSel.innerHTML = d.names.map(n=>`<option value="${escapeHtml(n)}"${n===d.active?' selected':''}>${escapeHtml(n)}</option>`).join('');
}
function loadLists(){ fetch('/lists').then(r=>r.json()).then(d=>{ fillLists(d); loadChannels(); }); }
listSel.onchange = ()=>{
  fetch('/lists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'select',name:listSel.value})})
    .then(r=>r.json()).then(d=>{ fillLists(d); data={results:[]}; render(); loadChannels(); });
};
newListBtn.onclick = ()=>{
  const name = prompt('새 목록 이름:'); if(!name || !name.trim()) return;
  fetch('/lists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'create',name})})
    .then(r=>r.json()).then(d=>{ fillLists(d); data={results:[]}; render(); loadChannels(); });
};
delListBtn.onclick = ()=>{
  if(!confirm(`목록 "${listSel.value}" 삭제할까요? (채널 등록내용도 지워짐)`)) return;
  fetch('/lists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',name:listSel.value})})
    .then(r=>r.json()).then(d=>{ fillLists(d); data={results:[]}; render(); loadChannels(); });
};
// 채널 추가 결과를 입력칸 아래에 알려준다.
function chMsg(text, bad){
  const el = document.getElementById('chMsg');
  el.textContent = text || '';
  el.className = bad ? 'chmsg bad' : 'chmsg ok';
  chInput.classList.toggle('bad', !!bad);
  if (text && !bad) setTimeout(()=>{ if(el.textContent===text) el.textContent=''; }, 2500);
}
function addChannel(){
  const c = chInput.value.trim(); if(!c) return;
  chMsg('유튜브에 있는 채널인지 확인 중…', false);
  addBtn.disabled = true;
  fetch('/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'add',channel:c})})
    .then(r=>r.json()).then(d=>{
      channels = d.channels; renderChannels();
      chMsg(d.msg || '', d.ok === false);
      if (d.ok !== false) chInput.value = '';   // 실패하면 고쳐 쓸 수 있게 남겨 둔다
    })
    .catch(e=>chMsg('확인 실패: '+e.message, true))
    .finally(()=>{ addBtn.disabled = false; });
}
function removeChannel(c){
  fetch('/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove',channel:c})})
    .then(r=>r.json()).then(d=>{channels=d.channels; renderChannels();});
}
addBtn.onclick = addChannel;
document.getElementById('verifyBtn').onclick = verifyChannels;
chInput.addEventListener('keydown', e=>{ if(e.key==='Enter') addChannel(); });
// 고쳐 쓰기 시작하면 빨간 표시를 지운다
chInput.addEventListener('input', ()=>{
  if (chInput.classList.contains('bad')) chMsg('', false);
});

function cardHtml(v){
  const meta = encodeURIComponent(JSON.stringify({url:v.url, title:v.title, channel:v.channel||'', views:v.views||0}));
  return `<a class="card" href="${v.url}" target="_blank">
    <div class="thumb"><img src="${v.thumb}" loading="lazy">
      <button class="analyzeBtn" data-meta="${meta}" title="분석 대기열에 추가">분석</button>
    </div>
    <div class="meta">
      <div class="title">${escapeHtml(v.title)}</div>
      <div class="sub2">조회 ${fmtViews(v.views)}</div>
    </div></a>`;
}

// 카드 안의 "분석" 버튼: 링크 이동 막고 대기열에 추가
document.addEventListener('click', e=>{
  const btn = e.target.closest('.analyzeBtn');
  if(!btn) return;
  e.preventDefault(); e.stopPropagation();
  let m; try{ m = JSON.parse(decodeURIComponent(btn.dataset.meta)); }catch(_){ return; }
  btn.disabled = true;
  fetch('/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(m)})
    .then(r=>r.json())
    .then(d=>{
      btn.textContent = d.dup ? '중복' : '담김 ✓';
      btn.classList.add('queued');
      updateQueueCount(d.count);
    })
    .catch(()=>{ btn.textContent='실패'; btn.disabled=false; });
});

function updateQueueCount(n){
  const el = document.getElementById('queueCount');
  if(el) el.textContent = n ? `분석 대기 ${n}개` : '';
}
fetch('/queue').then(r=>r.json()).then(d=>updateQueueCount(d.count)).catch(()=>{});

// 검색어로 걸러내고, 제외 단어가 든 영상은 빼낸다. 둘 다 비었으면 원본 그대로.
function filteredResults(){
  const q = query.trim().toLowerCase();
  // 쉼표로 여러 개. "먹방, 광고" 처럼 쓴다.
  const bad = exclude.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  if (!q && !bad.length) return data.results || [];
  const hit = v => {
    const t = (v.title||'').toLowerCase(), c = (v.channel||'').toLowerCase();
    if (bad.some(w => t.includes(w) || c.includes(w))) return false;
    return !q || t.includes(q) || c.includes(q);
  };
  return (data.results || [])
    .map(r => ({ ...r, videos: (r.videos||[]).filter(hit) }))
    .filter(r => r.videos.length);
}

function render(){
  const results = filteredResults();
  if (!results.length){
    const has = (data.results||[]).length;
    let msg = '';
    if (has && query.trim()) msg = `"${escapeHtml(query.trim())}" 검색 결과 없음`;
    else if (has && exclude.trim()) msg = '제외 단어로 모두 걸러졌습니다';
    viewEl.innerHTML = msg ? `<p class="sub2">${msg}</p>` : '';
    return;
  }

  if (tab === 'bychannel'){
    viewEl.innerHTML = results.map(r=>{
      const vids = r.videos || [];
      const avg = vids.length ? Math.round(vids.reduce((s,v)=>s+(v.views||0),0)/vids.length) : 0;
      const head = `<div class="chanhead">@${escapeHtml(r.channel)}
        <span class="stat">최근 ${vids.length}개 (조회수순) · 평균 ${fmtViews(avg)}</span>
        <a href="https://www.youtube.com/@${encodeURIComponent(r.channel)}/shorts" target="_blank">채널 열기</a>
        ${r.error?`<span class="err">${escapeHtml(r.error)}</span>`:''}</div>`;
      return `<div class="chanblock">${head}<div class="row">${vids.map(cardHtml).join('')}</div></div>`;
    }).join('');

  } else if (tab === 'top'){
    const all = [];
    results.forEach(r=>(r.videos||[]).forEach(v=>all.push(v)));
    all.sort((a,b)=>(b.views||0)-(a.views||0));
    viewEl.innerHTML = `<div class="row">${all.map(v=>
      `<a class="card" href="${v.url}" target="_blank">
        <div class="thumb"><img src="${v.thumb}" loading="lazy"></div>
        <div class="meta"><div class="title">${escapeHtml(v.title)}</div>
        <div class="sub2">@${escapeHtml(v.channel)} · 조회 ${fmtViews(v.views)}</div></div></a>`
    ).join('')}</div>`;

  } else {
    const rows = results.map(r=>{
      const vids = r.videos||[];
      const total = vids.reduce((s,v)=>s+(v.views||0),0);
      const avg = vids.length ? Math.round(total/vids.length) : 0;
      const mx = vids.reduce((m,v)=>Math.max(m,v.views||0),0);
      return { channel:r.channel, count:vids.length, avg, max:mx, total, error:r.error };
    });
    const key = compareSortKey;
    rows.sort((a,b)=>(b[key]||0)-(a[key]||0));
    viewEl.innerHTML = `<table>
      <tr><th data-k="channel">채널</th><th data-k="count" class="num">영상수</th>
      <th data-k="avg" class="num">평균조회</th><th data-k="max" class="num">최고조회</th></tr>
      ${rows.map(r=>`<tr>
        <td>@${escapeHtml(r.channel)}${r.error?` <span class="err">${escapeHtml(r.error)}</span>`:''}</td>
        <td class="num">${r.count}</td>
        <td class="num">${fmtViews(r.avg)}</td>
        <td class="num">${fmtViews(r.max)}</td></tr>`).join('')}
    </table>`;
    viewEl.querySelectorAll('th[data-k]').forEach(th=>{
      th.onclick = ()=>{ compareSortKey = th.dataset.k; render(); };
    });
  }
}

function setTab(t, btn){
  tab = t;
  [tabTop,tabCompare].forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  render();
}
const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('input', ()=>{ query = searchInput.value; render(); });
searchInput.addEventListener('keydown', e=>{
  if(e.key === 'Escape'){ searchInput.value=''; query=''; render(); }
});

// 제외할 단어 — 쉼표로 여러 개. 껐다 켜도 남는다.
const excludeInput = document.getElementById('excludeInput');
function applyExclude(){
  exclude = excludeInput.value;
  localStorage.setItem('chanmon_exclude', exclude);
  excludeInput.classList.toggle('on', !!exclude.trim());
  render();
}
excludeInput.value = exclude;
excludeInput.classList.toggle('on', !!exclude.trim());
excludeInput.addEventListener('input', applyExclude);
excludeInput.addEventListener('keydown', e=>{
  if(e.key === 'Escape'){ excludeInput.value=''; applyExclude(); }
});

tabTop.onclick = ()=>setTab('top', tabTop);
tabCompare.onclick = ()=>setTab('compare', tabCompare);

fetchBtn.onclick = async ()=>{
  if(!channels.length){ status.textContent='채널을 하나 이상 등록하세요.'; return; }
  data = { results: [] };
  render();
  fetchBtn.disabled = true;
  const total = channels.length;
  let done = 0;
  try {
    const depth = document.getElementById('depthSel').value;
    const resp = await fetch('/fetch_stream?depth=' + encodeURIComponent(depth));
    if(!resp.ok) throw new Error('서버 오류 ('+resp.status+')');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const {value, done:streamDone} = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx+1);
        if (!line) continue;
        let r; try { r = JSON.parse(line); } catch(e) { continue; }
        data.results.push(r);
        done++;
        const ok = data.results.filter(x=>!x.error).length;
        status.textContent = `가져오는 중... ${done}/${total}  (성공 ${ok})`;
        render();  // 도착할 때마다 즉시 반영 (전체 인기순은 조회수순 유지)
      }
    }
    const ok = data.results.filter(r=>!r.error).length;
    const bad = data.results.length - ok;
    status.textContent = `완료 · 성공 ${ok}/${data.results.length}개 채널`
      + (bad ? ` · 실패 ${bad}개는 왼쪽에 빨갛게 표시됨` : '');
    markBadChannels(data.results);   // 실패한 채널을 빨갛게
  } catch(err) {
    status.textContent = '오류: ' + err.message;
  } finally {
    fetchBtn.disabled = false;
  }
};

loadLists();
</script>
 </div>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


def channel_exists(handle):
    """유튜브에 그 채널이 있는지 확인한다. (있음, 메시지) 를 돌려준다.

    확인 자체가 안 되면(네트워크 문제 등) 막지 않고 통과시킨다.
    잘못된 이유로 추가를 못 하게 하는 편이 더 불편하기 때문이다.
    """
    cmd = [
        YT_DLP_PATH,
        "--cookies-from-browser", f"{CHROME_BROWSER}:{CHROME_PROFILE}",
        "--flat-playlist", "--dump-json", "--playlist-end", "1",
        "--extractor-args", "youtubetab:skip=authcheck",
        f"https://www.youtube.com/@{handle}/shorts",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return True, ""            # 느린 것뿐일 수 있다
    except FileNotFoundError:
        return True, ""            # yt-dlp 가 없으면 확인을 건너뛴다
    err = proc.stderr or ""
    if "Requested entity was not found" in err or "HTTP Error 404" in err:
        return False, f"@{handle} 채널을 찾을 수 없습니다. 핸들을 다시 확인하세요."
    if "This channel does not have a shorts tab" in err or "does not have a" in err:
        return False, f"@{handle} 에 쇼츠 탭이 없습니다."
    return True, ""


@app.route("/channels", methods=["GET", "POST"])
def channels_route():
    chs = load_channels()
    msg = ""
    ok = True
    if request.method == "POST":
        d = request.get_json()
        action = d.get("action")
        c = normalize_handle(d.get("channel") or "")
        if action == "add":
            if not c:
                ok, msg = False, "채널 핸들을 입력하세요."
            elif c.lower() in [x.lower() for x in chs]:
                ok, msg = False, f"@{c} 은(는) 이미 등록돼 있습니다."
            else:
                # 오타나 없는 채널을 걸러낸다
                exists, why = channel_exists(c)
                if not exists:
                    ok, msg = False, why
                else:
                    chs.append(c)
                    save_channels(chs)
                    msg = f"@{c} 추가됨"
        elif action == "remove":
            chs = [x for x in chs if x.lower() != c.lower()]
            save_channels(chs)
    return jsonify({"channels": chs, "ok": ok, "msg": msg})


@app.route("/verify", methods=["POST"])
def verify_channels():
    """등록된 채널이 실제로 있는지 한꺼번에 확인한다.

    영상은 안 받고 존재 여부만 보므로 가져오기보다 훨씬 빠르다.
    여러 개를 동시에 확인한다.
    """
    chs = load_channels()
    bad = {}

    def one(h):
        exists, why = channel_exists(h)
        return h, (None if exists else (why or "채널을 찾을 수 없음"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for h, why in pool.map(one, chs):
            if why:
                bad[h] = why
    return jsonify({"bad": bad, "checked": len(chs)})


# 분석 대기열을 쌓아 두는 곳. 쓰는 사람마다 홈 폴더 아래에 생긴다.
ANALYSIS_DIR = os.path.expanduser(os.environ.get("SHORTS_ANALYSIS_DIR", "~/shorts_analysis"))
QUEUE_PATH = os.path.join(ANALYSIS_DIR, "queue.jsonl")


def _queued_urls():
    urls = set()
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    urls.add(json.loads(line).get("url"))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return urls


@app.route("/queue", methods=["GET", "POST"])
def queue_route():
    """분석 대기열: 채널 모니터에서 고른 쇼츠를 분석용으로 쌓아둔다."""
    if request.method == "POST":
        d = request.get_json() or {}
        url = (d.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url 없음"}), 400
        if url in _queued_urls():
            return jsonify({"ok": True, "dup": True, "count": len(_queued_urls())})
        os.makedirs(ANALYSIS_DIR, exist_ok=True)
        row = {
            "url": url,
            "title": d.get("title") or "",
            "channel": d.get("channel") or "",
            "views": d.get("views") or 0,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return jsonify({"ok": True, "dup": False, "count": len(_queued_urls())})
    return jsonify({"count": len(_queued_urls()), "urls": sorted(_queued_urls())})


@app.route("/lists", methods=["GET", "POST"])
def lists_route():
    data = load_lists()
    if request.method == "POST":
        d = request.get_json()
        action = d.get("action")
        name = (d.get("name") or "").strip()
        if action == "select" and name in data["lists"]:
            data["active"] = name
            save_lists(data)
        elif action == "create" and name and name not in data["lists"]:
            data["lists"][name] = []
            data["active"] = name
            save_lists(data)
        elif action == "delete" and name in data["lists"] and len(data["lists"]) > 1:
            del data["lists"][name]
            if data["active"] == name:
                data["active"] = next(iter(data["lists"]))
            save_lists(data)
        data = load_lists()
    return jsonify({"active": data["active"], "names": list(data["lists"].keys())})


@app.route("/fetch_stream")
def fetch_stream():
    """채널 하나가 끝날 때마다 결과를 한 줄씩(NDJSON) 즉시 흘려보낸다."""
    chs = load_channels()
    depth_arg = request.args.get("depth", str(PER_CHANNEL))
    if depth_arg == "all":
        limit = None  # 전체(역대 인기순)
    else:
        try:
            limit = max(5, min(500, int(depth_arg)))
        except ValueError:
            limit = PER_CHANNEL

    def gen():
        for i, handle in enumerate(chs):
            if i > 0:
                time.sleep(DELAY_BETWEEN)
            try:
                videos, error = fetch_channel(handle, limit)
            except Exception as e:
                log_error(f"fetch {handle}")
                videos, error = [], f"{type(e).__name__}"
            yield json.dumps(
                {"channel": handle, "videos": videos, "error": error},
                ensure_ascii=False,
            ) + "\n"

    return Response(gen(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    (None if os.environ.get("NO_BROWSER") else Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start())
    app.run(port=PORT, debug=False, threaded=True)
