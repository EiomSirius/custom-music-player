import os, re, subprocess, uuid, json, shutil, threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

BASE = Path(os.environ.get("MUSIC_DIR", "/app/music"))
TRACKS = BASE / "tracks"
THUMBS = BASE / "thumbs"
LIB = BASE / "library.json"
os.makedirs(TRACKS, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)

# YouTube descarga: receta WARP + PO + formato 18 (ver skill yt-dlp-media-download)
YT_PROXY = os.environ.get("YT_PROXY", "socks5://85.208.48.210:1080")
YT_EXTRACTOR_ARGS = "youtube:player_client=tv,web,mweb"

def load_lib():
    if LIB.exists():
        try:
            lib = json.loads(LIB.read_text())
        except Exception:
            lib = {"tracks": [], "playlists": []}
    else:
        lib = {"tracks": [], "playlists": []}
    _import_existing(lib)
    return lib

def _import_existing(lib):
    """Importa mp3 que estén sueltos en MUSIC_DIR (no dentro de tracks/) al arrancar."""
    import hashlib
    known = {t["file"] for t in lib["tracks"]}
    changed = False
    for p in sorted(BASE.glob("*.mp3")):
        if p.name in known:
            continue
        tid = hashlib.md5(p.name.encode()).hexdigest()[:10]
        # mover a tracks/ para que el storage quede limpio
        dest = TRACKS / p.name
        if not dest.exists():
            shutil.move(str(p), str(dest))
        dur = probe_duration(dest)
        lib["tracks"].append({"id": tid, "title": Path(p.name).stem, "artist": "", "file": p.name, "dur": dur, "thumb": "", "source": "import"})
        known.add(p.name)
        changed = True
    if changed:
        save_lib(lib)

def save_lib(lib):
    LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))

def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1", str(path)],
                       capture_output=True, text=True, timeout=30)
    try:
        return round(float(r.stdout.strip().split("=")[-1]))
    except Exception:
        return 0

app = FastAPI(title="Custom Music Player")

@app.get("/api/library")
def get_library():
    lib = load_lib()
    return lib

@app.get("/api/tracks/{tid}/file")
def get_track(tid: str):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no track")
    p = TRACKS / t["file"]
    if not p.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(p, media_type="audio/mpeg", filename=Path(t["file"]).name)

@app.get("/api/thumbs/{tid}")
def get_thumb(tid: str):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404)
    p = THUMBS / (t["id"] + ".jpg")
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")

def _norm_name(s):
    s = re.sub(r"[^\w\-. ]+", "", s).strip()
    return s or "track"

def _add_track_file(src_path: Path, title: str, artist: str, ext: str, thumb_src: Optional[Path] = None):
    lib = load_lib()
    tid = uuid.uuid4().hex[:10]
    fname = f"{tid}.{ext}"
    shutil.copy(src_path, TRACKS / fname)
    dur = probe_duration(TRACKS / fname)
    thumb = ""
    if thumb_src and thumb_src.exists():
        try:
            shutil.copy(thumb_src, THUMBS / (tid + ".jpg"))
            thumb = tid
        except Exception:
            pass
    tr = {"id": tid, "title": title, "artist": artist, "file": fname, "dur": dur, "thumb": thumb, "source": "upload"}
    lib["tracks"].append(tr)
    save_lib(lib)
    return tr

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Sube mp3/audio o vídeo (se extrae el audio). Acepta varios archivos a la vez."""
    out = []
    for f in files:
        name = Path(f.filename or "audio.mp3").name
        raw = TRACKS / ("raw_" + uuid.uuid4().hex[:10] + Path(name).suffix.lower())
        raw.write_bytes(await f.read())
        ext = Path(name).suffix.lower()
        if ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma"):
            tr = _add_track_file(raw, Path(name).stem, "", "mp3")
            raw.unlink(missing_ok=True)
        elif ext in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
            mp3 = TRACKS / ("raw_" + uuid.uuid4().hex[:10] + ".mp3")
            r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3)],
                               capture_output=True, text=True, timeout=600)
            raw.unlink(missing_ok=True)
            if r.returncode != 0:
                out.append({"error": f"{name}: no se pudo extraer audio"})
                continue
            tr = _add_track_file(mp3, Path(name).stem, "", "mp3")
            mp3.unlink(missing_ok=True)
        else:
            raw.unlink(missing_ok=True)
            out.append({"error": f"{name}: formato no soportado"})
            continue
        out.append(tr)
    return {"added": [t for t in out if "error" not in t], "errors": [t for t in out if "error" in t]}

# ---------- YouTube ----------
def _yt_download(url: str, tid: str):
    """Descarga audio de YouTube con la receta WARP. Corre en thread."""
    out_template = str(TRACKS / tid) + ".%(ext)s"
    cmd = [
        "yt-dlp",
        "--proxy", YT_PROXY,
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--extractor-args", YT_EXTRACTOR_ARGS,
        "--no-part",
        "-f", "18/bestaudio/best",
        "-o", out_template,
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    # encontrar el archivo descargado
    files = sorted(TRACKS.glob(tid + ".*"))
    video = next((f for f in files if f.suffix in (".mp4", ".webm", ".mkv")), None)
    if not video:
        return {"error": r.stderr[-500:] if r.stderr else "descarga fallida"}
    mp3 = TRACKS / (tid + ".mp3")
    rr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3)],
                        capture_output=True, text=True, timeout=600)
    video.unlink(missing_ok=True)
    if rr.returncode != 0:
        return {"error": "conversión fallida"}
    # limpiar thumbnails a jpg
    for f in TRACKS.glob(tid + ".*"):
        if f.suffix in (".webp", ".png", ".jpeg"):
            f.unlink(missing_ok=True)
    return {"ok": True, "mp3": mp3.name}

class YTReq(BaseModel):
    url: str
    title: Optional[str] = None
    artist: Optional[str] = None

@app.post("/api/youtube")
def youtube(req: YTReq):
    lib = load_lib()
    tid = uuid.uuid4().hex[:10]
    title = (req.title or "Video de YouTube").strip()
    artist = req.artist or "YouTube"
    # correr en thread para no bloquear (timeout largo)
    res = _yt_download(req.url, tid)
    if "error" in res:
        return JSONResponse({"error": res["error"]}, status_code=502)
    mp3 = TRACKS / res["mp3"]
    dur = probe_duration(mp3)
    thumb = tid if (THUMBS / (tid + ".jpg")).exists() else ""
    tr = {"id": tid, "title": title, "artist": artist, "file": res["mp3"], "dur": dur, "thumb": thumb, "source": "youtube", "url": req.url}
    lib["tracks"].append(tr)
    save_lib(lib)
    return {"track": tr}

@app.delete("/api/tracks/{tid}")
def del_track(tid: str):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404)
    lib["tracks"] = [x for x in lib["tracks"] if x["id"] != tid]
    for pl in lib["playlists"]:
        pl["tracks"] = [x for x in pl["tracks"] if x != tid]
    (TRACKS / t["file"]).unlink(missing_ok=True)
    (THUMBS / (tid + ".jpg")).unlink(missing_ok=True)
    save_lib(lib)
    return {"ok": True}

# ---------- Playlists ----------
class PLReq(BaseModel):
    name: str

class PLAdd(BaseModel):
    track_ids: list[str]

@app.post("/api/playlists")
def create_pl(req: PLReq):
    lib = load_lib()
    pid = uuid.uuid4().hex[:10]
    lib["playlists"].append({"id": pid, "name": req.name, "tracks": []})
    save_lib(lib)
    return {"id": pid}

@app.post("/api/playlists/{pid}/tracks")
def add_to_pl(pid: str, req: PLAdd):
    lib = load_lib()
    pl = next((p for p in lib["playlists"] if p["id"] == pid), None)
    if not pl:
        raise HTTPException(404, "playlist no existe")
    for t in req.track_ids:
        if t not in pl["tracks"]:
            pl["tracks"].append(t)
    save_lib(lib)
    return {"ok": True}

@app.delete("/api/playlists/{pid}")
def del_pl(pid: str):
    lib = load_lib()
    lib["playlists"] = [p for p in lib["playlists"] if p["id"] != pid]
    save_lib(lib)
    return {"ok": True}

# ---------- estáticos ----------
STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
