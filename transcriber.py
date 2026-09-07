"""Pulls finished audio chunks off a queue, transcribes them, appends to a
markdown transcript with wall-clock timestamps."""
import ctypes
import gc
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from faster_whisper import WhisperModel


def _cuda_runtime_available() -> bool:
    """Return whether this Windows installation can load faster-whisper's CUDA runtime."""
    if sys.platform != "win32":
        return False
    try:
        # CTranslate2 4.8 uses CUDA 12 and cuDNN 9. Loading both catches a
        # partial NVIDIA installation before a recording thread is started.
        ctypes.WinDLL("cublas64_12.dll")
        ctypes.WinDLL("cudnn64_9.dll")
    except OSError:
        return False
    return True


def load_model(model_size: str) -> tuple[WhisperModel, str]:
    """Load on CUDA only when its required runtime libraries are usable.

    A GPU driver alone is not sufficient for faster-whisper. Checking the CUDA
    libraries first avoids a synthetic inference that can leave the UI stuck at
    "Loading transcription model" on partial Windows CUDA installations.
    """
    if _cuda_runtime_available():
        try:
            model = WhisperModel(model_size, device="cuda", compute_type="float16")
            return model, "cuda"
        except Exception:
            pass
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
        speaker_label: str | None = None,
        write_line: Optional[Callable[[str], None]] = None,
    ):
        self.model = model
        self.chunk_queue = chunk_queue
        self.transcript_path = transcript_path
        self.delete_chunks = delete_chunks
        self.on_line = on_line
        self.speaker_label = speaker_label
        self.write_line = write_line
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
                item = self.chunk_queue.get(timeout=1)
                chunk_path, chunk_start_wall, *metadata = item
            except queue.Empty:
                continue

            try:
                speaker_label = metadata[0] if metadata else self.speaker_label
                self._transcribe_chunk(chunk_path, chunk_start_wall, speaker_label)
            except Exception as exc:
                self._append_line(f"**[ERROR transcribing {chunk_path.name}: {exc}]**")
            finally:
                if self.delete_chunks:
                    self._delete_chunk(chunk_path)
                self.chunk_queue.task_done()

    def _transcribe_chunk(
        self, chunk_path: Path, chunk_start_wall: float, speaker_label: str | None = None
    ):
        segments, _info = self.model.transcribe(str(chunk_path), vad_filter=True)
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            ts = time.localtime(chunk_start_wall + seg.start)
            stamp = time.strftime("%H:%M:%S", ts)
            speaker = f"**{speaker_label}:** " if speaker_label else ""
            self._append_line(f"**[{stamp}]** {speaker}{text}")

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
        if self.write_line is not None:
            self.write_line(line)
        else:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.on_line:
            self.on_line(line)
