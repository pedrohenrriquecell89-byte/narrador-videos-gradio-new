"""Scene timing calculation and FFmpeg video assembly.

Strategy:
 1. Distribute the whisper word list across scenes using the script's own
    word counts (index-based mapping). This locks scene boundaries to the
    real narration audio.
 2. If the transcribed word count is far off from the script's word count,
    fall back to proportional distribution by word share, so timing
    degrades gracefully instead of crashing.
 3. Render each scene as its own clip (image, exact duration, fade
    in/out), concatenate them, then mux with the full narration audio.
"""

import json
import logging
import subprocess
import shutil
from pathlib import Path

log = logging.getLogger("video-narrator")

FADE = 0.35  # seconds, short fade in/out per scene
WIDTH, HEIGHT = 1920, 1080


class AssembleError(Exception):
    pass


def _run(cmd, timeout=1800):
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise AssembleError(result.stderr.decode(errors="ignore")[-1500:])
    return result


def plan_scene_timing(scenes, words):
    total_duration = words[-1]["end"] if words else 0
    script_word_counts = [len(s.split()) for s in scenes]
    total_script_words = sum(script_word_counts)

    use_index_mapping = bool(words) and abs(len(words) - total_script_words) <= max(3, 0.15 * total_script_words)

    scene_times = []
    if use_index_mapping:
        idx = 0
        for count in script_word_counts:
            chunk = words[idx: idx + count] or words[max(0, idx - 1): idx]
            start = chunk[0]["start"] if chunk else (scene_times[-1]["end"] if scene_times else 0)
            end = chunk[-1]["end"] if chunk else start
            scene_times.append({"start": start, "end": end})
            idx += count
        # Snap boundaries so end-of-scene == start-of-next-scene: no gaps, no overlaps.
        for i in range(len(scene_times) - 1):
            scene_times[i]["end"] = scene_times[i + 1]["start"]
        scene_times[-1]["end"] = total_duration
    else:
        log.warning(
            "Word-count mismatch (script=%d, transcribed=%d); using proportional timing.",
            total_script_words, len(words),
        )
        t = 0.0
        for count in script_word_counts:
            share = (count / total_script_words) * total_duration if total_script_words else 0
            scene_times.append({"start": t, "end": t + share})
            t += share

    if scene_times:
        scene_times[0]["start"] = 0.0
    return scene_times


def render_video(scene_times, image_files, narration_wav: Path, output_path: Path, progress_cb=None):
    if len(scene_times) != len(image_files):
        raise AssembleError("Internal error: scene/image count mismatch at render time.")

    work_dir = output_path.parent / "render_tmp"
    work_dir.mkdir(exist_ok=True)
    try:
        clip_paths = []
        n = len(scene_times)
        for i, (st, img) in enumerate(zip(scene_times, image_files)):
            duration = max(0.05, st["end"] - st["start"])
            clip_path = work_dir / f"clip_{i:04d}.mp4"
            fade_out_start = max(0, duration - FADE)
            vf = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                f"fade=t=in:st=0:d={FADE},fade=t=out:st={fade_out_start}:d={FADE}"
            )
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(img),
                "-t", f"{duration:.3f}",
                "-vf", vf,
                "-r", "30", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                str(clip_path),
            ]
            _run(cmd)
            clip_paths.append(clip_path)
            if progress_cb:
                progress_cb((i + 1) / n * 0.7, f"Rendering scene {i + 1}/{n}...")

        # Concatenate video-only clips (identical codec/params -> safe stream copy).
        concat_list = work_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths))
        video_only = work_dir / "video_only.mp4"
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(video_only),
        ])

        if progress_cb:
            progress_cb(0.85, "Mixing and normalizing audio...")

        # Normalize audio and mux with video. -shortest guards against tiny
        # rounding drift between video and audio length so there is never
        # trailing silence or a frozen last frame.
        cmd = [
            "ffmpeg", "-y", "-i", str(video_only), "-i", str(narration_wav),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
        _run(cmd)

        if progress_cb:
            progress_cb(1.0, "Done.")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
