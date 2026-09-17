"""
Video Auto - automação de edição de vídeo narrado a partir de roteiro + fotos.

Arquitetura de sincronia (decisão de design):
  Em vez de gerar UM áudio completo e depois tentar alinhar/transcrever
  (abordagem WhisperX), o roteiro é dividido em N blocos (N = número de
  fotos) e cada bloco vira um arquivo de áudio PRÓPRIO via Piper TTS.
  Cada foto fica em tela exatamente pela duração do SEU bloco de áudio.
  Isso garante sincronia perfeita por construção (sem deriva, sem
  necessidade de alinhamento posterior) e usa muito menos memória —
  essencial no plano gratuito do Render (512MB RAM).
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

VOICE_MODEL = BASE_DIR / "voices" / "en_US-hfc_male-medium.onnx"
PIPER_BIN = "piper"  # está no PATH (instalado no Dockerfile)

MAX_VIDEO_SECONDS = 10 * 60  # limite de 10 minutos
FADE_DURATION = 0.4  # segundos de fade in/out por imagem
TARGET_W, TARGET_H = 1920, 1080
FPS = 30
JOB_TTL_SECONDS = 2 * 60 * 60  # limpa jobs com mais de 2h (disco efêmero)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("videoauto")

app = FastAPI(title="Video Auto")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Estado dos jobs em memória (uso single-user, sem banco de dados)
JOBS = {}
JOBS_LOCK = Lock()


def set_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def job_log_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "job.log"


def log_error(job_id: str, message: str):
    """Grava erro no log do job para diagnóstico, sem travar o servidor."""
    try:
        path = job_log_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        logger.exception("Falha ao gravar log do job %s", job_id)


# ---------------------------------------------------------------------------
# Utilitários de shell / ffmpeg
# ---------------------------------------------------------------------------

def run_cmd(cmd: List[str], job_id: str = None, timeout: int = 600):
    """Executa um comando e levanta erro claro em caso de falha."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Comando excedeu tempo limite: {' '.join(cmd)}") from e
    except FileNotFoundError as e:
        raise RuntimeError(f"Comando não encontrado: {cmd[0]} (instalação incompleta)") from e

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore")[-2000:]
        if job_id:
            log_error(job_id, f"CMD FALHOU: {' '.join(cmd)}\nSTDERR: {stderr}")
        raise RuntimeError(f"Falha ao executar '{cmd[0]}': {stderr[:300]}")
    return result


def ffprobe_duration(path: Path) -> float:
    result = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(result.stdout.decode().strip())
    except ValueError:
        raise RuntimeError(f"Não foi possível obter duração de {path.name}")


# ---------------------------------------------------------------------------
# Divisão do roteiro em N blocos (um por foto)
# ---------------------------------------------------------------------------

def split_script_into_segments(script: str, n_segments: int) -> List[str]:
    """Divide o roteiro em n_segments blocos, balanceados por contagem de
    palavras, sem cortar frases ao meio."""
    sentences = re.split(r"(?<=[.!?])\s+", script.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) < n_segments:
        raise ValueError(
            f"O roteiro tem apenas {len(sentences)} frase(s), mas há "
            f"{n_segments} foto(s). Adicione mais texto ou reduza o número "
            f"de fotos (é necessária pelo menos 1 frase por foto)."
        )

    total_words = sum(len(s.split()) for s in sentences)
    target_per_segment = total_words / n_segments

    segments, current, current_words = [], [], 0
    for sent_idx, sentence in enumerate(sentences):
        current.append(sentence)
        current_words += len(sentence.split())
        remaining_segments = n_segments - len(segments)
        remaining_sentences = len(sentences) - (sent_idx + 1)
        if (
            current_words >= target_per_segment
            and len(segments) < n_segments - 1
            and remaining_sentences >= remaining_segments - 1
        ):
            segments.append(" ".join(current))
            current, current_words = [], 0

    if current:
        segments.append(" ".join(current))

    # Ajuste final de segurança: garante exatamente n_segments blocos
    while len(segments) < n_segments:
        # divide o maior segmento em dois
        idx = max(range(len(segments)), key=lambda i: len(segments[i]))
        words = segments[idx].split()
        mid = max(1, len(words) // 2)
        segments[idx:idx + 1] = [" ".join(words[:mid]), " ".join(words[mid:])]
    while len(segments) > n_segments:
        segments[-2] = segments[-2] + " " + segments[-1]
        segments.pop()

    return segments


# ---------------------------------------------------------------------------
# Pipeline principal do job
# ---------------------------------------------------------------------------

def process_job(job_id: str):
    job_dir = JOBS_DIR / job_id
    images_dir = job_dir / "images"
    work_dir = job_dir / "work"
    work_dir.mkdir(exist_ok=True)

    try:
        job = get_job(job_id)
        script_text = (job_dir / "script.txt").read_text(encoding="utf-8")
        image_paths = sorted(images_dir.iterdir(), key=lambda p: p.name)

        # 1) Dividir roteiro em blocos (1 por imagem) -------------------------
        set_job(job_id, status="processing", stage="Dividindo roteiro", progress=5)
        segments = split_script_into_segments(script_text, len(image_paths))

        # 2) Gerar áudio de cada bloco via Piper --------------------------------
        audio_paths = []
        for i, segment_text in enumerate(segments):
            set_job(
                job_id, stage=f"Gerando narração ({i + 1}/{len(segments)})",
                progress=10 + int(30 * (i + 1) / len(segments)),
            )
            out_wav = work_dir / f"audio_{i:03d}.wav"
            proc = subprocess.run(
                [PIPER_BIN, "--model", str(VOICE_MODEL), "--output_file", str(out_wav),
                 "--length_scale", "1.0", "--noise_scale", "0.667", "--noise_w", "0.8"],
                input=segment_text.encode("utf-8"),
                capture_output=True, timeout=180,
            )
            if proc.returncode != 0 or not out_wav.exists():
                stderr = proc.stderr.decode("utf-8", errors="ignore")[-1500:]
                log_error(job_id, f"Piper falhou no bloco {i}: {stderr}")
                raise RuntimeError(
                    f"Falha ao gerar narração do bloco {i + 1}. "
                    f"Verifique se o texto está em inglês e sem caracteres inválidos."
                )
            audio_paths.append(out_wav)

        # 3) Medir duração de cada bloco de áudio -------------------------------
        set_job(job_id, stage="Medindo durações", progress=42)
        durations = [ffprobe_duration(a) for a in audio_paths]
        total_duration = sum(durations)

        if total_duration > MAX_VIDEO_SECONDS:
            raise ValueError(
                f"O vídeo gerado teria {total_duration / 60:.1f} min, acima do "
                f"limite de 10 min. Reduza o tamanho do roteiro."
            )
        if total_duration < 0.5:
            raise ValueError("Áudio gerado é vazio ou inválido demais para renderizar.")

        # 4) Criar um clipe de vídeo por imagem, com a duração exata do áudio --
        clip_paths = []
        for i, (img_path, duration) in enumerate(zip(image_paths, durations)):
            set_job(
                job_id, stage=f"Montando cena ({i + 1}/{len(image_paths)})",
                progress=45 + int(30 * (i + 1) / len(image_paths)),
            )
            clip_path = work_dir / f"clip_{i:03d}.mp4"
            fade = min(FADE_DURATION, max(0.05, duration / 4))
            fade_out_start = max(0.0, duration - fade)
            vf = (
                f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={FPS},format=yuv420p,"
                f"fade=t=in:st=0:d={fade:.3f},"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"
            )
            run_cmd([
                "ffmpeg", "-y", "-loop", "1", "-i", str(img_path),
                "-t", f"{duration:.3f}", "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-an", str(clip_path),
            ], job_id=job_id)
            clip_paths.append(clip_path)

        # 5) Concatenar clipes de vídeo (silenciosos) ---------------------------
        set_job(job_id, stage="Concatenando cenas", progress=78)
        concat_list = work_dir / "clips.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.name}'" for p in clip_paths), encoding="utf-8"
        )
        silent_video = work_dir / "silent_video.mp4"
        run_cmd([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list), "-c", "copy", str(silent_video),
        ], job_id=job_id, timeout=300)

        # 6) Concatenar áudios e normalizar volume ------------------------------
        set_job(job_id, stage="Normalizando áudio", progress=85)
        audio_list = work_dir / "audios.txt"
        audio_list.write_text(
            "\n".join(f"file '{p.name}'" for p in audio_paths), encoding="utf-8"
        )
        full_audio = work_dir / "full_audio.wav"
        run_cmd([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(audio_list), "-c", "copy", str(full_audio),
        ], job_id=job_id)

        normalized_audio = work_dir / "normalized_audio.wav"
        run_cmd([
            "ffmpeg", "-y", "-i", str(full_audio),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
            str(normalized_audio),
        ], job_id=job_id)

        # 7) Validação de sincronia (checagem de segurança) ---------------------
        video_duration = ffprobe_duration(silent_video)
        audio_duration = ffprobe_duration(normalized_audio)
        drift = abs(video_duration - audio_duration)
        if drift > 0.2:
            log_error(
                job_id,
                f"Divergência de sincronia detectada: vídeo={video_duration:.3f}s "
                f"áudio={audio_duration:.3f}s (drift={drift:.3f}s)",
            )
            # não é fatal (o mux abaixo usa -shortest como rede de segurança),
            # mas registramos para diagnóstico.

        # 8) Mux final -----------------------------------------------------------
        set_job(job_id, stage="Renderizando vídeo final", progress=93)
        final_path = job_dir / "final.mp4"
        run_cmd([
            "ffmpeg", "-y", "-i", str(silent_video), "-i", str(normalized_audio),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(final_path),
        ], job_id=job_id, timeout=300)

        if not final_path.exists() or final_path.stat().st_size < 1024:
            raise RuntimeError("Arquivo final não foi gerado corretamente.")

        set_job(
            job_id, status="done", stage="Concluído", progress=100,
            output_path=str(final_path), error=None,
        )
        logger.info("Job %s concluído com sucesso (%.1fs de vídeo)", job_id, video_duration)

    except Exception as e:
        logger.exception("Job %s falhou", job_id)
        log_error(job_id, f"ERRO FATAL: {e}")
        set_job(job_id, status="error", error=str(e), stage="Erro")
    finally:
        # libera espaço em disco (mantém apenas o vídeo final e o log)
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def cleanup_old_jobs():
    """Remove jobs antigos do disco (armazenamento efêmero, single-user)."""
    now = time.time()
    for job_dir in JOBS_DIR.iterdir():
        try:
            if job_dir.is_dir() and (now - job_dir.stat().st_mtime) > JOB_TTL_SECONDS:
                shutil.rmtree(job_dir, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(job_dir.name, None)
        except Exception:
            logger.exception("Falha ao limpar job antigo %s", job_dir)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/jobs")
async def create_job(script: str = Form(...), images: List[UploadFile] = File(...)):
    cleanup_old_jobs()

    # ---- Validações antes de gastar qualquer processamento ----
    script = script.strip()
    if not script:
        raise HTTPException(400, "O roteiro está vazio. Cole o texto em inglês antes de enviar.")
    if len(script.split()) < 5:
        raise HTTPException(400, "Roteiro muito curto. Escreva pelo menos algumas frases.")
    if len(script.split()) > 1600:
        raise HTTPException(
            400,
            "Roteiro muito longo (pode passar de 10 minutos de narração). "
            "Reduza o texto para no máximo ~1500 palavras.",
        )
    if not images:
        raise HTTPException(400, "Envie pelo menos uma foto.")
    if len(images) > 200:
        raise HTTPException(400, "Número de fotos muito alto (máximo 200).")

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    images_dir = job_dir / "images"
    images_dir.mkdir(parents=True)

    # Salva e valida cada imagem (rejeita o job inteiro se alguma for inválida,
    # evitando gastar processamento depois para descobrir o erro no meio)
    for idx, upload in enumerate(images):
        content = await upload.read()
        if not content:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, f"Arquivo de imagem vazio: {upload.filename}")
        img_path = images_dir / f"{idx:04d}.jpg"
        tmp_path = images_dir / f"{idx:04d}._raw"
        tmp_path.write_bytes(content)
        try:
            with Image.open(tmp_path) as im:
                im.verify()
            with Image.open(tmp_path) as im:
                im.convert("RGB").save(img_path, "JPEG", quality=92)
        except (UnidentifiedImageError, OSError):
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                400, f"Arquivo inválido ou corrompido: {upload.filename} (posição {idx + 1})"
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    (job_dir / "script.txt").write_text(script, encoding="utf-8")

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued", "stage": "Na fila", "progress": 0, "error": None,
        }

    asyncio.get_event_loop().run_in_executor(None, process_job, job_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado (pode ter expirado).")
    return JSONResponse({
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": job.get("progress", 0),
        "error": job.get("error"),
    })


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado.")
    if job.get("status") != "done":
        raise HTTPException(409, "O vídeo ainda não está pronto.")
    path = Path(job["output_path"])
    if not path.exists():
        raise HTTPException(410, "Arquivo expirou ou foi removido do servidor.")
    return FileResponse(path, media_type="video/mp4", filename="video_final.mp4")
