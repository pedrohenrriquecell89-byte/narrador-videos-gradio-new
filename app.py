"""
FastAPI backend for automated narrated-video generation.
Single-user, no auth. Pipeline: script+photos -> TTS (Piper) ->
word-level alignment (faster-whisper) -> scene timing -> FFmpeg render.
"""

import json
import shutil
import logging
import traceback
import uuid
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from pipeline import validate, tts, align, assemble

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("video-narrator")

app = FastAPI(title="Video Narrator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# In-memory job status (single-user tool). Mirrored to disk for resilience
# across a Render free-tier sleep/wake cycle within the same deploy.
JOBS = {}


def set_status(job_id, stage, progress=0, message="", error=None, done=False, download_ready=False):
    status = {
        "stage": stage,
        "progress": progress,
        "message": message,
        "error": error,
        "done": done,
        "download_ready": download_ready,
    }
    JOBS[job_id] = status
    try:
        (JOBS_DIR / job_id / "status.json").write_text(json.dumps(status))
    except Exception:
        log.warning("Could not persist status for job %s", job_id, exc_info=True)


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload(script: str = Form(...), images: list[UploadFile] = File(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    (job_dir / "images").mkdir(parents=True, exist_ok=True)

    try:
        # ---- Validate everything before spending any processing time ----
        validate.validate_script(script)
        scenes = validate.split_scenes(script)
        validate.validate_images(images, len(scenes))

        (job_dir / "script.txt").write_text(script, encoding="utf-8")

        for i, img in enumerate(images):
            ext = validate.safe_ext(img.filename)
            dest = job_dir / "images" / f"{i:04d}{ext}"
            content = await img.read()
            validate.validate_image_bytes(content, img.filename)
            dest.write_bytes(content)

    except validate.ValidationError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("Upload failed: %s", e, exc_info=True)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Upload failed. Please try again.")

    set_status(job_id, "queued", 0, "Job queued.")
    Thread(target=run_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


def run_pipeline(job_id: str):
    job_dir = JOBS_DIR / job_id
    try:
        set_status(job_id, "tts", 10, "Generating narration audio...")
        script = (job_dir / "script.txt").read_text(encoding="utf-8")
        scenes = validate.split_scenes(script)
        wav_path = job_dir / "narration.wav"
        tts.synthesize(script, wav_path)

        set_status(job_id, "align", 35, "Aligning narration word-by-word...")
        words = align.get_word_timestamps(wav_path)

        set_status(job_id, "planning", 55, "Calculating scene timing...")
        scene_times = assemble.plan_scene_timing(scenes, words)

        images_dir = job_dir / "images"
        image_files = sorted(images_dir.glob("*"))

        set_status(job_id, "render", 65, "Rendering video (this can take a few minutes)...")
        output_path = job_dir / "final.mp4"
        assemble.render_video(
            scene_times, image_files, wav_path, output_path,
            progress_cb=lambda p, m: set_status(job_id, "render", 65 + int(p * 30), m),
        )

        set_status(job_id, "done", 100, "Video ready!", done=True, download_ready=True)

    except Exception as e:
        log.error("Pipeline failed for job %s: %s", job_id, e, exc_info=True)
        (job_dir / "error.log").write_text(traceback.format_exc())
        set_status(job_id, "error", 0, "", error=str(e), done=True)


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in JOBS:
        status_file = JOBS_DIR / job_id / "status.json"
        if status_file.exists():
            return json.loads(status_file.read_text())
        raise HTTPException(status_code=404, detail="Job not found.")
    return JOBS[job_id]


@app.get("/download/{job_id}")
def download(job_id: str):
    path = JOBS_DIR / job_id / "final.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not ready or job not found.")
    return FileResponse(path, media_type="video/mp4", filename="video.mp4")
