"""WASAPI loopback audio capture, chunked to rolling WAV files.

Two capture paths: `LoopbackRecorder` captures the full default output
device mix; `ProcessLoopbackRecorder` isolates a single process's audio via
Windows' WASAPI process-loopback API (AUDIOCLIENT_ACTIVATION_PARAMS /
PROCESS_LOOPBACK), wrapped by the third-party `proc-tap` package rather than
hand-rolled here. `make_recorder()` is the single entry point that picks
between them and reports which one it actually built.
"""
import ctypes
import os
import queue
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

import numpy as np
import psutil
import pyaudiowpatch as pyaudio

try:
    from proctap import ProcessAudioCapture
    PROC_TAP_AVAILABLE = True
except Exception:      # broad on purpose - a native-extension load failure can surface as OSError
    PROC_TAP_AVAILABLE = False

# proc-tap always delivers this fixed format regardless of the source
# device - it's not derived per-capture the way LoopbackRecorder's is.
PROC_TAP_SAMPLE_RATE = 48000
PROC_TAP_CHANNELS = 2
PROC_TAP_BYTES_PER_FRAME = PROC_TAP_CHANNELS * 4  # float32

_user32 = ctypes.windll.user32
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_GW_OWNER = 4

# shell/system windows that technically pass the visible-top-level-window
# check but aren't apps a user would pick as a call target
_EXCLUDED_TITLES = {"Program Manager", "Windows Input Experience"}
_EXCLUDED_PROCESSES = {"searchhost.exe", "shellexperiencehost.exe", "textinputhost.exe",
                        "startmenuexperiencehost.exe"}


def list_open_windows() -> list[tuple[str, str, int]]:
    """Enumerates visible top-level windows with a taskbar presence.

    Returns a sorted list of (window_title, process_name, pid) tuples, deduped
    by process name (keeps whichever window/pid was seen first per process -
    a second window of an already-seen exe, e.g. a second browser window, is
    not offered as a separate target). Excludes this app's own process and
    common shell/system windows.

    The pid is the *main window's* owning process, which for multi-process
    apps (browsers, Discord) is not necessarily the process actually playing
    audio - but Windows' PROCESS_LOOPBACK capture defaults to including the
    whole descendant process tree of the pid it's given, so targeting this
    pid still isolates the right audio in practice (verified against a real
    multi-process Chromium instance; see cdct_proctap_spike memory).
    """
    results: dict[str, tuple[str, int]] = {}
    own_pid = os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindow(hwnd, _GW_OWNER) != 0:
            return True
        ex_style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if ex_style & _WS_EX_TOOLWINDOW:
            return True

        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title in _EXCLUDED_TITLES:
            return True

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        try:
            pname = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        if pname.lower() in _EXCLUDED_PROCESSES:
            return True

        results.setdefault(pname, (title, pid.value))
        return True

    _user32.EnumWindows(callback, 0)
    return sorted(
        ((title, pname, pid) for pname, (title, pid) in results.items()),
        key=lambda kv: kv[0].lower(),
    )


def process_running(name: str) -> bool:
    target = name.strip().lower()
    if not target:
        return False
    if not target.endswith(".exe"):
        target += ".exe"
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == target:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def get_default_loopback_device(pa: "pyaudio.PyAudio") -> dict:
    wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_speakers = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

    if not default_speakers.get("isLoopbackDevice"):
        for loopback in pa.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                default_speakers = loopback
                break
        else:
            raise RuntimeError(
                "Could not find a loopback-capable match for the default output "
                f"device ({default_speakers['name']}). Is WASAPI available?"
            )
    return default_speakers


MAX_CONSECUTIVE_STREAM_ERRORS = 5


class LoopbackRecorder:
    """Records the default output device in rolling chunks, one WAV file per chunk.

    Each finished chunk (path, wall_clock_start) is pushed onto `chunk_queue`.
    A transient WASAPI read error (e.g. the audio session briefly going
    inactive) reopens the stream and keeps going rather than silently killing
    the recording thread. `on_error(message, fatal)` - if given - is called
    for both transient hiccups and, if retries are exhausted, the final
    give-up; `fatal=True` means the recorder has stopped itself.
    """

    def __init__(
        self,
        chunk_seconds: int,
        out_dir: Path,
        chunk_queue: "queue.Queue",
        on_error=None,
    ):
        self.chunk_seconds = chunk_seconds
        self.out_dir = out_dir
        self.chunk_queue = chunk_queue
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pa = pyaudio.PyAudio()
        self.device = get_default_loopback_device(self._pa)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.chunk_seconds + 10)
        # _pa.terminate() happens inside _run()'s own finally block, so it
        # runs exactly once regardless of whether the thread stopped because
        # of this or because it gave up on its own after repeated errors.

    def _open_stream(self):
        device = self.device
        return self._pa.open(
            format=pyaudio.paInt16,
            channels=device["maxInputChannels"],
            rate=int(device["defaultSampleRate"]),
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=1024,
        )

    def _run(self):
        device = self.device
        channels = device["maxInputChannels"]
        rate = int(device["defaultSampleRate"])
        frames_per_buffer = 1024

        stream = self._open_stream()
        consecutive_errors = 0

        try:
            while not self._stop.is_set():
                chunk_start_wall = time.time()
                frames = []
                frames_needed = int(rate * self.chunk_seconds / frames_per_buffer)
                read_error = None
                for _ in range(frames_needed):
                    if self._stop.is_set():
                        break
                    try:
                        data = stream.read(frames_per_buffer, exception_on_overflow=False)
                    except Exception as exc:
                        read_error = exc
                        break
                    frames.append(data)

                if read_error is not None:
                    consecutive_errors += 1
                    if self.on_error:
                        self.on_error(f"Audio stream hiccup, reconnecting... ({read_error})", False)
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                    if consecutive_errors > MAX_CONSECUTIVE_STREAM_ERRORS:
                        if self.on_error:
                            self.on_error(
                                "Audio capture failed repeatedly and stopped - "
                                "restart the recording to try again.",
                                True,
                            )
                        return
                    time.sleep(0.5)
                    try:
                        stream = self._open_stream()
                    except Exception as exc2:
                        if self.on_error:
                            self.on_error(f"Could not reopen audio stream: {exc2}", False)
                        time.sleep(1)
                    continue

                consecutive_errors = 0
                if not frames:
                    continue

                chunk_path = self.out_dir / f"chunk_{int(chunk_start_wall)}.wav"
                with wave.open(str(chunk_path), "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(self._pa.get_sample_size(pyaudio.paInt16))
                    wf.setframerate(rate)
                    wf.writeframes(b"".join(frames))

                self.chunk_queue.put((chunk_path, chunk_start_wall))
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            try:
                self._pa.terminate()
            except Exception:
                pass


# How often the accumulator checks whether the target process has exited.
# proc-tap's backend never surfaces this on its own - it just keeps streaming
# silence forever for a dead/nonexistent pid (verified empirically, see
# cdct_proctap_spike memory) - so this is the only way to detect it.
LIVENESS_CHECK_INTERVAL = 2.0


class ProcessLoopbackRecorder:
    """Records a single process's audio (via `proc-tap`'s WASAPI process-
    loopback backend) in rolling chunks, mirroring `LoopbackRecorder`'s
    public surface so callers don't need to know which one they have.

    proc-tap's own callback runs on its internal capture thread and must
    stay cheap (no file I/O) - it just hands raw float32 PCM off to a queue
    that a separate accumulator thread turns into chunk_seconds-sized WAV
    files, same shape as `LoopbackRecorder._run()`.
    """

    def __init__(
        self,
        chunk_seconds: int,
        out_dir: Path,
        chunk_queue: "queue.Queue",
        pid: int,
        on_error=None,
    ):
        self.chunk_seconds = chunk_seconds
        self.out_dir = out_dir
        self.chunk_queue = chunk_queue
        self.pid = pid
        self.on_error = on_error
        self._raw_q: "queue.SimpleQueue" = queue.SimpleQueue()
        self._first_data = threading.Event()
        self._stop = threading.Event()
        self._tap: "ProcessAudioCapture | None" = None
        self._accum_thread: "threading.Thread | None" = None

    def _on_data(self, pcm: bytes, frames: int):
        self._first_data.set()
        self._raw_q.put(pcm)

    def probe(self, timeout: float) -> bool:
        """Starts the real tap + accumulator, blocks up to `timeout`s for the
        first callback. False (or an exception raised out of here) means the
        caller must `.stop()` this and fall back."""
        self._tap = ProcessAudioCapture(pid=self.pid, on_data=self._on_data, resample_quality="best")
        self._tap.start()
        self._accum_thread = threading.Thread(target=self._accumulate, daemon=True)
        self._accum_thread.start()
        return self._first_data.wait(timeout)

    def start(self):
        if self._tap is None:
            self.probe(timeout=0)

    def stop(self):
        self._stop.set()
        try:
            if self._tap is not None:
                self._tap.close()
        except Exception:
            pass
        if self._accum_thread:
            self._accum_thread.join(timeout=self.chunk_seconds + 10)

    def _write_chunk(self, raw_bytes: bytes, chunk_start_wall: float):
        float_samples = np.frombuffer(raw_bytes, dtype=np.float32)
        clipped = np.clip(float_samples, -1.0, 1.0)
        int16_samples = (clipped * 32767.0).astype(np.int16)

        chunk_path = self.out_dir / f"chunk_{int(chunk_start_wall)}.wav"
        with wave.open(str(chunk_path), "wb") as wf:
            wf.setnchannels(PROC_TAP_CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(PROC_TAP_SAMPLE_RATE)
            wf.writeframes(int16_samples.tobytes())

        self.chunk_queue.put((chunk_path, chunk_start_wall))

    def _accumulate(self):
        bytes_needed = int(PROC_TAP_SAMPLE_RATE * self.chunk_seconds) * PROC_TAP_BYTES_PER_FRAME
        buf = bytearray()
        chunk_start_wall = time.time()
        last_liveness_check = time.time()

        while not self._stop.is_set():
            try:
                pcm = self._raw_q.get(timeout=0.5)
            except queue.Empty:
                pcm = None

            now = time.time()
            if now - last_liveness_check >= LIVENESS_CHECK_INTERVAL:
                last_liveness_check = now
                if not psutil.pid_exists(self.pid):
                    if self.on_error:
                        self.on_error(
                            "The program being captured has closed - stopped recording its audio.",
                            True,
                        )
                    return

            if pcm is None:
                continue

            buf.extend(pcm)
            if len(buf) >= bytes_needed:
                chunk_bytes = bytes(buf[:bytes_needed])
                del buf[:bytes_needed]
                self._write_chunk(chunk_bytes, chunk_start_wall)
                chunk_start_wall = time.time()

        # mirrors LoopbackRecorder._run()'s behavior of flushing a final
        # partial chunk on a clean stop rather than discarding it
        if buf:
            self._write_chunk(bytes(buf), chunk_start_wall)


def make_recorder(
    chunk_seconds: int,
    out_dir: Path,
    chunk_queue: "queue.Queue",
    target_pid: "int | None" = None,
    on_error=None,
    probe_timeout: float = 2.5,
):
    """Single construction path for both recorder types.

    Returns (recorder, mode, reason): mode is "process" or "full"; reason is
    None on success, else a short human-readable string explaining why
    per-process isolation wasn't used, for the caller to show the user
    before silently falling back to full-device capture.
    """
    if target_pid is None:
        return LoopbackRecorder(chunk_seconds, out_dir, chunk_queue, on_error=on_error), "full", None

    if not PROC_TAP_AVAILABLE:
        return (
            LoopbackRecorder(chunk_seconds, out_dir, chunk_queue, on_error=on_error),
            "full",
            "per-process capture unavailable on this build",
        )

    candidate = ProcessLoopbackRecorder(chunk_seconds, out_dir, chunk_queue, target_pid, on_error=on_error)
    try:
        if candidate.probe(probe_timeout):
            return candidate, "process", None
        reason = "no audio activity detected from that program"
    except Exception as exc:
        reason = str(exc)

    try:
        candidate.stop()
    except Exception:
        pass
    return LoopbackRecorder(chunk_seconds, out_dir, chunk_queue, on_error=on_error), "full", reason
