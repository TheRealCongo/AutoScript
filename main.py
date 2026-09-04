"""CDCT - CLI entry point.

Captures the default Windows output device (WASAPI loopback) in rolling
chunks, transcribes each chunk with faster-whisper, and appends timestamped
lines to a markdown transcript as it goes. Works with any audio source, not
tied to a specific application.

Usage:
    venv\\Scripts\\python.exe main.py [--chunk-seconds 30] [--model small] [--target-process Zoom.exe]
"""
import argparse
import queue
import signal
import sys
import time
from datetime import date
from pathlib import Path

from capture import LoopbackRecorder, process_running
from transcriber import TranscriptionWorker, load_model

ROOT = Path(__file__).parent
CHUNKS_DIR = ROOT / "chunks"
TRANSCRIPTS_DIR = ROOT / "transcripts"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-seconds", type=int, default=30)
    parser.add_argument(
        "--model", default="small", help="faster-whisper model size: tiny/base/small/medium"
    )
    parser.add_argument(
        "--keep-audio", action="store_true", help="Keep WAV chunks after transcription"
    )
    parser.add_argument(
        "--target-process",
        default=None,
        help="Optional process name (e.g. Discord.exe) to check is running before recording "
             "starts. Purely informational - capture always covers the full output device.",
    )
    args = parser.parse_args()

    CHUNKS_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    if args.target_process and not process_running(args.target_process):
        print(
            f"[WARN] {args.target_process} not detected. Capturing default output device "
            "regardless - anything else playing audio will bleed into the transcript."
        )

    today = date.today().isoformat()
    transcript_path = TRANSCRIPTS_DIR / f"transcript_{today}_{int(time.time())}.md"
    transcript_path.write_text(f"## Transcript — {today}\n\n", encoding="utf-8")

    print(f"[INFO] Transcript file: {transcript_path}")
    print(f"[INFO] Loading faster-whisper model '{args.model}' ...")
    model, device = load_model(args.model)
    print(f"[INFO] Model loaded on {device}.")

    chunk_q: "queue.Queue" = queue.Queue()

    recorder = LoopbackRecorder(args.chunk_seconds, CHUNKS_DIR, chunk_q)
    print(f"[INFO] Capturing loopback device: {recorder.device['name']}")

    worker = TranscriptionWorker(
        model, chunk_q, transcript_path, delete_chunks=not args.keep_audio
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
