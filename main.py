import os
import tempfile
import uuid
import subprocess
from typing import List, Optional, Literal

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

app = FastAPI(title="Media Downloader & Converter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Storage setup
DOWNLOAD_ROOT = os.environ.get("DOWNLOAD_ROOT", "/tmp/downloads")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

class DownloadRequest(BaseModel):
    url: str = Field(..., description="Source media URL")
    format: Literal["mp3","mp4","wav","mkv","webm","m4a","opus"] = "mp4"
    quality: Optional[str] = Field("best", description="yt-dlp format string or preset like 'best', 'worst' or 'bestvideo+bestaudio'")
    subtitles: bool = False
    subtitle_langs: Optional[List[str]] = Field(default_factory=lambda: ["en"], description="Subtitle languages")
    embed_subs: bool = False
    audio_only: bool = False
    filename_template: Optional[str] = Field(None, description="Output template, yt-dlp style")
    ffmpeg_args: Optional[List[str]] = Field(default=None, description="Extra FFmpeg args to pass during post-processing")

class BatchRequest(BaseModel):
    urls: List[str]
    common: Optional[DownloadRequest] = None

class ScheduledTask(BaseModel):
    when: str
    request: DownloadRequest

@app.get("/")
def root():
    return {"message": "Media Downloader & Converter API running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


def run_cmd(cmd: List[str]):
    try:
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, text=True)
        return completed.stdout
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e.stdout[-2000:] if e.stdout else str(e))


def yt_dlp_download(req: DownloadRequest) -> str:
    session_id = str(uuid.uuid4())
    out_dir = os.path.join(DOWNLOAD_ROOT, session_id)
    os.makedirs(out_dir, exist_ok=True)

    # Build output template
    if req.filename_template:
        outtmpl = os.path.join(out_dir, req.filename_template)
    else:
        outtmpl = os.path.join(out_dir, "%(title)s-%(id)s.%(ext)s")

    args = [
        "yt-dlp",
        req.url,
        "-o", outtmpl,
        "--merge-output-format", req.format,
    ]

    # Quality selections
    if req.audio_only:
        args += ["-x", "--audio-format", req.format]
    else:
        if req.quality and req.quality not in ("best", "worst"):
            args += ["-f", req.quality]
        else:
            # Default: bestvideo+bestaudio for high quality
            args += ["-f", "bestvideo+bestaudio/best"]

    # Subtitles
    if req.subtitles:
        langs = ",".join(req.subtitle_langs or ["en"])
        args += ["--write-subs", "--sub-langs", langs]
        if req.embed_subs:
            args += ["--embed-subs"]

    # FFmpeg location (in env) - optional
    if os.environ.get("FFMPEG_PATH"):
        args += ["--ffmpeg-location", os.environ["FFMPEG_PATH"]]

    # SponsorBlock integration toggled via env
    if os.environ.get("ENABLE_SPONSORBLOCK", "false").lower() == "true":
        args += ["--sponsorblock-remove", "sponsor,intro,outro"]

    output = run_cmd(args)

    # Pick the first created file to return path hint
    # We can scan out_dir
    chosen_file = None
    for root, _, files in os.walk(out_dir):
        for f in files:
            if f.endswith((".mp4",".mp3",".mkv",".webm",".m4a",".opus",".wav")):
                chosen_file = os.path.join(root, f)
                break
        if chosen_file:
            break

    if not chosen_file:
        # If not found, just return directory
        chosen_file = out_dir

    # Log to DB
    try:
        create_document("history", {
            "session_id": session_id,
            "url": req.url,
            "format": req.format,
            "audio_only": req.audio_only,
            "subtitles": req.subtitles,
            "embed_subs": req.embed_subs,
            "out_dir": out_dir,
            "output_hint": chosen_file,
            "stdout": output,
        })
    except Exception:
        pass

    return chosen_file


@app.post("/api/download")
async def download_media(req: DownloadRequest):
    path = yt_dlp_download(req)
    return {"path": path}


class ConvertRequest(BaseModel):
    input_path: str
    output_format: Literal["mp3","mp4","wav","mkv","webm","m4a","opus"]
    start: Optional[str] = None  # e.g. "00:00:10"
    end: Optional[str] = None
    extra_args: Optional[List[str]] = None


def ffmpeg_convert(req: ConvertRequest) -> str:
    if not os.path.exists(req.input_path):
        raise HTTPException(status_code=404, detail="Input file not found")

    base, _ = os.path.splitext(req.input_path)
    out_path = f"{base}_conv.{req.output_format}"

    cmd = ["ffmpeg", "-y", "-i", req.input_path]
    if req.start:
        cmd += ["-ss", req.start]
    if req.end:
        cmd += ["-to", req.end]
    if req.extra_args:
        cmd += req.extra_args
    cmd += [out_path]

    run_cmd(cmd)

    try:
        create_document("conversions", {
            "input": req.input_path,
            "output": out_path,
            "extra_args": req.extra_args,
        })
    except Exception:
        pass

    return out_path


@app.post("/api/convert")
async def convert_media(req: ConvertRequest):
    out_path = ffmpeg_convert(req)
    return {"output": out_path}


class ProbeResponse(BaseModel):
    raw: str


@app.get("/api/probe")
async def probe_media(path: str):
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    output = run_cmd(["ffprobe", "-hide_banner", "-i", path])
    return {"raw": output}


@app.get("/api/file")
async def get_file(path: str):
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    filename = os.path.basename(path)
    return FileResponse(path, filename=filename)


class HistoryQuery(BaseModel):
    limit: int = 20


@app.get("/api/history")
async def history(limit: int = 20):
    try:
        docs = get_documents("history", limit=limit)
        # Convert ObjectId
        for d in docs:
            d["_id"] = str(d.get("_id"))
        return {"items": docs}
    except Exception:
        return {"items": []}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
