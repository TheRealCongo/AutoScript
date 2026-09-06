"""AutoScript - CLI entry point.

Captures either a selected process or the default Windows output device in
rolling chunks, transcribes each chunk with faster-whisper, and saves
timestamped lines to an encrypted .asenc transcript as it goes.

Usage:
    venv\\Scripts\\python.exe main.py [--chunk-seconds 10] [--model small] [--target-process Zoom.exe]
"""
import argparse
import os
import queue
import re
import secrets
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path

import psutil

from capture import make_recorder
from transcriber import TranscriptionWorker, load_model
from transcript_security import EncryptedLog, TokenVault, new_token

ROOT = Path(__file__).parent
CHUNKS_DIR = ROOT / "chunks"
TRANSCRIPTS_DIR = ROOT / "transcripts"


def _resolve_pid(process_name: str) -> "int | None":
    """First running process matching `process_name` by exe name, or None."""
    target = process_name.strip().lower()
    if not target.endswith(".exe"):
        target += ".exe"
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == target:
                return proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _safe_filename_stem(label: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", label).strip(" ._")
    return cleaned[:80] or "SystemAudio"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-seconds", type=int, default=10)
    parser.add_argument(
        "--model", default="small", help="faster-whisper model size: tiny/base/small/medium"
    )
    parser.add_argument(
        "--keep-audio", action="store_true", help="Keep WAV chunks after transcription"
    )
    parser.add_argument(
        "--target-process",
        default=None,
        help="Optional process name (e.g. Discord.exe) whose audio to isolate. Falls back "
             "to capturing the full output device (with a warning printed) if it can't be "
             "isolated - see capture.py's ProcessLoopbackRecorder for why that can happen.",
    )
    args = parser.parse_args()

    CHUNKS_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    target_pid = None
    if args.target_process:
        target_pid = _resolve_pid(args.target_process)
        if target_pid is None:
            print(
                f"[WARN] {args.target_process} not detected. Capturing default output device "
                "regardless - anything else playing audio will bleed into the transcript."
            )

    today = date.today().isoformat()
    target_label = Path(args.target_process).stem if args.target_process else "SystemAudio"
    stamp = datetime.now().strftime("%d%b%y_%H%M%S").upper()
    transcript_path = TRANSCRIPTS_DIR / f"{_safe_filename_stem(target_label)}_{stamp}.asenc"
    sharing_token = new_token()
    record_id = secrets.token_urlsafe(16)
    token_vault = TokenVault(
        Path(os.environ.get("APPDATA", str(Path.home()))) / "AutoScript" / "transcript-tokens.json"
    )
    token_vault.save(record_id, sharing_token)
    encrypted_log = EncryptedLog(transcript_path, sharing_token, record_id, f"## Transcript — {today}\n\n")

    print(f"[INFO] Transcript file: {transcript_path}")
    print(f"[INFO] Sharing token: {sharing_token}")
    print(f"[INFO] Loading faster-whisper model '{args.model}' ...")
    model, device = load_model(args.model)
    print(f"[INFO] Model loaded on {device}.")

    chunk_q: "queue.Queue" = queue.Queue()

    recorder, mode, reason = make_recorder(args.chunk_seconds, CHUNKS_DIR, chunk_q, target_pid=target_pid)
    if mode == "process":
        print(f"[INFO] Capturing isolated audio for {args.target_process} (pid {target_pid})")
    else:
        if target_pid is not None and reason:
            print(f"[WARN] Could not isolate {args.target_process}'s audio ({reason}) - capturing full system output instead.")
        print(f"[INFO] Capturing loopback device: {recorder.device['name']}")

    worker = TranscriptionWorker(
        model, chunk_q, transcript_path, delete_chunks=not args.keep_audio,
        write_line=encrypted_log.append,
    )

    stop_requested = {"flag": False}

    def handle_sigint(signum, frame):
        if stop_requested["flag"]:
            print("\n[INFO] Force exit.")
            sys.exit(1)
        stop_requested["flag"] = True
        print("\n[INFO] Stopping - finishing current chunk and draining transcription queue...")

    signal.signal(signal.SIGINT, handle_sigint)

    recorder.start()
    worker.start()

    print("[INFO] Recording. Press Ctrl+C to stop.")
    while not stop_requested["flag"]:
        time.sleep(0.5)

    recorder.stop()
    worker.stop(drain=True)
    print(f"[INFO] Done. Transcript saved to {transcript_path}")


if __name__ == "__main__":
    main()
