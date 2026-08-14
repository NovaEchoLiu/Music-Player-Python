# Music Player for Python 版 v1.4
# 使用Python编写。需要Cloudflare workerss JavaScript版本：https://github.com/NovaEchoLiu/Music-Player-CFworkers
import os
import base64
from pathlib import Path
from functools import lru_cache
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import mutagen
from mutagen.id3 import ID3, APIC, USLT
import io
from PIL import Image

def get_password() -> str:
    pwd_file = Path(__file__).resolve().parent / "access_password.txt"
    if pwd_file.exists():
        return pwd_file.read_text(encoding="utf-8-sig").strip()
    return ""

def get_music_dir() -> Path:
    base_dir = Path(__file__).resolve().parent
    url_file = base_dir / "url.txt"
    if not url_file.exists():
        url_file = Path("url.txt")
        
    if url_file.exists():
        raw_path = url_file.read_text(encoding="utf-8-sig").strip().strip('"').strip("'")
        if raw_path:
            return Path(raw_path).resolve()
    return (base_dir / "music").resolve()

def get_auth_token(password: str) -> str:
    if not password:
        return ""
    raw = f"{password}_music_secure"
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

def is_authorized(request: Request) -> bool:
    password = get_password()
    if not password:
        return True
    token = get_auth_token(password)
    cookie_token = request.cookies.get("auth")
    return cookie_token == token

# ID3 标签提取与元数据解析
@lru_cache(maxsize=2048)
def get_media_metadata(audio_path_str: str, fallback_stem: str, fallback_mtime: float) -> dict:
    meta = {
        "title": fallback_stem,
        "artist": "未知歌手",
        "album": "未知专辑",
        "date": datetime.fromtimestamp(fallback_mtime, tz=timezone.utc).isoformat()
    }
    try:
        audio = mutagen.File(audio_path_str, easy=True)
        if audio is not None and getattr(audio, 'tags', None) is not None:
            if 'title' in audio.tags and audio.tags['title']:
                title_str = str(audio.tags['title'][0]).strip()
                if title_str: meta["title"] = title_str
            if 'artist' in audio.tags and audio.tags['artist']:
                artist_str = str(audio.tags['artist'][0]).strip()
                if artist_str: meta["artist"] = artist_str
            if 'album' in audio.tags and audio.tags['album']:
                album_str = str(audio.tags['album'][0]).strip()
                if album_str: meta["album"] = album_str
            if 'date' in audio.tags and audio.tags['date']:
                raw_date = str(audio.tags['date'][0]).strip()
                if len(raw_date) > 4: meta["date"] = raw_date
    except Exception:
        pass
    return meta

@lru_cache(maxsize=256)
def extract_embedded_cover(audio_path_str: str) -> tuple[bytes, str] | None:
    audio_path = Path(audio_path_str)
    if not audio_path.exists():
        return None
    try:
        f = mutagen.File(audio_path)
        if f is None:
            return None
        if hasattr(f, "tags") and f.tags:
            for val in f.tags.values():
                if isinstance(val, APIC):
                    return val.data, val.mime
        if "covr" in f and f["covr"]:
            data = bytes(f["covr"][0])
            mime = "image/png" if getattr(f["covr"][0], "imageformat", None) == 2 else "image/jpeg"
            return data, mime
        if hasattr(f, "pictures") and f.pictures:
            pic = f.pictures[0]
            return pic.data, pic.mime
    except Exception:
        pass
    return None

@lru_cache(maxsize=256)
def extract_embedded_lyrics(audio_path_str: str) -> str | None:
    audio_path = Path(audio_path_str)
    if not audio_path.exists():
        return None
    try:
        f = mutagen.File(audio_path)
        if f is None:
            return None
        if hasattr(f, "tags") and f.tags:
            for val in f.tags.values():
                if isinstance(val, USLT):
                    return val.text
        for tag in ["LYRICS", "unsyncedlyrics"]:
            if tag in f:
                return f[tag][0]
    except Exception:
        pass
    return None

def find_audio_file(base_path: Path) -> Path | None:
    for ext in [".mp3", ".flac", ".m4a", ".wav", ".aac"]:
        cand = base_path.with_suffix(ext)
        if cand.is_file():
            return cand
    return None

def get_mime_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".mp3"): return "audio/mpeg"
    if lower.endswith(".m4a"): return "audio/mp4"
    if lower.endswith(".flac"): return "audio/flac"
    if lower.endswith(".wav"): return "audio/wav"
    if lower.endswith(".aac"): return "audio/aac"
    if lower.endswith((".jpg", ".jpeg")): return "image/jpeg"
    if lower.endswith(".png"): return "image/png"
    if lower.endswith(".lrc"): return "text/plain; charset=utf-8"
    return "application/octet-stream"

# FastAPI
app = FastAPI(title="Music Player")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content=HTML_CONTENT)

@app.get("/robots.txt", response_class=PlainTextResponse)
async def serve_robots():
    return "User-agent: *\nDisallow: /"

@app.post("/login")
async def login(request: Request):
    req_pass = (await request.body()).decode("utf-8").strip()
    password = get_password()
    if req_pass == password:
        token = get_auth_token(password)
        resp = JSONResponse(content={"success": True})
        resp.set_cookie(
            key="auth",
            value=token,
            path="/",
            httponly=True,
            secure=False,
            max_age=31536000
        )
        return resp
    return JSONResponse(status_code=401, content={"error": "密码错误"})

@app.get("/list")
async def get_song_list(request: Request):
    if not is_authorized(request):
        return Response("Unauthorized - 请先登录", status_code=401)
    
    music_dir = get_music_dir()
    if not music_dir.exists():
        return JSONResponse(status_code=500, content={"error": f"音乐文件夹不存在: {music_dir}"})
    
    songs = []
    audio_extensions = {".mp3", ".flac", ".m4a", ".wav", ".aac"}
    
    for path in music_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in audio_extensions:
            rel_path = path.relative_to(music_dir).as_posix()
            
            fallback_mtime = path.stat().st_mtime
            meta = get_media_metadata(str(path), path.stem, fallback_mtime)
            
            songs.append({
                "name": meta["title"],
                "filename": path.name,
                "artist": meta["artist"],
                "album": meta["album"],
                "rel_path": rel_path,
                "url": f"/stream/{quote(rel_path, safe='/')}",
                "date": meta["date"]
            })
            
    songs.sort(key=lambda x: x["name"])
    return JSONResponse(content=songs)

@app.get("/stream/{file_path:path}")
async def stream_file(file_path: str, request: Request):
    if not is_authorized(request):
        return Response("Unauthorized", status_code=401)
        
    music_dir = get_music_dir()
    target_path = (music_dir / file_path).resolve()
    
    try:
        if not target_path.is_relative_to(music_dir):
            return Response("Forbidden", status_code=403)
    except AttributeError:
        if not str(target_path).startswith(str(music_dir)):
            return Response("Forbidden", status_code=403)

    if target_path.is_file():
        return FileResponse(
            path=target_path,
            media_type=get_mime_type(target_path.name),
            headers={"Content-Disposition": "inline"}
        )

    lower_path = file_path.lower()
    is_data_file = any(lower_path.endswith(ext) for ext in [".lrc", ".jpg", ".jpeg", ".png"])
    
    if is_data_file:
        audio_file = find_audio_file(target_path)
        
        if not audio_file and "/" not in file_path:
            data_path = (music_dir / "data" / file_path).resolve()
            if data_path.is_file():
                return FileResponse(data_path, media_type=get_mime_type(data_path.name))
            audio_file = find_audio_file(music_dir / "data" / Path(file_path).stem)
            
        if audio_file:
            if any(lower_path.endswith(ext) for ext in [".jpg", ".jpeg", ".png"]):
                extracted = extract_embedded_cover(str(audio_file))
                if extracted:
                    img_data, mime = extracted
                    return Response(content=img_data, media_type=mime, headers={"Content-Disposition": "inline"})
            elif lower_path.endswith(".lrc"):
                lyric_text = extract_embedded_lyrics(str(audio_file))
                if lyric_text:
                    return Response(content=lyric_text, media_type="text/plain; charset=utf-8")

    return Response("Not Found", status_code=404)


@app.get("/bg/{file_path:path}")
async def get_background_thumbnail(file_path: str, request: Request):
    if not is_authorized(request):
        return Response("Unauthorized", status_code=401)
        
    music_dir = get_music_dir()
    target_path = (music_dir / file_path).resolve()
    cache_dir = music_dir / ".cover_cache"
    cache_dir.mkdir(exist_ok=True)
    safe_cache_name = file_path.replace('/', '_').replace('\\', '_')
    cache_file = cache_dir / f"{Path(safe_cache_name).stem}_bg.jpg"
    
    if cache_file.exists():
        return FileResponse(path=cache_file, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000"})
        
    audio_file = find_audio_file(target_path)
    if audio_file:
        extracted = extract_embedded_cover(str(audio_file))
        if extracted:
            img_data, mime = extracted
            try:
                image = Image.open(io.BytesIO(img_data))
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.thumbnail((10, 10))
                image.save(cache_file, format="JPEG", quality=50)
                return FileResponse(path=cache_file, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000"})
            except Exception:
                pass
    return Response("Not Found", status_code=404)

# HTML 源码 版 v1.4
HTML_CONTENT = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Music Player</title>
    <style>
        * { -webkit-box-sizing: border-box; box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: Arial, Helvetica, sans-serif; background-color: #f2f4f8; color: #1e293b; height: 100%; overflow: hidden; position: relative; -webkit-transition: background-color 0.3s ease, color 0.2s ease; transition: background-color 0.3s ease, color 0.2s ease; }
        html { height: 100%; overflow: hidden; }
        #header { position: fixed; top: 0; left: 0; right: 0; height: 56px; background-color: #ffffff; border-bottom: 1px solid #e9edf2; display: flex; align-items: center; justify-content: space-between; z-index: 15; padding: 0 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }
        #header-title { font-size: 18px; font-weight: bold; color: #0f172a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; text-align: center; margin: 0 8px; }
        .icon-svg { width: 1em; height: 1em; vertical-align: -0.15em; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; display: inline-block; }
        .header-btn { font-size: 22px; color: #64748b; background: none; border: none; padding: 0 8px; cursor: pointer; flex: none; -webkit-tap-highlight-color: transparent; outline: none; display: flex; align-items: center; justify-content: center; }
        .header-btn .icon-svg { width: 22px; height: 22px; }
        #header-back { color: #3b82f6; font-size: 24px; font-weight: bold; }
        #search-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.4); z-index: 10050; align-items: center; justify-content: center; backdrop-filter: blur(4px); }
        .search-box { background: #ffffff; padding: 20px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.15); width: 85%; max-width: 320px; text-align: center; }
        .search-box h3 { font-size: 16px; margin-bottom: 14px; color: #0f172a; }
        .search-box input { width: 100%; padding: 10px 14px; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 15px; outline: none; background: #f8fafc; }
        .search-box input:focus { border-color: #3b82f6; background: #ffffff; }
        .search-btn-group { display: flex; gap: 10px; margin-top: 16px; }
        .search-box button { flex: 1; padding: 10px; border-radius: 8px; font-size: 14px; cursor: pointer; border: none; }
        .search-box .btn-confirm { background: #3b82f6; color: white; }
        .search-box .btn-cancel { background: #e2e8f0; color: #475569; }
        #sort-bar { position: fixed; top: 56px; left: 0; right: 0; height: 38px; background-color: #f2f4f8; display: flex; align-items: center; justify-content: flex-end; padding: 0 16px; gap: 8px; z-index: 9; border-bottom: 1px solid rgba(0,0,0,0.02); }
        #sort-bar select, #sort-bar button { padding: 4px 8px; border-radius: 8px; border: 1px solid #e2e8f0; background-color: #ffffff; color: #1e293b; font-size: 13px; outline: none; transition: all 0.2s; cursor: pointer; }
        #sort-bar button:active { background-color: #e2e8f0; transform: scale(0.96); }
        .view-container { position: fixed; top: 94px; bottom: 132px; left: 0; right: 0; overflow-y: auto; overflow-x: hidden; padding: 8px 10px 12px 10px; -webkit-overflow-scrolling: touch; }
        #list-container { z-index: 2; background-color: rgba(242, 244, 248, 0.3); }
        #sub-view-container { z-index: 10; background-color: rgba(242, 244, 248, 0.95); backdrop-filter: blur(10px); transform: translateX(100%); transition: transform 0.35s cubic-bezier(0.2, 0.8, 0.2, 1); }
        #sub-view-container.active { transform: translateX(0); }
        
        ul.song-list { list-style: none; padding: 0; margin: 0; }
        .list-view ul.song-list li { position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; margin-bottom: 6px; background-color: rgba(255, 255, 255, 0.7); border-radius: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.04); border: 1px solid rgba(255,255,255,0.4); cursor: pointer; }
        .list-view ul.song-list li:active { transform: scale(0.98); }
        .list-view .song-bg { position: absolute; top: -15px; left: -15px; right: -15px; bottom: -15px; background-size: cover; background-position: center; opacity: 0.35; filter: blur(12px); z-index: 0; transition: opacity 0.5s; }
        .list-view .song-info, .list-view .song-status { position: relative; z-index: 1; }
        .list-view .song-info { display: flex; align-items: center; overflow: hidden; flex: 1; }
        .list-view .song-index { width: 26px; font-size: 14px; font-weight: 500; color: #94a3b8; text-align: center; margin-right: 12px; }
        .list-view .song-text-wrap { display: flex; flex-direction: column; flex: 1; overflow: hidden; }
        .list-view .song-name { font-size: 15px; font-weight: 500; color: #1e293b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .list-view .song-artist { font-size: 12px; color: #64748b; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .list-view .song-status { font-size: 16px; color: #3b82f6; margin-left: 8px; width: 24px; text-align: center; }
        .grid-view ul.song-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 4px; }
        @media (min-width: 380px) { .grid-view ul.song-list { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); } }
        .grid-view ul.song-list li { position: relative; display: flex; flex-direction: column; align-items: center; text-align: center; background: rgba(255,255,255,0.6); padding: 8px; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); cursor: pointer; border: 1px solid rgba(255,255,255,0.4); min-width: 0; }
        .grid-view ul.song-list li:active { transform: scale(0.96); }
        .grid-view .grid-cover-wrap { width: 100%; aspect-ratio: 1/1; border-radius: 8px; overflow: hidden; background: #e2e8f0; margin-bottom: 8px; position: relative; }
        .grid-view .grid-cover { width: 100%; height: 100%; object-fit: cover; display: block; transition: opacity 0.3s; }
        .grid-view .song-name { font-size: 13px; font-weight: 600; color: #1e293b; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .grid-view .song-artist { font-size: 11px; color: #64748b; margin-top: 4px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .grid-view .song-bg, .grid-view .song-index, .grid-view .song-status { display: none; }
        .grid-view li.is-group .grid-cover-wrap { border-radius: 50%; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        #lyric-bar { position: fixed; bottom: 96px; left: 0; right: 0; height: 36px; background-color: rgba(255, 255, 255, 0.65); backdrop-filter: blur(10px); border-top: 1px solid rgba(233, 237, 242, 0.4); display: flex; align-items: center; justify-content: flex-start; z-index: 15; padding: 0 20px; font-size: 14px; color: #475569; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; box-shadow: 0 -2px 10px rgba(0,0,0,0.02); }
        #footer { position: fixed; bottom: 0; left: 0; right: 0; height: 96px; background-color: #ffffff; border-top: 1px solid #e9edf2; padding: 10px 16px 8px 16px; z-index: 16; display: flex; align-items: center; box-shadow: 0 -6px 20px rgba(0,0,0,0.05); }
        #cover-container { flex: none; width: 68px; height: 68px; margin-right: 14px; border-radius: 12px; overflow: hidden; background-color: #e9edf2; border: 1px solid #e2e8f0; display: flex; align-items: center; justify-content: center; }
        #cover-image { width: 100%; height: 100%; object-fit: cover; display: block; }
        #cover-placeholder { font-size: 32px; color: #94a3b8; line-height: 68px; text-align: center; }
        #footer-right { flex: 1; display: flex; flex-direction: column; justify-content: center; min-width: 0; }
        #footer-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2px; }
        #current-song-name { font-size: 15px; font-weight: 600; color: #0f172a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; margin-right: 8px; }
        #current-time { font-size: 12px; font-weight: 500; color: #94a3b8; }
        #progress-bar { width: 100%; height: 5px; -webkit-appearance: none; appearance: none; background-color: #e2e8f0; border-radius: 3px; outline: none; margin: 2px 0 6px 0; }
        #progress-bar::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 16px; height: 16px; background-color: #3b82f6; border-radius: 50%; border: 2px solid #ffffff; box-shadow: 0 2px 6px rgba(59,130,246,0.4); }
        #controls { display: flex; align-items: center; justify-content: center; }
        .ctrl-btn { background: none; border: none; padding: 0; width: 44px; height: 44px; cursor: pointer; color: #334155; text-align: center; outline: none; border-radius: 50%; }
        .ctrl-btn:active { background-color: rgba(0,0,0,0.05); transform: scale(0.92); }
        .ctrl-btn.play-btn { color: #3b82f6; }
        .ctrl-btn svg { width: 28px; height: 28px; display: inline-block; fill: currentColor; vertical-align: middle; }
        #fullscreen-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: 999; display: none; align-items: center; justify-content: center; background-color: rgba(0,0,0,0.3); }
        #fullscreen-overlay .bg { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-size: cover; background-position: center; filter: blur(80px); transform: scale(1.1); }
        #fullscreen-overlay .gradient { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(to top, rgba(0,0,0,0.7), rgba(0,0,0,0.2)); }
        #fullscreen-lyrics { position: relative; z-index: 10; width: 85%; max-height: 80%; overflow-y: auto; text-align: center; color: #ffffff; font-size: 24px; font-weight: 500; text-shadow: 0 2px 12px rgba(0,0,0,0.8); padding: 20px 10px; -webkit-overflow-scrolling: touch; }
        .lyric-line { padding: 12px 0; opacity: 0.4; transition: opacity 0.3s ease; transform: scale(0.95); }
        .lyric-line.active { opacity: 1; transform: scale(1.05); font-weight: 700; }
        
        #empty-tip { text-align: center; padding: 60px 20px; color: #94a3b8; font-size: 15px; }
        .hidden { display: none !important; }
        
        body.dark-mode { background-color: #121212; color: #e2e8f0; }
        body.dark-mode #header, body.dark-mode #footer, body.dark-mode .search-box { background-color: #1e293b; border-color: #2d3a4f; }
        body.dark-mode #header-title, body.dark-mode #current-song-name { color: #f1f5f9; }
        body.dark-mode #sort-bar { background-color: #121212; border-bottom-color: rgba(255,255,255,0.05); }
        body.dark-mode #sort-bar select, body.dark-mode #sort-bar button { background-color: #1e293b; border-color: #2d3a4f; color: #f1f5f9; }
        body.dark-mode #sort-bar button:active { background-color: #334155; }
        body.dark-mode #list-container { background-color: rgba(18, 18, 18, 0.3); }
        body.dark-mode #sub-view-container { background-color: rgba(18, 18, 18, 0.95); }
        body.dark-mode #lyric-bar { background-color: rgba(30, 41, 59, 0.65); border-top-color: rgba(45, 58, 79, 0.4); color: #cbd5e1; }
        body.dark-mode .list-view ul.song-list li { background-color: rgba(30, 41, 59, 0.7); border-color: rgba(45, 58, 79, 0.4); }
        body.dark-mode .list-view .song-name { color: #f1f5f9; }
        body.dark-mode .grid-view ul.song-list li { background: rgba(30, 41, 59, 0.7); border-color: rgba(45, 58, 79, 0.4); }
        body.dark-mode .grid-view .grid-cover-wrap { background: #0f172a; }
        body.dark-mode .grid-view .song-name { color: #f1f5f9; }
        #login-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(242, 244, 248, 0.95); z-index: 10000; align-items: center; justify-content: center; backdrop-filter: blur(10px); }
        .login-box { background: #ffffff; padding: 32px 24px; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); width: 85%; max-width: 320px; text-align: center; }
        .login-box h2 { font-size: 20px; color: #0f172a; margin-bottom: 24px; font-weight: 600; }
        .login-box input { width: 100%; padding: 12px 16px; margin-bottom: 20px; border: 1px solid #e2e8f0; border-radius: 10px; font-size: 16px; outline: none; background: #f8fafc; transition: all 0.2s; }
        .login-box input:focus { border-color: #3b82f6; background: #ffffff; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
        .login-box button { width: 100%; padding: 14px; background: #3b82f6; color: white; border: none; border-radius: 10px; font-size: 16px; cursor: pointer; }
        .login-error { color: #ef4444; font-size: 14px; margin-top: 12px; display: none; }
        body.dark-mode #login-overlay { background: rgba(18, 18, 18, 0.95); }
    </style>
</head>
<body>
    <div id="login-overlay">
        <div class="login-box">
            <h2>🔒 受保护的站点</h2>
            <input type="password" id="pwd-input" placeholder="请输入访问密码">
            <button onclick="submitLogin()">登录</button>
            <p class="login-error" id="login-err">密码错误，请重试！</p>
        </div>
    </div>

    <div id="search-overlay" onclick="closeSearchModal(event)">
        <div class="search-box" onclick="event.stopPropagation()">
            <h3><svg class="icon-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg> 检索网盘音乐</h3>
            <input type="text" id="search-input" placeholder="输入关键字(歌名/歌手/专辑)..." onkeypress="if(event.key === 'Enter') executeSearch()">
            <div class="search-btn-group">
                <button class="btn-cancel" onclick="clearSearch()">清除</button>
                <button class="btn-confirm" onclick="executeSearch()">搜索</button>
            </div>
        </div>
    </div>

    <div id="header">
        <button class="header-btn" id="header-back" onclick="handleGlobalBack()"><svg class="icon-svg" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg></button>
        <button class="header-btn" id="view-toggle" onclick="toggleViewMode()">田</button>
        <span id="header-title"><svg class="icon-svg" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg> 我的音乐库</span>
        <button class="header-btn" id="theme-toggle" onclick="toggleTheme()"><svg class="icon-svg" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg></button>
        <button class="header-btn" id="search-toggle" onclick="openSearchModal()"><svg class="icon-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg></button>
    </div>
    
    <div id="sort-bar">
        <select id="group-type" onchange="handleSortChange()">
            <option value="name">按歌曲排列</option>
            <option value="artist">按歌手排列</option>
            <option value="album">按专辑排列</option>
        </select>
        <select id="song-sort-type" onchange="handleSortChange()">
            <option value="name">按名称排序</option>
            <option value="date">按创建日期</option>
            <option value="random">随机排序</option>
        </select>
        <select id="sort-order" onchange="handleSortChange()">
            <option value="asc">升序 ↑</option>
            <option value="desc">降序 ↓</option>
        </select>
        <button id="shuffle-btn" class="hidden" onclick="triggerShuffle()" title="重新随机打乱">🔀 刷新</button>
    </div>

    <div id="list-container" class="view-container list-view">
        <ul id="song-list" class="song-list"></ul>
        <div id="empty-tip">Please Wait...</div>
    </div>

    <div id="sub-view-container" class="view-container list-view">
        <ul id="sub-song-list" class="song-list"></ul>
    </div>

    <div id="lyric-bar"><svg class="icon-svg" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg> 暂无歌词</div>
    
    <div id="footer">
        <div id="cover-container" onclick="openFullscreen()">
            <img id="cover-image" src="" alt="封面" style="display:none;">
            <span id="cover-placeholder"><svg class="icon-svg" style="width:32px; height:32px;" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg></span>
        </div>
        <div id="footer-right">
            <div id="footer-top">
                <span id="current-song-name">未选择歌曲</span>
                <span id="current-time">00:00 / 00:00</span>
            </div>
            <input type="range" id="progress-bar" min="0" max="100" value="0" step="1">
            <div id="controls">
                <button class="ctrl-btn mode-btn" id="mode-btn" onclick="toggleMode()" style="font-size: 20px; margin-right: 12px;">🔁</button>
                <button class="ctrl-btn prev-btn" onclick="prevSong()" style="margin-right:12px;">
                    <svg viewBox="0 0 24 24"><path d="M6 6h2v12H6V6zm3.5 6l8.5 6V6l-8.5 6z"/></svg>
                </button>
                <button class="ctrl-btn play-btn" id="play-btn" onclick="togglePlay()">
                    <svg id="play-icon" viewBox="0 0 24 24"><path d="M8 5v14l11-7L8 5z"/></svg>
                    <svg id="pause-icon" style="display:none;" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                </button>
                <button class="ctrl-btn next-btn" onclick="nextSong()" style="margin-left:12px;">
                    <svg viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
                </button>
            </div>
        </div>
    </div>

    <div id="fullscreen-overlay" onclick="closeFullscreen()">
        <div class="bg" id="fullscreen-bg"></div>
        <div class="gradient"></div>
        <div id="fullscreen-lyrics"></div>
    </div>

    <audio id="audio-player" preload="metadata"></audio>

    <script type="text/javascript">
        var ICONS = {
            music: '<svg class="icon-svg" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>',
            singleLoop: '<svg class="icon-svg" viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"></polyline><path d="M3 11V9a4 4 0 0 1 4-4h14"></path><polyline points="7 23 3 19 7 15"></polyline><path d="M21 13v2a4 4 0 0 1-4 4H3"></path><text x="12" y="16" text-anchor="middle" font-size="9" fill="currentColor" stroke="none" font-family="Arial, sans-serif" font-weight="bold">1</text></svg>',
            random: '<svg class="icon-svg" viewBox="0 0 24 24"><polyline points="16 3 21 3 21 8"></polyline><line x1="4" y1="20" x2="21" y2="3"></line><polyline points="21 16 21 21 16 21"></polyline><line x1="15" y1="15" x2="21" y2="21"></line><line x1="4" y1="4" x2="9" y2="9"></line></svg>',
            loop: '<svg class="icon-svg" viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"></polyline><path d="M3 11V9a4 4 0 0 1 4-4h14"></path><polyline points="7 23 3 19 7 15"></polyline><path d="M21 13v2a4 4 0 0 1-4 4H3"></path></svg>',
            search: '<svg class="icon-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>',
            moon: '<svg class="icon-svg" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>',
            sun: '<svg class="icon-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>',
            folder: '<svg class="icon-svg" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>',
            refresh: '<svg class="icon-svg" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>'
        };

        var rawSongList = [];
        var currentList = [];
        var subSongList = [];
        var shuffledSongList = [];
        
        var currentIndex = -1; 
        var isPlaying = false; 
        var playHistory = [];
        var playMode = 'loop'; 
        
        var viewMode = localStorage.getItem('player-view-mode') || 'list'; // 'list' | 'grid'
        var isSubViewActive = false;
        
        var sim = { active: false, currentTime: 0, duration: 120, timer: null };
        var lyrics = []; var hasLyrics = false;
        var listEl = document.getElementById('song-list'); 
        var subListEl = document.getElementById('sub-song-list');
        var emptyTip = document.getElementById('empty-tip');
        var audio = document.getElementById('audio-player'); 
        var currentNameEl = document.getElementById('current-song-name'); 
        var currentTimeEl = document.getElementById('current-time');
        var progressBar = document.getElementById('progress-bar'); 
        var coverImage = document.getElementById('cover-image');
        var coverPlaceholder = document.getElementById('cover-placeholder'); 
        var overlay = document.getElementById('fullscreen-overlay'); 
        var fullscreenBg = document.getElementById('fullscreen-bg');
        var fullscreenLyrics = document.getElementById('fullscreen-lyrics');

        function encodePath(p) { return p.split('/').map(encodeURIComponent).join('/'); }
        function getRelPathNoExt(p) { return p.substring(0, p.lastIndexOf('.')); }

        var coverObserver = new IntersectionObserver(function(entries, observer) {
            entries.forEach(function(entry) {
                if (entry.isIntersecting) {
                    var el = entry.target;
                    var relPath = el.getAttribute('data-rel');
                    var isBg = el.classList.contains('song-bg');
                    
                    if (relPath) {
                        if (isBg && !el.style.backgroundImage) {
                            var bgUrl = '/bg/' + encodePath(relPath);
                            var img = new Image();
                            img.onload = function() { el.style.backgroundImage = 'url(' + bgUrl + ')'; };
                            img.src = bgUrl;
                        } else if (!isBg && el.getAttribute('data-loaded') !== '1') {
                            var noExt = getRelPathNoExt(relPath);
                            el.setAttribute('data-loaded', '1');
                            el.onerror = function() { this.onerror = null; this.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg"/%3E'; };
                            el.src = '/stream/' + encodePath(noExt) + '.jpg';
                        }
                    }
                    observer.unobserve(el);
                }
            });
        }, { rootMargin: '100px 0px' });

        function toggleTheme() {
            var body = document.body;
            if (body.className.indexOf('dark-mode') !== -1) {
                body.className = ''; document.getElementById('theme-toggle').innerHTML = ICONS.moon; localStorage.setItem('player-theme', 'light');
            } else {
                body.className = 'dark-mode'; document.getElementById('theme-toggle').innerHTML = ICONS.sun; localStorage.setItem('player-theme', 'dark');
            }
        }
        function loadTheme() { if (localStorage.getItem('player-theme') === 'dark') { document.body.className = 'dark-mode'; document.getElementById('theme-toggle').innerHTML = ICONS.sun; } }

        function toggleViewMode() {
            viewMode = (viewMode === 'list') ? 'grid' : 'list';
            document.getElementById('view-toggle').innerHTML = (viewMode === 'list') ? '田' : '☰';
            localStorage.setItem('player-view-mode', viewMode);
            renderView();
            updateListHighlight();
        }

        function groupData(songs, keyField) {
            var groups = {};
            songs.forEach(function(s) {
                var val = s[keyField] || '未知';
                if (!groups[val]) groups[val] = [];
                groups[val].push(s);
            });
            var arr = [];
            for (var k in groups) {
                arr.push({ 
                    isGroup: true, 
                    type: keyField,
                    name: k, 
                    count: groups[k].length, 
                    songs: groups[k], 
                    representative: groups[k][0] 
                });
            }
            return arr;
        }

        function doShuffle(arr) {
            var copy = arr.slice();
            for (var i = copy.length - 1; i > 0; i--) {
                var j = Math.floor(Math.random() * (i + 1));
                var temp = copy[i];
                copy[i] = copy[j];
                copy[j] = temp;
            }
            return copy;
        }

        function triggerShuffle() {
            shuffledSongList = doShuffle(rawSongList);
            handleSortChange();
        }

        function handleSortChange() {
            var groupType = document.getElementById('group-type').value;
            var songSortTypeEl = document.getElementById('song-sort-type');
            var sortOrderEl = document.getElementById('sort-order');
            var shuffleBtnEl = document.getElementById('shuffle-btn');

            if (groupType === 'name') {
                songSortTypeEl.classList.remove('hidden');
                var songSortType = songSortTypeEl.value;

                if (songSortType === 'random') {
                    sortOrderEl.classList.add('hidden');
                    shuffleBtnEl.classList.remove('hidden');
                    if (shuffledSongList.length !== rawSongList.length) {
                        shuffledSongList = doShuffle(rawSongList);
                    }
                    currentList = shuffledSongList.slice();
                } else {
                    sortOrderEl.classList.remove('hidden');
                    shuffleBtnEl.classList.add('hidden');
                    var sortOrder = sortOrderEl.value;

                    if (songSortType === 'name') {
                        currentList = rawSongList.slice().sort(function(a, b) {
                            var res = a.name.localeCompare(b.name, 'zh-CN');
                            return sortOrder === 'asc' ? res : -res;
                        });
                    } else if (songSortType === 'date') {
                        currentList = rawSongList.slice().sort(function(a, b) {
                            var dateA = a.date || '';
                            var dateB = b.date || '';
                            var res = dateA.localeCompare(dateB);
                            return sortOrder === 'asc' ? res : -res;
                        });
                    }
                }
            } else {
                songSortTypeEl.classList.add('hidden');
                sortOrderEl.classList.remove('hidden');
                shuffleBtnEl.classList.add('hidden');

                var sortOrder = sortOrderEl.value;
                var grouped = groupData(rawSongList, groupType);
                currentList = grouped.sort(function(a, b) {
                    var res = a.name.localeCompare(b.name, 'zh-CN');
                    return sortOrder === 'asc' ? res : -res;
                });
            }

            if (!isSubViewActive) renderView();
        }

        function handleGlobalBack() {
            if (isSubViewActive) {
                closeSubGroupView();
            } else {
                init();
            }
        }

        function openSubGroupView(type, name, songs) {
            isSubViewActive = true;
            var sOrder = document.getElementById('sort-order').value;
            subSongList = songs.slice().sort(function(a, b) {
                var res = a.name.localeCompare(b.name, 'zh-CN');
                return sOrder === 'asc' ? res : -res;
            });
            
            document.getElementById('header-title').innerHTML = name;
            document.getElementById('header-back').innerHTML = '←';
            document.getElementById('sub-view-container').classList.add('active');
            renderView();
        }

        function closeSubGroupView() {
            isSubViewActive = false;
            document.getElementById('sub-view-container').classList.remove('active');
            document.getElementById('header-title').innerHTML = ICONS.folder + ' 我的音乐库';
            document.getElementById('header-back').innerHTML = ICONS.refresh;
            renderView();
            updateListHighlight();
        }

        function renderView() {
            var container = isSubViewActive ? subListEl : listEl;
            var data = isSubViewActive ? subSongList : currentList;
            var parent = isSubViewActive ? document.getElementById('sub-view-container') : document.getElementById('list-container');
            
            if (viewMode === 'grid') {
                parent.classList.add('grid-view'); parent.classList.remove('list-view');
            } else {
                parent.classList.add('list-view'); parent.classList.remove('grid-view');
            }
            
            container.innerHTML = '';
            
            if (!data || data.length === 0) {
                if (!isSubViewActive) { emptyTip.className = ''; emptyTip.innerHTML = '未找到匹配的资源'; }
                return;
            }
            emptyTip.className = 'hidden';

            data.forEach(function(item, i) {
                var li = document.createElement('li');
                
                if (item.isGroup) {
                    li.classList.add('is-group');
                    li.onclick = function() { openSubGroupView(item.type, item.name, item.songs); };
                    if (viewMode === 'grid') {
                        li.innerHTML = '<div class="grid-cover-wrap"><img class="grid-cover" data-rel="'+item.representative.rel_path+'" data-loaded="0"></div>' +
                                       '<div class="song-name">'+item.name+'</div>' +
                                       '<div class="song-artist">'+item.count+' 首资源</div>';
                    } else {
                        li.innerHTML = '<div class="song-bg" data-rel="'+item.representative.rel_path+'"></div>' +
                                       '<div class="song-info"><span class="song-text-wrap"><span class="song-name">'+item.name+'</span><span class="song-artist">'+item.count+' 首资源</span></span></div>' +
                                       '<span class="song-status">' + ICONS.folder + '</span>';
                    }
                } else {
                    var masterIdx = rawSongList.indexOf(item);
                    li.setAttribute('data-master-idx', masterIdx);
                    li.onclick = function() { playHistory = []; playSongByIndex(masterIdx); };
                    
                    if (viewMode === 'grid') {
                        li.innerHTML = '<div class="grid-cover-wrap"><img class="grid-cover" data-rel="'+item.rel_path+'" data-loaded="0"></div>' +
                                       '<div class="song-name" title="'+item.name+'">'+item.name+'</div>' +
                                       '<div class="song-artist" title="'+item.artist+'">'+item.artist+'</div>';
                    } else {
                        li.innerHTML = '<div class="song-bg" data-rel="'+item.rel_path+'"></div>' +
                                       '<div class="song-info"><span class="song-index">'+(i+1)+'</span>' +
                                       '<span class="song-text-wrap"><span class="song-name">'+item.name+'</span>' +
                                       '<span class="song-artist">'+item.artist+' - '+item.album+'</span></span></div>' +
                                       '<span class="song-status">' + ICONS.music + '</span>';
                    }
                }
                
                var covers = li.querySelectorAll('.song-bg, .grid-cover');
                covers.forEach(function(c) { coverObserver.observe(c); });
                container.appendChild(li);
            });
            
            updateListHighlight();
        }

        function updateCoverByRelPath(relPath) {
            var noExt = encodePath(getRelPathNoExt(relPath));
            var coverUrl = '/stream/' + noExt + '.jpg';
            coverImage.onerror = function() { coverImage.style.display = 'none'; coverPlaceholder.style.display = 'block'; };
            coverImage.onload = function() { coverImage.style.display = 'block'; coverPlaceholder.style.display = 'none'; };
            coverImage.src = coverUrl;
        }

        function loadLyricsByRelPath(relPath, callback) {
            var noExt = encodePath(getRelPathNoExt(relPath));
            var lrcUrl = '/stream/' + noExt + '.lrc';
            var xhr = new XMLHttpRequest();
            xhr.open('GET', lrcUrl, true);
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    if (xhr.status === 200) { parseLrc(xhr.responseText); } else { lyrics = []; hasLyrics = false; }
                    if (callback) callback();
                }
            };
            xhr.send();
        }

        function parseLrc(text) {
            var lines = text.split('\n'); var result = []; var timeReg = /\[(\d{2}):(\d{2})(?:\.(\d{2}))?\]/;
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i].trim(); if (!line) continue;
                var match = timeReg.exec(line);
                if (match) {
                    var time = parseInt(match[1], 10) * 60 + parseInt(match[2], 10) + parseInt(match[3] || '0', 10) / 100;
                    var textStr = line.replace(timeReg, '').trim();
                    if (textStr) result.push({ time: time, text: textStr });
                }
            }
            result.sort(function(a, b) { return a.time - b.time; }); lyrics = result; hasLyrics = lyrics.length > 0;
        }

        function getCurrentLyricIndex(time) {
            if (!hasLyrics) return -1; var idx = -1;
            for (var i = 0; i < lyrics.length; i++) { if (lyrics[i].time <= time) { idx = i; } else { break; } }
            return idx;
        }

        function renderFullscreenLyrics() {
            fullscreenLyrics.innerHTML = '';
            if (!hasLyrics || lyrics.length === 0) {
                var tip = document.createElement('div'); tip.className = 'no-lyrics'; tip.textContent = '暂无歌词';
                fullscreenLyrics.appendChild(tip); return;
            }
            for (var i = 0; i < lyrics.length; i++) {
                var div = document.createElement('div'); div.className = 'lyric-line'; div.textContent = lyrics[i].text;
                fullscreenLyrics.appendChild(div);
            }
        }

        function scrollToCurrentLyric() {
            if (!hasLyrics) return;
            var t = sim.active ? sim.currentTime : (audio.currentTime || 0);
            var idx = getCurrentLyricIndex(t);
            var lines = fullscreenLyrics.getElementsByClassName('lyric-line');
            for (var i = 0; i < lines.length; i++) { lines[i].className = (i === idx) ? 'lyric-line active' : 'lyric-line'; }
            if (idx >= 0 && lines.length > 0) {
                var activeLine = lines[idx]; var container = fullscreenLyrics;
                container.scrollTop = activeLine.offsetTop - container.offsetHeight / 2 + activeLine.offsetHeight / 2;
            }
        }

        function updateUI() {
            var current = sim.active ? sim.currentTime : (audio.currentTime || 0); 
            var duration = sim.active ? sim.duration : (audio.duration || 0);
            if (!isNaN(duration) && duration > 0) {
                progressBar.value = (current / duration) * 100;
                var cM = Math.floor(current / 60); var cS = Math.floor(current % 60); cS = cS < 10 ? '0'+cS : cS;
                var dM = Math.floor(duration / 60); var dS = Math.floor(duration % 60); dS = dS < 10 ? '0'+dS : dS;
                currentTimeEl.innerHTML = cM + ':' + cS + ' / ' + dM + ':' + dS;
                if (overlay.style.display !== 'none') scrollToCurrentLyric();
                
                var lyricBar = document.getElementById('lyric-bar');
                if (!hasLyrics) { lyricBar.innerHTML = ICONS.music + ' 暂无歌词'; } 
                else {
                    var idx = getCurrentLyricIndex(current);
                    lyricBar.innerHTML = (idx >= 0 && idx < lyrics.length) ? lyrics[idx].text : lyrics[0].text;
                }
            }
        }

        function playSongByIndex(index) {
            if (!rawSongList || rawSongList.length === 0 || index < 0 || index >= rawSongList.length) return;
            currentIndex = index; 
            var song = rawSongList[index]; 
            currentNameEl.innerHTML = song.name; 
            updateCoverByRelPath(song.rel_path); 
            
            stopSimulation();
            if (song.url) {
                sim.active = false; audio.src = song.url; audio.load(); audio.play(); isPlaying = true; updatePlayBtn(true);
            } else {
                sim.active = true; sim.currentTime = 0; sim.duration = 120; audio.removeAttribute('src'); audio.load();
                startSimulation(); isPlaying = true; updatePlayBtn(true);
            }

            loadLyricsByRelPath(song.rel_path, function() { 
                updateUI(); 
                if (overlay.style.display !== 'none') { renderFullscreenLyrics(); scrollToCurrentLyric(); } 
            });
            updateListHighlight();
            updateUI();
        }

        function updateListHighlight() {
            document.querySelectorAll('li[data-master-idx]').forEach(function(li) {
                var masterIdx = parseInt(li.getAttribute('data-master-idx'));
                var isActive = (masterIdx === currentIndex);
                
                var status = li.querySelector('.song-status');
                if (status) {
                    status.innerHTML = isActive ? '▶' : ICONS.music;
                    status.style.color = isActive ? '#3b82f6' : '#94a3b8';
                }
            });
        }

        function togglePlay() {
            if (currentIndex === -1) { if (rawSongList.length > 0) playSongByIndex(0); return; }
            if (sim.active) {
                if (isPlaying) { pauseSimulation(); isPlaying = false; updatePlayBtn(false); } 
                else { resumeSimulation(); isPlaying = true; updatePlayBtn(true); }
            } else {
                if (isPlaying) { audio.pause(); isPlaying = false; updatePlayBtn(false); } 
                else { audio.play(); isPlaying = true; updatePlayBtn(true); }
            }
        }

        function toggleMode() {
            var modeBtn = document.getElementById('mode-btn');
            if (playMode === 'loop') { playMode = 'random'; modeBtn.innerHTML = ICONS.random; } 
            else if (playMode === 'random') { playMode = 'single'; modeBtn.innerHTML = ICONS.singleLoop; } 
            else { playMode = 'loop'; modeBtn.innerHTML = ICONS.loop; }
        }

        function prevSong() { 
            if (!rawSongList.length) return;
            var newIdx = (playMode === 'random' && playHistory.length > 0) ? playHistory.pop() : 
                         (playMode === 'random') ? Math.floor(Math.random() * rawSongList.length) :
                         (currentIndex - 1 < 0) ? rawSongList.length - 1 : currentIndex - 1;
            playSongByIndex(newIdx); 
        }

        function nextSong() { 
            if (!rawSongList.length) return;
            if (playMode === 'random' && currentIndex >= 0) playHistory.push(currentIndex);
            var newIdx = (playMode === 'random') ? Math.floor(Math.random() * rawSongList.length) :
                         (currentIndex + 1 >= rawSongList.length) ? 0 : currentIndex + 1;
            playSongByIndex(newIdx); 
        }

        function updatePlayBtn(pl) {
            document.getElementById('play-icon').style.display = pl ? 'none' : 'inline-block';
            document.getElementById('pause-icon').style.display = pl ? 'inline-block' : 'none';
        }

        function openSearchModal() { document.getElementById('search-overlay').style.display = 'flex'; document.getElementById('search-input').focus(); }
        function closeSearchModal(e) { if (!e || e.target === document.getElementById('search-overlay')) document.getElementById('search-overlay').style.display = 'none'; }
        function clearSearch() { document.getElementById('search-input').value = ''; handleSortChange(); closeSearchModal(); }
        function executeSearch() {
            var keyword = document.getElementById('search-input').value.trim().toLowerCase();
            if (!keyword) { handleSortChange(); } 
            else {
                currentList = rawSongList.filter(function(s) {
                    return (s.name && s.name.toLowerCase().indexOf(keyword) > -1) || 
                           (s.artist && s.artist.toLowerCase().indexOf(keyword) > -1) || 
                           (s.album && s.album.toLowerCase().indexOf(keyword) > -1);
                });
                document.getElementById('group-type').value = 'name'; // Force pure list on search
                if (isSubViewActive) closeSubGroupView();
                renderView();
            }
            closeSearchModal();
        }

        function startSimulation() { sim.timer = setInterval(function() { if (!isPlaying) return; sim.currentTime += 0.5; if (sim.currentTime >= sim.duration) { sim.currentTime = sim.duration; updateUI(); audioEnded(); } else updateUI(); }, 500); }
        function pauseSimulation() { clearInterval(sim.timer); sim.timer = null; }
        function resumeSimulation() { if (!sim.timer) startSimulation(); }
        function stopSimulation() { clearInterval(sim.timer); sim.timer = null; sim.active = false; sim.currentTime = 0; }
        function audioEnded() { isPlaying = false; updatePlayBtn(false); if (playMode === 'single') playSongByIndex(currentIndex); else nextSong(); }
        
        audio.addEventListener('timeupdate', function() { if (!sim.active) updateUI(); });
        audio.addEventListener('ended', function() { if (!sim.active) audioEnded(); });
        audio.addEventListener('error', function() { isPlaying = false; updatePlayBtn(false); });
        progressBar.addEventListener('input', function() {
            var val = parseFloat(progressBar.value); var dur = sim.active ? sim.duration : (audio.duration || 0);
            if (!isNaN(dur) && dur > 0) { var seek = (val / 100) * dur; if (sim.active) { sim.currentTime = seek; updateUI(); } else { audio.currentTime = seek; } }
        });

        function openFullscreen() {
            var src = coverImage.src; if (!src || coverImage.style.display === 'none') src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg"/%3E';
            fullscreenBg.style.backgroundImage = 'url(' + src + ')';
            overlay.style.display = 'flex'; renderFullscreenLyrics(); scrollToCurrentLyric();
        }
        function closeFullscreen() { overlay.style.display = 'none'; }

        function submitLogin() {
            var pwd = document.getElementById('pwd-input').value; if(!pwd) return;
            var xhr = new XMLHttpRequest(); xhr.open('POST', '/login', true);
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    if (xhr.status === 200) { document.getElementById('login-overlay').style.display = 'none'; document.getElementById('login-err').style.display = 'none'; init(); } 
                    else { document.getElementById('login-err').style.display = 'block'; }
                }
            };
            xhr.send(pwd);
        }
        document.getElementById('pwd-input').addEventListener('keypress', function (e) { if (e.key === 'Enter') submitLogin(); });

        function init() {
            loadTheme();
            document.getElementById('view-toggle').innerHTML = (viewMode === 'list') ? '田' : '☰';
            document.getElementById('mode-btn').innerHTML = ICONS.loop;
            isSubViewActive = false; document.getElementById('sub-view-container').classList.remove('active');
            
            emptyTip.innerHTML = 'Connecting...'; emptyTip.className = ''; 
            listEl.innerHTML = '';
            
            var xhr = new XMLHttpRequest(); xhr.open('GET', '/list', true);
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    if (xhr.status === 401) { document.getElementById('login-overlay').style.display = 'flex'; emptyTip.innerHTML = '受保护的站点，请先登录！'; return; }
                    if (xhr.status === 200) {
                        try {
                            rawSongList = JSON.parse(xhr.responseText);
                            document.getElementById('group-type').value = 'name';
                            handleSortChange();
                            
                            if (rawSongList.length > 0 && currentIndex === -1) {
                                currentNameEl.innerHTML = rawSongList[0].name;
                                updateCoverByRelPath(rawSongList[0].rel_path);
                                loadLyricsByRelPath(rawSongList[0].rel_path, function(){ updateUI(); });
                            }
                        } catch (e) { emptyTip.innerHTML = '解析数据失败！'; }
                    } else { emptyTip.innerHTML = xhr.responseText || '连接失败！(错误码:' + xhr.status + ')'; }
                }
            };
            xhr.send();
        }

        init();
    </script>
</body>
</html>"""

# 启动入口
# 0.0.0.0 默认允许公网 port=8000 监听8000端口 可自行修改
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)