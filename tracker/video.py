"""Render the play-through as an MP4: Playwright steps the chart's clock frame by frame, ffmpeg encodes.

    python run.py video incidents/x.yaml [--seconds 60] [--fps 30]

Needs: pip install playwright && playwright install chromium ; ffmpeg on PATH (or pip install imageio-ffmpeg).
Output: out/<slug>/<slug>.mp4 — 1920x1296 (3:2), H.264, suitable for X / LinkedIn / embedding.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import Incident

FRAME_W, FRAME_H, SCALE = 1600, 1080, 1.2


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found: install it, or `pip install imageio-ffmpeg`")


def video(inc: Incident, seconds: float = 60.0, fps: int = 30, hold_start: float = 1.0, hold_end: float = 3.0) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")

    page_path = inc.out_dir / "index.html"
    if not page_path.exists():
        sys.exit("no index.html yet — run render first")
    out = inc.out_dir / f"{inc.slug}.mp4"
    n_frames = int(seconds * fps)
    n_hold_start, n_hold_end = int(hold_start * fps), int(hold_end * fps)

    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert enc.stdin is not None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": FRAME_W, "height": FRAME_H}, device_scale_factor=SCALE)
        page.goto(page_path.resolve().as_uri() + "?video=1")
        page.wait_for_function("window.__tracker !== undefined", timeout=20000)
        page.wait_for_timeout(500)
        span_minutes = page.evaluate("window.__tracker.span / 60000")
        rate_label = f"{round(span_minutes / seconds)} minutes per second"

        def frame(progress: float) -> None:
            page.evaluate("([f, lbl]) => { window.__tracker.setProgress(f); window.__tracker.setSpeedLabel(lbl); }",
                          [progress, rate_label])
            enc.stdin.write(page.screenshot(type="jpeg", quality=92))

        print(f"rendering {n_frames + n_hold_start + n_hold_end} frames at {fps} fps -> {out.name}", flush=True)
        for _ in range(n_hold_start):
            frame(0.0)
        for i in range(n_frames):
            frame(i / max(1, n_frames - 1))
            if i % (fps * 5) == 0:
                print(f"  {i / fps:5.1f}s / {seconds:.0f}s", flush=True)
        page.evaluate("window.__tracker.setSpeedLabel('end of window')")
        for _ in range(n_hold_end):
            enc.stdin.write(page.screenshot(type="jpeg", quality=92))
        browser.close()

    enc.stdin.close()
    enc.wait()
    if enc.returncode != 0:
        sys.exit(f"ffmpeg failed ({enc.returncode})")
    print(f"wrote {out}  ({out.stat().st_size // 1024} KB, {seconds + hold_start + hold_end:.0f}s)")
    return out
