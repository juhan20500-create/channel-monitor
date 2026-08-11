import json
import os
import subprocess
import sys
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from threading import Timer

from flask import Flask, request, jsonify, Response

PORT = 5054


def open_browser(url):
    """크롬으로 연다. 크롬이 없으면 기본 브라우저로 연다.

    APP_BROWSER 환경변수로 바꿀 수 있다. (chrome / edge / default)
    """
    import os
    import shutil
    import subprocess
    import sys
    import webbrowser

    want = (os.environ.get("APP_BROWSER") or "chrome").strip().lower()
    if want == "default":
        webbrowser.open(url)
        return

    if sys.platform == "darwin":
        names = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}
        app = names.get(want)
        if app and os.path.isdir(f"/Applications/{app}.app"):
            subprocess.Popen(["open", "-a", app, url])
            return
    elif sys.platform == "win32":
        cands = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ],
            "edge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
        }.get(want, [])
        for p in cands:
            if os.path.exists(p):
                subprocess.Popen([p, url])
                return
    else:
        exe = shutil.which("google-chrome") or shutil.which("chromium")
        if want == "chrome" and exe:
            subprocess.Popen([exe, url])
            return

    webbrowser.open(url)   # 못 찾으면 기본 브라우저


def _find_yt_dlp():
    """yt-dlp 위치를 찾는다.

    exe 로 묶어 배포하면 실행파일 안에 yt-dlp 가 함께 들어 있다.
    그게 없으면 컴퓨터에 설치된 것을 쓴다.
    """
    import shutil
    import sys
    names = ("yt-dlp.exe", "yt-dlp") if os.name == "nt" else ("yt-dlp",)
    # 1) PyInstaller 로 묶였을 때 풀리는 임시 폴더
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for n in names:
            p = os.path.join(base, n)
            if os.path.exists(p):
                return p
    # 2) 실행파일과 같은 폴더
    here = os.path.dirname(os.path.abspath(sys.executable if base else __file__))
    for n in names:
        p = os.path.join(here, n)
        if os.path.exists(p):
            return p
    # 3) 컴퓨터에 설치된 것
    return shutil.which("yt-dlp") or "yt-dlp"


YT_DLP_PATH = _find_yt_dlp()
CHROME_BROWSER = "chrome"


def _chrome_user_data_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(base, "Google", "Chrome", "User Data")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return os.path.expanduser("~/.config/google-chrome")


def list_chrome_profiles():
    """이 컴퓨터의 크롬 프로필 목록. 크롬이 저장해 둔 표시 이름까지 함께 준다.

    폴더 이름(Default, Profile 1 …)만 보여주면 어느 계정인지 알 수 없어서,
    크롬의 Local State 에 있는 이름과 메일 주소를 같이 보여 준다.
    """
    base = _chrome_user_data_dir()
    if not os.path.isdir(base):
        return []
    names = {}
    try:
        with open(os.path.join(base, "Local State"), encoding="utf-8") as f:
            names = json.load(f).get("profile", {}).get("info_cache", {})
    except (OSError, json.JSONDecodeError):
        pass
    out = []
    for d in sorted(os.listdir(base)):
        if d != "Default" and not d.startswith("Profile "):
            continue
        if not os.path.isdir(os.path.join(base, d, "Network")) and \
           not os.path.exists(os.path.join(base, d, "Cookies")):
            continue                      # 쿠키가 없는 껍데기 폴더는 뺀다
        info = names.get(d) or {}
        label = info.get("name") or d
        mail = info.get("user_name") or ""
        out.append({"id": d, "label": label, "mail": mail})
    return out


def settings_path():
    return os.path.join(base_dir, "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(d):
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _pick_chrome_profile():
    """쓸 크롬 프로필을 정한다.

    순서: 환경변수 → 화면에서 고른 값 → 이 컴퓨터에 있는 첫 프로필.
    쓸 만한 게 없으면 None 을 돌려주고 쿠키 없이 조회한다.
    """
    want = os.environ.get("YTDLP_CHROME_PROFILE")
    if want:
        return want
    chosen = load_settings().get("chrome_profile")
    avail = [p["id"] for p in list_chrome_profiles()]
    if chosen and chosen in avail:
        return chosen
    return avail[0] if avail else None


def cookie_args():
    """yt-dlp 에 넘길 쿠키 옵션. 쓸 프로필이 없으면 아무것도 넘기지 않는다."""
    prof = _pick_chrome_profile()      # 화면에서 바꾸면 바로 반영되도록 매번 확인한다
    if not prof:
        return []
    return ["--cookies-from-browser", f"{CHROME_BROWSER}:{prof}"]


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


def classify_error(err, handle):
    """실패 원인을 구분한다.

    전에는 실패하면 무조건 "핸들이 잘못됐다"고 알렸다. 그러다 보니
    네트워크 문제나 유튜브 차단으로 실패한 것까지 멀쩡한 채널을 지우게 만들었다.
    핸들이 틀렸다고 단정하는 건 유튜브가 "없다"고 답한 경우뿐이다.
    """
    e = err.lower()
    # 404 는 그 채널이 없다는 뜻. 고쳐야 하는 문제다.
    if "requested entity was not found" in e or "http error 404" in e:
        return f"@{handle} 채널이 없습니다 — 핸들을 고치거나 지우세요", "missing"
    if "does not have a" in e and "shorts" in e:
        return "이 채널에는 쇼츠가 없습니다", "missing"
    # 아래는 채널 잘못이 아니라 그때그때 사정. 다시 하면 될 수 있다.
    if "sign in" in e or "cookies" in e or "bot" in e or "consent" in e:
        return "유튜브가 확인을 요구했습니다 — 아래에서 크롬 계정을 바꿔보세요", "temp"
    if "429" in e or "too many requests" in e:
        return "요청이 많아 잠시 막혔습니다 — 잠시 후 다시", "temp"
    if "unable to download" in e or "urlopen" in e or "timed out" in e or "network" in e:
        return "인터넷 연결 문제 — 잠시 후 다시", "temp"
    return "가져오지 못했습니다 — 잠시 후 다시", "temp"


def fetch_channel(handle, limit=PER_CHANNEL):
    """채널의 쇼츠 탭에서 영상 메타데이터를 가져온다. limit=None이면 전체(역대)."""
    url = f"https://www.youtube.com/@{handle}/shorts"
    cmd = [
        YT_DLP_PATH,
        *cookie_args(),
        "--flat-playlist",
        "--dump-json",
        "--extractor-args", "youtubetab:skip=authcheck",
    ]
    if limit:  # None/0이면 전체 다 받음
        cmd += ["--playlist-end", str(limit)]
    cmd.append(url)
    videos = []
    error = None
    kind = None
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
            error, kind = classify_error(proc.stderr or "", handle)
    except subprocess.TimeoutExpired:
        error, kind = "시간 초과 — 잠시 후 다시", "temp"
    except FileNotFoundError:
        error, kind = "yt-dlp 를 찾을 수 없습니다 (설치 필요)", "temp"
    return videos, error, kind


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
  /* 가져오지 못한 채널.
     빨강 = 없는 채널(고쳐야 함), 노랑 = 잠깐 실패(다시 하면 될 수 있음) */
  .chip .chname{ display:flex; flex-direction:column; gap:2px; min-width:0; }
  .chip .why{ font-style:normal; font-size:11px; font-weight:400; opacity:.9;
    line-height:1.35; white-space:normal; }
  .chip.bad{ color:var(--danger); border-color:var(--danger);
    background:rgba(255,107,107,.10); font-weight:600; align-items:flex-start; }
  .chip.warn{ color:#ffb454; border-color:#ffb454;
    background:rgba(255,180,84,.10); font-weight:600; align-items:flex-start; }
  #badCount{ font-size:12px; color:var(--danger); line-height:1.45; margin:6px 2px 0; }
  /* 유튜브 로그인(크롬 프로필) 고르기 */
  .prof-help{ font-size:12px; color:var(--muted); line-height:1.5; margin:4px 2px 8px; }
  #profSel{ width:100%; background:var(--elev); border:1px solid var(--line); color:var(--txt);
    border-radius:var(--ctrl); padding:9px 10px; font-size:13px; outline:none; }
  #profSel:focus{ border-color:var(--accent); }
  .linkbtn{ float:right; background:var(--elev); border:1px solid var(--line); box-shadow:none;
    padding:4px 10px; border-radius:999px; color:var(--txt); font-size:11.5px;
    font-weight:600; cursor:pointer; margin-top:-3px; }
  .linkbtn:hover{ border-color:var(--accent); color:var(--accent); }
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

      <div class="side-label" style="margin-top:18px">유튜브 로그인
        <button id="profToggle" class="linkbtn">설정</button>
      </div>
      <div id="profBox" hidden>
        <div class="prof-help">잘 안 가져와지면, 유튜브에 로그인해 둔 크롬 계정을 고르세요.</div>
        <select id="profSel"></select>
        <div id="profMsg" class="chmsg"></div>
      </div>
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
    const b = badChannels[c.toLowerCase()];
    const kind = b && (b.kind || 'temp');
    const cls = 'chip' + (kind === 'missing' ? ' bad' : kind ? ' warn' : '');
    const reason = b ? `<em class="why">${escapeHtml(b.msg || b)}</em>` : '';
    return `<span class="${cls}"><span class="chname">${escapeHtml(c)}${reason}</span>` +
           `<button data-c="${escapeHtml(c)}">×</button></span>`;
  }).join('');
  channelsEl.querySelectorAll('button').forEach(b => b.onclick = () => removeChannel(b.dataset.c));
  const bads = channels.map(c => badChannels[c.toLowerCase()]).filter(Boolean);
  const miss = bads.filter(b => (b.kind || 'temp') === 'missing').length;
  const temp = bads.length - miss;
  const warn = document.getElementById('badCount');
  if (warn && bads.length) {
    const parts = [];
    if (miss) parts.push(`빨강 ${miss}개는 없는 채널 — 고치거나 지우세요`);
    if (temp) parts.push(`노랑 ${temp}개는 잠깐 실패 — 다시 해보세요`);
    warn.textContent = parts.join(' · ');
  }
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
      Object.entries(d.bad || {}).forEach(([k,v])=>{
        badChannels[k.toLowerCase()] = (typeof v === 'string') ? {msg:v, kind:'temp'} : v;
      });
      localStorage.setItem('chanmon_bad', JSON.stringify(badChannels));
      renderChannels();
      const n = Object.keys(d.bad || {}).length;
      warn.textContent = n
        ? `${n}개를 가져오지 못했습니다`
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
    if (r.error) badChannels[k] = {msg: r.error, kind: r.kind || 'temp'};
    else delete badChannels[k];        // 이제 잘 되면 표시를 지운다
  });
  localStorage.setItem('chanmon_bad', JSON.stringify(badChannels));
  renderChannels();
}
function loadChannels(){
  fetch('/channels').then(r=>r.json()).then(d=>{
    channels = d.channels; renderChannels();
    // 아직 한 번도 검사하지 않은 목록이면 알아서 확인해 준다.
    // (검사 결과는 남아 있으므로 다시 열 때는 그대로 쓴다)
    const key = 'chanmon_checked_' + (channels.join(',') || '-');
    if (channels.length && !sessionStorage.getItem(key)) {
      sessionStorage.setItem(key, '1');
      verifyChannels();
    }
  });
}

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

// ---- 유튜브 로그인(크롬 프로필) 고르기 ----
const profBox = document.getElementById('profBox');
const profSel = document.getElementById('profSel');
const profMsg = document.getElementById('profMsg');

function loadProfiles(){
  fetch('/profiles').then(r=>r.json()).then(d=>{
    const list = d.profiles || [];
    if(!list.length){
      profSel.innerHTML = '<option value="">크롬을 찾지 못했습니다</option>';
      profSel.disabled = true;
      profMsg.className = 'chmsg ok';
      profMsg.textContent = '쿠키 없이 가져옵니다. 유튜브가 막으면 크롬을 설치하세요.';
      return;
    }
    profSel.disabled = !!d.locked;
    profSel.innerHTML = list.map(p=>{
      const who = p.mail ? `${p.label} (${p.mail})` : p.label;
      return `<option value="${p.id}"${p.id===d.current?' selected':''}>${escapeHtml(who)}</option>`;
    }).join('');
    profMsg.className = 'chmsg ok';
    profMsg.textContent = d.locked ? '환경변수로 고정돼 있어 바꿀 수 없습니다' : '';
  }).catch(()=>{});
}
profSel.onchange = () => {
  fetch('/profiles', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({profile: profSel.value})})
    .then(r=>r.json()).then(()=>{
      profMsg.className = 'chmsg ok';
      profMsg.textContent = '바꿨습니다. 가져오기를 다시 해보세요.';
      setTimeout(()=>{ profMsg.textContent=''; }, 3000);
    })
    .catch(()=>{ profMsg.className='chmsg bad'; profMsg.textContent='바꾸지 못했습니다'; });
};
document.getElementById('profToggle').onclick = () => {
  profBox.hidden = !profBox.hidden;
  document.getElementById('profToggle').textContent = profBox.hidden ? '설정' : '접기';
  if(!profBox.hidden) loadProfiles();
};
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
        *cookie_args(),
        "--flat-playlist", "--dump-json", "--playlist-end", "1",
        "--extractor-args", "youtubetab:skip=authcheck",
        f"https://www.youtube.com/@{handle}/shorts",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return True, "", None      # 느린 것뿐일 수 있다
    except FileNotFoundError:
        return True, "", None      # yt-dlp 가 없으면 확인을 건너뛴다
    err = proc.stderr or ""
    if "Requested entity was not found" in err or "HTTP Error 404" in err:
        return False, f"@{handle} 채널이 없습니다 — 핸들을 고치거나 지우세요", "missing"
    if "This channel does not have a shorts tab" in err or "does not have a" in err:
        return False, f"@{handle} 에 쇼츠 탭이 없습니다", "missing"
    return True, "", None


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
                exists, why, _ = channel_exists(c)
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


@app.route("/profiles", methods=["GET", "POST"])
def profiles_route():
    """쓸 크롬 프로필을 보여 주고 바꾼다.

    유튜브가 로그인 안 된 접근을 자주 막는다. 로그인해 둔 프로필을 고르면
    조회가 훨씬 잘 된다.
    """
    if request.method == "POST":
        d = request.get_json() or {}
        chosen = (d.get("profile") or "").strip()
        s = load_settings()
        if chosen:
            s["chrome_profile"] = chosen
        else:
            s.pop("chrome_profile", None)      # 빈 값이면 자동 선택으로 되돌린다
        save_settings(s)
    return jsonify({
        "profiles": list_chrome_profiles(),
        "current": _pick_chrome_profile(),
        "locked": bool(os.environ.get("YTDLP_CHROME_PROFILE")),
    })


@app.route("/verify", methods=["POST"])
def verify_channels():
    """등록된 채널이 실제로 있는지 한꺼번에 확인한다.

    영상은 안 받고 존재 여부만 보므로 가져오기보다 훨씬 빠르다.
    여러 개를 동시에 확인한다.
    """
    chs = load_channels()
    bad = {}

    def one(h):
        exists, why, kind = channel_exists(h)
        return h, (None if exists else (why or "채널을 찾을 수 없음")), kind

    with ThreadPoolExecutor(max_workers=8) as pool:
        for h, why, kind in pool.map(one, chs):
            if why:
                bad[h] = {"msg": why, "kind": kind or "temp"}
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
                videos, error, kind = fetch_channel(handle, limit)
            except Exception as e:
                log_error(f"fetch {handle}")
                videos, error, kind = [], f"{type(e).__name__}", "temp"
            yield json.dumps(
                {"channel": handle, "videos": videos, "error": error,
                 "kind": kind},
                ensure_ascii=False,
            ) + "\n"

    return Response(gen(), mimetype="application/x-ndjson")


def pick_port(start):
    """이미 쓰는 포트면 다음 번호로 넘어간다. 안 그러면 프로그램이 그냥 꺼진다."""
    import socket
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def say(msg):
    """윈도우 콘솔은 한글·기호에서 깨지며 멈출 수 있어 안전하게 출력한다."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


if __name__ == "__main__":
    port = pick_port(PORT)
    url = f"http://127.0.0.1:{port}"

    def open_when_ready():
        """서버가 실제로 응답할 때까지 기다렸다 연다. 너무 일찍 열면 연결 실패로 보인다."""
        import socket
        import time
        for _ in range(60):          # 최대 30초
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.5)
        open_browser(url)

    if not os.environ.get("NO_BROWSER"):
        Timer(0.3, open_when_ready).start()
    say(f"채널 모니터 실행 중 -> {url}   (종료: Ctrl+C)")
    try:
        app.run(port=port, debug=False, threaded=True)
    except Exception as e:
        say(f"[오류] 실행하지 못했습니다: {type(e).__name__}: {e}")
        input("엔터를 누르면 창이 닫힙니다...")
