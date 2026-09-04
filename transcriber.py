"""Pulls finished audio chunks off a queue, transcribes them, appends to a
markdown transcript with wall-clock timestamps."""
import gc
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from faster_whisper import WhisperModel


def load_model(model_size: str) -> tuple[WhisperModel, str]:
    """Try GPU first, fall back to CPU if CUDA/cuBLAS/cuDNN isn't usable.

    Model construction alone doesn't touch the CUDA libs (lazy load), so we
    have to actually run a tiny inference to know if cuBLAS/cuDNN are missing.
    """
    try:
        model = WhisperModel(model_size, device="cuda", compute_type="float16")
        list(model.transcribe(np.zeros(16000, dtype=np.float32))[0])
        return model, "cuda"
    except Exception:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        return model, "cpu"


class TranscriptionWorker:
    def __init__(
        self,
        model: WhisperModel,
        chunk_queue: "queue.Queue",
        transcript_path: Path,
        delete_chunks: bool = True,
        on_line: Optional[Callable[[str], None]] = None,
    ):
        self.model = model
        self.chunk_queue = chunk_queue
        self.transcript_path = transcript_path
        self.delete_chunks = delete_chunks
        self.on_line = on_line
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, drain: bool = True):
        self._stop.set()
        if drain and self._thread:
            self._thread.join(timeout=120)

    def _run(self):
        while not (self._stop.is_set() and self.chunk_queue.empty()):
            try:
                chunk_path, chunk_start_wall = self.chunk_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._transcribe_chunk(chunk_path, chunk_start_wall)
            except Exception as exc:
                self._append_line(f"**[ERROR transcribing {chunk_path.name}: {exc}]**")
            finally:
                if self.delete_chunks:
                    self._delete_chunk(chunk_path)
                self.chunk_queue.task_done()

    def _transcribe_chunk(self, chunk_path: Path, chunk_start_wall: float):
        segments, _info = self.model.transcribe(str(chunk_path), vad_filter=True)
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            ts = time.localtime(chunk_start_wall + seg.start)
            stamp = time.strftime("%H:%M:%S", ts)
            self._append_line(f"**[{stamp}]** {text}")

    def _delete_chunk(self, chunk_path: Path):
        # av's decoder can hold the file handle open a beat past the loop
        # that consumes transcribe()'s segment generator; retry briefly
        # instead of silently leaking the file.
        gc.collect()
        for attempt in range(5):
            try:
                chunk_path.unlink(missing_ok=True)
                return
            except OSError:
                if attempt == 4:
                    self._append_line(f"**[WARN could not delete {chunk_path.name}, left on disk]**")
                    return
                time.sleep(0.3)

    def _append_line(self, line: str):
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.on_line:
            self.on_line(line)
