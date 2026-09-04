"""WASAPI loopback audio capture, chunked to rolling WAV files.

Captures the full default output device mix, not a single process's audio.
True per-process loopback (AUDIOCLIENT_ACTIVATION_PARAMS / PROCESS_LOOPBACK)
has no mature Python wrapper - it requires implementing a COM async
activation callback by hand via ctypes, which no library here does. See
README for the practical implication (mute other audio sources first).
"""
import ctypes
import os
import queue
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

import psutil
import pyaudiowpatch as pyaudio

_user32 = ctypes.windll.user32
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_GW_OWNER = 4

# shell/system windows that technically pass the visible-top-level-window
# check but aren't apps a user would pick as a call target
_EXCLUDED_TITLES = {"Program Manager", "Windows Input Experience"}
_EXCLUDED_PROCESSES = {"searchhost.exe", "shellexperiencehost.exe", "textinputhost.exe",
                        "startmenuexperiencehost.exe"}


def list_open_windows() -> list[tuple[str, str]]:
    """Enumerates visible top-level windows with a taskbar presence.

    Returns a sorted list of (window_title, process_name) pairs, deduped by
    process name (keeps whichever window title was seen first per process).
    Excludes this app's own process and common shell/system windows.
    """
    results: dict[str, str] = {}
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

        results.setdefault(pname, title)
        return True

    _user32.EnumWindows(callback, 0)
    return sorted(
        ((title, pname) for pname, title in results.items()),
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
