"""AutoScript - local voice-to-text dictation.

Captures either a selected program's audio or your PC's full output and
transcribes it live to a markdown file. Works with a Discord/Zoom/Teams call,
a video, or any other desktop audio source.

Layout inspired by chat-app / call-transcript tools: a left sidebar with a
wordmark and history of past sessions, and a main panel that's either the
live/new-recording view or a read-only view of a selected past transcript,
with an in-transcript search box.

Wraps capture.py / transcriber.py. Run directly with python, or build to an
.exe with PyInstaller (see build_exe.ps1).
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image
from tkinter import filedialog, messagebox

from capture import (
    LoopbackRecorder,
    PROC_TAP_AVAILABLE,
    ProcessLoopbackRecorder,
    list_open_windows,
    make_recorder,
    process_running,
)
from plugin_manager import PluginManager
from transcript_security import EncryptedLog, TokenVault, decrypt as decrypt_transcript, new_token, transcript_id

# transcriber.py pulls in faster-whisper -> ctranslate2/onnxruntime/av, which
# together take ~2s+ to import in a frozen build (vs ~0.2s from source) -
# imported lazily in _start_backend() instead of here so the window can
# appear immediately rather than paying that cost before anything is shown.
if TYPE_CHECKING:
    from transcriber import TranscriptionWorker, load_model

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
    RESOURCES = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).parent
    RESOURCES = ROOT

# ``ROOT`` becomes the user-selected data directory after first-run setup.
# Keep the install folder separate: downloadable plugins always live beside
# AutoScript.exe, regardless of where a user stores their transcripts.
APP_DIR = ROOT

CHUNKS_DIR = ROOT / "chunks"
TRANSCRIPTS_DIR = ROOT / "transcripts"
ICON_PATH = RESOURCES / "icon.ico"
LOGO_PATH = RESOURCES / "autoscript_logo.png"
PINNED_FILE = TRANSCRIPTS_DIR / ".pinned.json"
NAMES_FILE = TRANSCRIPTS_DIR / ".names.json"
APP_VERSION = "4.0.2"

# Small per-user config (just "where's the data") that lives in a fixed OS
# location regardless of where the user picks to store everything else -
# otherwise there'd be nowhere reliable to remember that choice from.
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "AutoScript"
LEGACY_CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "CDCT"
CONFIG_FILE = CONFIG_DIR / "config.json"

FONT_FAMILY = "Segoe UI Variable"


def app_font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


# --- modern neutral palette: layered dark surfaces and one calm accent ---
BG_MAIN = "#101010"
BG_SIDEBAR = "#171717"
BG_SIDEBAR_HOVER = "#232323"
BG_SIDEBAR_ACTIVE = "#2C2C2C"
BG_TEXTBOX = "#191919"
BG_CONTROL = "#232323"
BG_CONTROL_HOVER = "#303030"
BG_RAISED = "#2A2A2A"
BG_RAISED_HOVER = "#363636"
FG_TEXT = "#F5F5F5"
FG_MUTED = "#A1A1AA"
FG_FAINT = "#71717A"
FG_TIMESTAMP = "#8EA4C8"
ACCENT = "#0A7CFF"
ACCENT_HOVER = "#0069DC"


class SegmentedScrollbar(ctk.CTkScrollbar):
    """A normal CTk scrollbar whose thumb is rendered as a stack of short dashes."""

    def _draw(self, no_color_updates=False):
        # Call CTkBaseClass directly: CTkScrollbar's normal draw routine makes
        # one solid thumb, while its existing input handlers still work with
        # the border_parts and scrollbar_parts tags created below.
        super(ctk.CTkScrollbar, self)._draw(no_color_updates)

        corrected_start, corrected_end = self._get_scrollbar_values_for_minimum_pixel_size()
        width = max(int(self._apply_widget_scaling(self._current_width)), 1)
        height = max(int(self._apply_widget_scaling(self._current_height)), 1)
        border = max(int(self._apply_widget_scaling(self._border_spacing)), 1)
        track_color = self._apply_appearance_mode(
            self._bg_color if self._fg_color == "transparent" else self._fg_color
        )
        dash_color = self._apply_appearance_mode(
            self._button_hover_color if self._hover_state else self._button_color
        )

        self._canvas.delete("segmented_track")
        self._canvas.delete("segmented_parts")
        self._canvas.create_rectangle(
            0, 0, width, height, fill=track_color, outline=track_color,
            tags=("border_parts", "segmented_track"),
        )

        usable_height = max(height - (border * 2), 1)
        top = border + (corrected_start * usable_height)
        bottom = border + (corrected_end * usable_height)
        thumb_height = max(bottom - top, 1)
        dash_height = 3
        dash_gap = 5
        dash_count = max(1, int((thumb_height + dash_gap) // (dash_height + dash_gap)))
        if dash_count == 1:
            dash_positions = [top + max((thumb_height - dash_height) / 2, 0)]
        else:
            step = max((thumb_height - dash_height) / (dash_count - 1), dash_height)
            dash_positions = [top + (step * index) for index in range(dash_count)]

        for y in dash_positions:
            self._canvas.create_rectangle(
                2, y, max(width - 2, 3), min(y + dash_height, bottom),
                fill=dash_color, outline=dash_color,
                tags=("scrollbar_parts", "segmented_parts"),
            )
        self._canvas.update_idletasks()


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

if PROC_TAP_AVAILABLE:
    WELCOME_TEXT = (
        "Pick a model and hit Start to transcribe.\n\n"
        "Pick a program in \"Capturing for\" to capture just that program's "
        "audio, when possible - otherwise AutoScript captures your full speaker "
        "output.\n\n"
        "Not sure what a setting does? Hover over it to find out."
    )
else:
    WELCOME_TEXT = (
        "Pick a model and hit Start to transcribe.\n\n"
        "This captures your PC's full speaker output, not just the program "
        "picked in \"Capturing for\" - close or mute anything else making "
        "sound first.\n\n"
        "Not sure what a setting does? Hover over it to find out."
    )

TS_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _history_title(path: Path) -> str:
    m = re.match(r"transcript_(\d{4}-\d{2}-\d{2})_(\d+)\.md", path.name)
    if m:
        dt = datetime.fromtimestamp(int(m.group(2)))
    else:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
    day = dt.strftime("%b %d").replace(" 0", " ")
    tm = dt.strftime("%I:%M %p").lstrip("0")
    return f"{day}, {tm}"


def _safe_filename_stem(label: str) -> str:
    """Make the selected capture target safe and readable as a Windows name."""
    cleaned = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]+', "_", label).strip(" ._")
    return cleaned[:80] or "SystemAudio"


def _clean_line(line: str) -> str:
    return line.strip().strip("*").strip()


def _round_window_corners(win):
    """Windows 11 DWM native rounded corners - works on borderless/overrideredirect windows."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
        )
    except Exception:
        pass


def _mark_as_tool_window(win):
    """Keep AutoScript's transient panels out of taskbar/Alt-Tab/window enumeration."""
    try:
        win.attributes("-toolwindow", True)
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW
        )
    except Exception:
        pass


class Tooltip:
    """Small dark hover tooltip for a widget, matching the app's palette."""

    def __init__(self, widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.attributes("-topmost", True)
        _mark_as_tool_window(self._tip)
        tk.Label(
            self._tip,
            text=self.text,
            justify="left",
            bg=BG_CONTROL,
            fg=FG_TEXT,
            padx=12,
            pady=8,
            font=(FONT_FAMILY, 12),
            wraplength=280,
            bd=1,
            relief="solid",
            highlightbackground=BG_SIDEBAR_ACTIVE,
        ).pack()

    def _hide(self, event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class TranscriberApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoScript - Idle")
        self.geometry("1120x720")
        self.minsize(820, 520)
        self.configure(fg_color=BG_MAIN)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        self.recorder: LoopbackRecorder | ProcessLoopbackRecorder | None = None
        self.worker: TranscriptionWorker | None = None
        self.transcript_path: Path | None = None
        self.line_queue: "queue.Queue[str]" = queue.Queue()
        # Workers never touch Tk directly.  Tkinter may deadlock when a
        # background thread calls widget methods (including ``after``).
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.running = False
        self.starting = False
        self._start_cancelled = threading.Event()
        self.mode = "new"  # "new" or "history"
        self.selected_history_path: Path | None = None
        self.history_buttons: dict[Path, ctk.CTkButton] = {}
        self._last_history_scan = 0.0
        self.current_raw_text = ""
        self.token_vault = TokenVault(CONFIG_DIR / "transcript-tokens.json")
        self._active_encrypted_log: EncryptedLog | None = None
        self.pinned: set[str] = self._load_pinned()
        self.names: dict[str, str] = self._load_names()

        # settings state - lives here so it persists whether or not the
        # settings popover happens to be open
        self.model_var = ctk.StringVar(value="Small")
        self.chunk_var = ctk.StringVar(value="10")
        self.keep_audio_var = ctk.BooleanVar(value=False)
        self.include_microphone_var = ctk.BooleanVar(value=False)
        self.recording_mode_var = ctk.StringVar(value="Call / system audio")
        self.recording_mode_toggle: ctk.CTkSwitch | None = None
        self.target_process_name: str | None = "Discord.exe"
        self.target_display_name = "Discord"
        self.target_pid: int | None = None
        self.target_menu: ctk.CTkButton | None = None
        self.target_dropdown: ctk.CTkToplevel | None = None
        self._target_options: dict[str, tuple[str, str, int]] = {}
        self.plugin_menu: ctk.CTkButton | None = None
        self.plugin_dropdown: ctk.CTkToplevel | None = None
        self._capture_fallback_popup: ctk.CTkToplevel | None = None
        self._disclosure_popup: ctk.CTkToplevel | None = None
        self._plugin_toggle_vars: dict[str, ctk.BooleanVar] = {}
        self._plugin_status_labels: dict[str, ctk.CTkLabel] = {}
        self.plugin_manager = PluginManager(
            APP_DIR / "plugins",
            CONFIG_DIR / "plugins.json",
            APP_VERSION,
            TRANSCRIPTS_DIR,
            copy_token=lambda token: self._queue_ui(lambda: self._copy_new_transcript_token(token)),
        )
        self.plugin_manager.discover()
        self.active_capture_mode: str = "full"
        self.active_self_transcription = False
        self.settings_popup = None

        self._build_sidebar()
        self._build_main()
        self.show_new_recording()
        self._refresh_history()
        self._poll_queue()
        self.plugin_manager.activate_enabled()
        self.after(150, self._open_file_from_windows)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Borderless settings/dropdown windows are independent native topmost
        # windows. Dismiss them when AutoScript is minimized so none remain floating
        # above the desktop or prevent the main window from being restored.
        self.bind("<Unmap>", self._on_main_unmap, add="+")

    def _open_file_from_windows(self) -> None:
        """Open a file passed by a Windows file association, if present."""
        candidates = [Path(value) for value in sys.argv[1:] if value.lower().endswith(".asenc")]
        if not candidates:
            return
        path = candidates[0]
        if not path.is_file():
            messagebox.showerror("Encrypted transcript not found", f"AutoScript could not find:\n{path}", parent=self)
            return
        self.show_history_item(path)

    # ---------- UI construction ----------

    def _on_main_unmap(self, _event=None):
        """Close transient UI after Windows finishes minimizing the main app."""
        self.after_idle(self._close_transients_if_minimized)

    def _close_transients_if_minimized(self):
        if self.state() == "iconic":
            self._close_settings_panel()

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=270, fg_color=BG_SIDEBAR, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(28, 22))
        if LOGO_PATH.exists():
            pil_logo = Image.open(LOGO_PATH)
            w, h = pil_logo.size
            display_w = 220
            display_h = round(display_w * h / w)
            logo_image = ctk.CTkImage(
                light_image=pil_logo, dark_image=pil_logo, size=(display_w, display_h)
            )
            ctk.CTkLabel(logo, image=logo_image, text="").pack(anchor="w")
        else:
            ctk.CTkLabel(
                logo, text="DCT", font=app_font(30, "bold"), text_color=FG_TEXT
            ).pack(anchor="w")
            ctk.CTkLabel(
                logo,
                text="Local Voice to Text",
                font=app_font(11),
                text_color=FG_FAINT,
            ).pack(anchor="w")

        ctk.CTkButton(
            sidebar,
            text="+  New Recording",
            command=self.show_new_recording,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="white",
            corner_radius=10,
            height=42,
            anchor="w",
            font=app_font(13, "bold"),
        ).pack(fill="x", padx=14, pady=(0, 18))

        ctk.CTkLabel(
            sidebar,
            text="RECENT CALLS",
            text_color=FG_FAINT,
            font=app_font(11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 6))

        footer = ctk.CTkFrame(sidebar, fg_color="transparent", height=64)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.gear_btn = ctk.CTkButton(
            footer,
            text="⚙",
            width=36,
            height=36,
            corner_radius=18,
            fg_color=BG_SIDEBAR_HOVER,
            hover_color=BG_SIDEBAR_ACTIVE,
            text_color=FG_TEXT,
            font=app_font(16),
            command=self._open_settings_panel,
        )
        self.gear_btn.pack(side="left", padx=(14, 8), pady=12)
        Tooltip(self.gear_btn, "Settings: model, chunk length, capture target, microphone, and kept audio.")

        mode_toggle = ctk.CTkFrame(footer, fg_color="transparent")
        mode_toggle.pack(side="right", padx=14, pady=12)
        ctk.CTkLabel(
            mode_toggle, text="Device", text_color=FG_MUTED, font=app_font(10)
        ).pack(side="left", padx=(0, 5))
        self.recording_mode_toggle = ctk.CTkSwitch(
            mode_toggle,
            text="",
            variable=self.recording_mode_var,
            onvalue="Self transcription",
            offvalue="Call / system audio",
            command=lambda: self._set_recording_mode(self.recording_mode_var.get()),
            width=38,
            switch_width=34,
            switch_height=18,
            fg_color=BG_SIDEBAR_HOVER,
            progress_color=ACCENT,
            button_color=FG_TEXT,
            button_hover_color=FG_MUTED,
        )
        self.recording_mode_toggle.pack(side="left")
        ctk.CTkLabel(
            mode_toggle, text="Notes", text_color=FG_MUTED, font=app_font(10)
        ).pack(side="left", padx=(5, 0))
        Tooltip(self.recording_mode_toggle, "Off: device capture. On: note taking from your microphone only.")

        self.history_scroll = ctk.CTkScrollableFrame(
            sidebar, fg_color="transparent", scrollbar_button_color=BG_SIDEBAR_HOVER
        )
        self.history_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        main.pack(side="left", fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=32, pady=(30, 8))
        self.title_var = ctk.StringVar(value="New Recording")
        title_label = ctk.CTkLabel(
            header,
            textvariable=self.title_var,
            font=app_font(24, "bold"),
            text_color=FG_TEXT,
            cursor="hand2",
        )
        title_label.pack(side="left")
        title_label.bind("<Button-1>", self._on_title_click)
        Tooltip(title_label, "Click to rename this session.")
        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            header, textvariable=self.status_var, text_color=FG_MUTED, font=app_font(13)
        ).pack(side="right")

        search_row = ctk.CTkFrame(main, fg_color="transparent")
        search_row.pack(fill="x", padx=32, pady=(4, 14))
        # NOTE: customtkinter silently ignores placeholder_text when a
        # textvariable is bound, so this drives filtering off the entry's
        # own .get() via <KeyRelease> instead of a StringVar + trace.
        self.search_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Search Transcript",
            fg_color=BG_RAISED,
            border_color=BG_RAISED_HOVER,
            text_color=FG_TEXT,
            height=38,
            corner_radius=10,
            font=app_font(14),
        )
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self._apply_filter())
        Tooltip(self.search_entry, "Type to find lines in this transcript.")

        self.controls = ctk.CTkFrame(main, fg_color="transparent")
        self.controls.pack(fill="x", padx=32, pady=(0, 16))

        self.start_btn = ctk.CTkButton(
            self.controls,
            text="●  Start Recording",
            command=self.start,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            width=160,
            height=38,
            corner_radius=10,
            font=app_font(13, "bold"),
        )
        self.start_btn.pack(side="left", padx=(0, 6))
        Tooltip(self.start_btn, "Start recording and transcribing.")
        self.stop_btn = ctk.CTkButton(
            self.controls,
            text="Stop",
            command=self.stop,
            fg_color=BG_CONTROL,
            hover_color=BG_CONTROL_HOVER,
            width=96,
            height=38,
            corner_radius=10,
            state="disabled",
            font=app_font(13),
        )
        self.stop_btn.pack(side="left")
        Tooltip(self.stop_btn, "Stop recording.")

        # Shown only while AutoScript is preparing, starting, or cancelling a
        # recording.  The underlying model library provides no reliable byte
        # progress callback, so this deliberately uses an honest animated
        # activity indicator rather than a made-up percentage.
        self.busy_row = ctk.CTkFrame(main, fg_color=BG_CONTROL, corner_radius=12)
        self.busy_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            self.busy_row, textvariable=self.busy_var, text_color=FG_MUTED,
            font=app_font(12), anchor="w",
        ).pack(side="left", padx=(12, 10), pady=8)
        self.busy_bar = ctk.CTkProgressBar(
            self.busy_row, mode="indeterminate", width=150,
            progress_color=ACCENT, fg_color=BG_TEXTBOX,
        )
        self.busy_bar.pack(side="right", padx=12, pady=10)

        self.unlock_row = ctk.CTkFrame(main, fg_color=BG_CONTROL, corner_radius=12)
        ctk.CTkLabel(self.unlock_row, text="Encrypted transcript token", font=app_font(12)).pack(side="left", padx=(12, 8), pady=8)
        self.unlock_entry = ctk.CTkEntry(self.unlock_row, show="*", placeholder_text="Paste token", width=300)
        self.unlock_entry.pack(side="left", fill="x", expand=True, pady=8)
        self.unlock_entry.bind("<Return>", lambda _event: self._unlock_selected_history())
        ctk.CTkButton(self.unlock_row, text="Unlock", width=90, command=self._unlock_selected_history).pack(side="left", padx=8, pady=8)

        self.textbox = ctk.CTkTextbox(
            main,
            fg_color=BG_MAIN,
            text_color=FG_TEXT,
            corner_radius=0,
            border_width=0,
            font=app_font(14),
            wrap="word",
        )
        self.textbox.pack(fill="both", expand=True, padx=32, pady=(0, 18))
        self._install_segmented_transcript_scrollbar()
        self.textbox.configure(state="disabled")
        self.textbox._textbox.tag_config("ts", foreground=FG_TIMESTAMP)
        self.textbox._textbox.tag_config("system", foreground=FG_FAINT)
        self.textbox._textbox.tag_config("body", foreground=FG_TEXT)

        bottom = ctk.CTkFrame(main, fg_color="transparent")
        bottom.pack(fill="x", padx=32, pady=(0, 20))
        self.file_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            bottom, textvariable=self.file_var, text_color=FG_FAINT, font=app_font(11)
        ).pack(side="left")
        ctk.CTkButton(
            bottom,
            text="Open Folder",
            command=self.open_folder,
            fg_color=BG_RAISED,
            hover_color=BG_RAISED_HOVER,
            width=110,
            height=34,
            corner_radius=9,
            font=app_font(12),
        ).pack(side="right")
        ctk.CTkLabel(
            bottom,
            text=f"Release v{APP_VERSION}",
            text_color=FG_FAINT,
            font=app_font(10),
        ).pack(side="right", padx=(0, 12))

    # ---------- history sidebar ----------

    def _load_pinned(self) -> set:
        try:
            return set(json.loads(PINNED_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    def _save_pinned(self):
        try:
            TRANSCRIPTS_DIR.mkdir(exist_ok=True)
            PINNED_FILE.write_text(json.dumps(sorted(self.pinned)), encoding="utf-8")
        except OSError:
            pass

    def _load_names(self) -> dict:
        try:
            return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_names(self):
        try:
            TRANSCRIPTS_DIR.mkdir(exist_ok=True)
            NAMES_FILE.write_text(json.dumps(self.names), encoding="utf-8")
        except OSError:
            pass

    def _display_title(self, path: Path) -> str:
        return self.names.get(path.name) or _history_title(path)

    def _sorted_history_paths(self):
        # Base AutoScript sessions and optional plugins share this folder. Plugins
        # may use their own meaningful filename convention. Encrypted Discord
        # sessions stay beside Markdown sessions and unlock only when the
        # matching plugin token is available locally.
        paths = [*TRANSCRIPTS_DIR.glob("*.md"), *TRANSCRIPTS_DIR.glob("*.asenc")]
        return sorted(
            paths,
            key=lambda p: (p.name not in self.pinned, -p.stat().st_mtime),
        )

    def _refresh_history(self, force: bool = False):
        paths = self._sorted_history_paths()
        order_changed = [p for p in self.history_buttons] != paths
        if force or set(paths) != set(self.history_buttons.keys()) or order_changed:
            for btn in self.history_buttons.values():
                # The row also contains the gear button.  Destroying only the
                # transcript button leaves an empty recent-call entry behind.
                btn.master.destroy()
            self.history_buttons = {}
            for p in paths:
                row = ctk.CTkFrame(self.history_scroll, fg_color="transparent")
                row.pack(fill="x", pady=2)
                btn = ctk.CTkButton(
                    row,
                    text=self._history_label(p),
                    command=lambda p=p: self._on_history_click(p),
                    anchor="w",
                    fg_color="transparent",
                    hover_color=BG_SIDEBAR_HOVER,
                    text_color=FG_TEXT,
                    font=app_font(12),
                    corner_radius=6,
                    height=32,
                )
                btn.pack(side="left", fill="x", expand=True)
                gear = ctk.CTkButton(
                    row,
                    text="⚙",
                    width=30,
                    height=30,
                    fg_color="transparent",
                    hover_color=BG_SIDEBAR_HOVER,
                    text_color=FG_FAINT,
                    font=app_font(13),
                    command=lambda p=p, row=row: self._show_context_menu_at(
                        p, row.winfo_rootx() + row.winfo_width() - 28, row.winfo_rooty() + 30
                    ),
                )
                gear.pack(side="right", padx=(3, 0))
                self.history_buttons[p] = btn
        else:
            for p, btn in self.history_buttons.items():
                btn.configure(text=self._history_label(p))
        self._update_history_highlight()

    def _history_label(self, path: Path) -> str:
        if self.running and path == self.transcript_path:
            prefix = "●  "
        elif path.suffix.lower() == ".asenc":
            prefix = "🔒 "
        elif path.name in self.pinned:
            prefix = "📌 "
        else:
            prefix = "    "
        return f"{prefix}{self._display_title(path)}"

    def _show_context_menu(self, event, path: Path):
        self._show_context_menu_at(path, event.x_root, event.y_root)

    def _show_context_menu_at(self, path: Path, x: int, y: int):
        menu = tk.Menu(
            self,
            tearoff=False,
            bg=BG_CONTROL,
            fg=FG_TEXT,
            activebackground=BG_SIDEBAR_ACTIVE,
            activeforeground=FG_TEXT,
            bd=0,
        )
        menu.add_command(label="Rename", command=lambda: self._rename_transcript(path))
        if path.suffix.lower() == ".asenc" and self.token_vault.get(transcript_id(path)):
            menu.add_command(label="Copy Token", command=lambda: self._copy_transcript_token(path))
        is_pinned = path.name in self.pinned
        menu.add_command(
            label="Unpin" if is_pinned else "Pin to top",
            command=lambda: self._toggle_pin(path),
        )
        is_live = self.running and path == self.transcript_path
        menu.add_command(
            label="Delete",
            command=lambda: self._delete_transcript(path),
            state="disabled" if is_live else "normal",
        )
        menu.tk_popup(x, y)

    def _toggle_pin(self, path: Path):
        if path.name in self.pinned:
            self.pinned.discard(path.name)
        else:
            self.pinned.add(path.name)
        self._save_pinned()
        self._refresh_history(force=True)

    def _copy_transcript_token(self, path: Path):
        token = self.token_vault.get(transcript_id(path))
        if not token:
            messagebox.showerror("Token unavailable", "This Windows account does not hold the token for this transcript.", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(token)
        self.update()
        self.status_var.set("Transcript token copied to clipboard")

    def _delete_transcript(self, path: Path):
        if self.running and path == self.transcript_path:
            return
        if not messagebox.askyesno("Delete transcript", f"Delete {self._display_title(path)}? This can't be undone."):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror("Delete failed", str(exc))
            return
        self.pinned.discard(path.name)
        self._save_pinned()
        if path.name in self.names:
            del self.names[path.name]
            self._save_names()
        was_current = self.transcript_path == path and not self.running
        if was_current:
            self.transcript_path = None
        if self.selected_history_path == path or was_current:
            self.show_new_recording()
        self._refresh_history(force=True)

    def _on_title_click(self, event=None):
        if self.mode == "history" and self.selected_history_path:
            self._rename_transcript(self.selected_history_path)
        elif self.mode == "new" and self.transcript_path:
            self._rename_transcript(self.transcript_path)

    def _rename_transcript(self, path: Path):
        popup = ctk.CTkToplevel(self)
        popup.overrideredirect(True)
        popup.configure(fg_color=BG_CONTROL)
        popup.attributes("-topmost", True)
        _mark_as_tool_window(popup)
        popup.grab_set()

        ctk.CTkLabel(
            popup,
            text="Rename session",
            text_color=FG_TEXT,
            font=app_font(13, "bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

        name_var = ctk.StringVar(value=self._display_title(path))
        entry = ctk.CTkEntry(popup, textvariable=name_var, width=260, font=app_font(13))
        entry.pack(padx=16, pady=(0, 14))
        entry.focus_set()
        entry.select_range(0, "end")

        def save(event=None):
            new_name = name_var.get().strip()
            if new_name and new_name != _history_title(path):
                self.names[path.name] = new_name
            else:
                self.names.pop(path.name, None)
            self._save_names()
            if self.mode == "new" and self.transcript_path == path:
                self.title_var.set(self._display_title(path))
            elif self.mode == "history" and self.selected_history_path == path:
                self.title_var.set(self._display_title(path))
            self._refresh_history(force=True)
            popup.destroy()

        def cancel(event=None):
            popup.destroy()

        entry.bind("<Return>", save)
        entry.bind("<Escape>", cancel)

        btn_row = ctk.CTkFrame(popup, fg_color="transparent")
        btn_row.pack(padx=16, pady=(0, 14))
        ctk.CTkButton(
            btn_row, text="Cancel", command=cancel, fg_color=BG_SIDEBAR,
            hover_color=BG_SIDEBAR_HOVER, width=90, font=app_font(12),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Save", command=save, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            width=90, font=app_font(12),
        ).pack(side="left")

        # measure post-render (logical vs physical size can differ under DPI
        # scaling), then center within the main window using that true size
        popup.update()
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        popup.geometry(f"{w}x{h}+0+0")
        popup.update()
        actual_w = popup.winfo_width()
        actual_h = popup.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - actual_w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - actual_h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")
        _round_window_corners(popup)

    def _update_history_highlight(self):
        for p, btn in self.history_buttons.items():
            active = (self.mode == "history" and p == self.selected_history_path) or (
                self.mode == "new" and self.running and p == self.transcript_path
            )
            btn.configure(fg_color=BG_SIDEBAR_ACTIVE if active else "transparent")

    def _on_history_click(self, path: Path):
        if self.running and path == self.transcript_path:
            self.show_new_recording()
        else:
            self.show_history_item(path)

    # ---------- view switching ----------

    def show_new_recording(self):
        self.mode = "new"
        self.selected_history_path = None
        self.controls.pack(fill="x", padx=28, pady=(0, 14))
        self.search_entry.delete(0, "end")
        if self.running and self.transcript_path:
            self.title_var.set("Recording...")
            self._load_live_transcript()
        else:
            title = self._display_title(self.transcript_path) if self.transcript_path else "New Recording"
            self.title_var.set(title)
            self.status_var.set("Idle")
            if self.transcript_path:
                self._load_live_transcript()
            else:
                self._set_textbox_content(WELCOME_TEXT)
        self.file_var.set(str(self.transcript_path) if self.transcript_path else "")
        self._update_history_highlight()

    def show_history_item(self, path: Path):
        self.mode = "history"
        self.selected_history_path = path
        self.controls.pack_forget()
        self.search_entry.delete(0, "end")
        self.title_var.set(("🔒 " if path.suffix.lower() == ".asenc" else "") + self._display_title(path))
        self.status_var.set("")
        self.file_var.set(str(path))
        if path.suffix.lower() == ".asenc":
            try:
                token = self.token_vault.get(transcript_id(path))
                if not token:
                    self._show_unlock_field()
                    content = "🔒 This transcript is encrypted. Enter its unique sharing token above to unlock it."
                    self.status_var.set("Locked — unique transcript token required")
                    self._set_textbox_content(content)
                    self._update_history_highlight()
                    return
                content = decrypt_transcript(path, token)
                self.status_var.set("Encrypted transcript — decrypted in memory")
                self.unlock_row.pack_forget()
            except PermissionError as exc:
                content = f"🔒 This transcript is encrypted.\n\n{exc}"
                self.status_var.set("Locked — matching bot token required")
            except Exception:
                content = "🔒 This transcript could not be decrypted with that token."
                self.status_var.set("Locked — token mismatch or damaged file")
        else:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = "(could not read this transcript file)"
        self._set_textbox_content(content)
        self._update_history_highlight()

    def _show_unlock_field(self):
        self.unlock_entry.delete(0, "end")
        self.unlock_row.pack(fill="x", padx=32, pady=(0, 12), before=self.textbox)
        self.unlock_entry.focus_set()

    def _set_busy(self, message: str) -> None:
        """Show an animated, stage-specific indicator on the GUI thread."""
        self.busy_var.set(message)
        self.status_var.set(message)
        if not self.busy_row.winfo_manager():
            self.busy_row.pack(fill="x", padx=32, pady=(0, 12), before=self.textbox)
            self.busy_bar.start()

    def _clear_busy(self) -> None:
        self.busy_bar.stop()
        self.busy_row.pack_forget()

    def _unlock_selected_history(self):
        path = self.selected_history_path
        token = self.unlock_entry.get().strip()
        if not path or path.suffix.lower() != ".asenc" or not token:
            return
        try:
            content = decrypt_transcript(path, token)
        except Exception:
            self.status_var.set("Locked — token did not match")
            return
        record_id = transcript_id(path)
        if record_id:
            self.token_vault.save(record_id, token)
        self.unlock_row.pack_forget()
        self.status_var.set("Encrypted transcript — decrypted in memory")
        self._set_textbox_content(content)

    def _load_live_transcript(self):
        if self.transcript_path and self.transcript_path.exists():
            if self.transcript_path.suffix.lower() == ".asenc" and self._active_encrypted_log:
                self._set_textbox_content(self._active_encrypted_log.text)
                return
            try:
                content = self.transcript_path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            self._set_textbox_content(content)

    # ---------- transcript rendering ----------

    def _set_textbox_content(self, text: str):
        self.current_raw_text = text
        self._render(text, self.search_entry.get())

    def _apply_filter(self):
        self._render(self.current_raw_text, self.search_entry.get())

    def _render(self, text: str, query: str):
        query = query.strip().lower()
        tb = self.textbox
        tb.configure(state="normal")
        tb.delete("1.0", "end")

        if text is WELCOME_TEXT or text == WELCOME_TEXT:
            tb.insert("1.0", text)
            tb.configure(state="disabled")
            return

        any_match = False
        for raw in text.splitlines():
            line = _clean_line(raw)
            if not line or line.startswith("#"):
                continue
            if query and query not in line.lower():
                continue
            any_match = True
            m = TS_LINE_RE.match(line)
            if m and TIME_RE.match(m.group(1)):
                tb.insert("end", f"[{m.group(1)}]  ", "ts")
                tb.insert("end", f"{m.group(2)}\n\n", "body")
            elif m:
                tb.insert("end", f"{line}\n\n", "system")
            else:
                tb.insert("end", f"{line}\n\n", "body")

        if not any_match:
            placeholder = "No matching lines." if query else "(no speech captured yet)"
            tb.insert("1.0", placeholder, "system")

        tb.see("end")
        tb.configure(state="disabled")

    # ---------- recording control ----------

    def open_folder(self):
        TRANSCRIPTS_DIR.mkdir(exist_ok=True)
        os.startfile(TRANSCRIPTS_DIR)

    def _open_settings_panel(self):
        if self.settings_popup and self.settings_popup.winfo_exists():
            self._close_settings_panel()
            return

        popup = ctk.CTkToplevel(self)
        popup.overrideredirect(True)
        popup.configure(fg_color=BG_CONTROL)
        popup.attributes("-topmost", True)
        # The DPI-safe measurement below needs one initial map before it can
        # know the popup's physical footprint. Keep that pass invisible so
        # Settings appears only at its final aligned location.
        popup.attributes("-alpha", 0.0)
        _mark_as_tool_window(popup)
        _round_window_corners(popup)

        ctk.CTkLabel(
            popup, text="Settings", text_color=FG_TEXT, font=app_font(13, "bold")
        ).pack(anchor="w", padx=16, pady=(14, 10))

        def setting_row(label_text: str, tip_text: str) -> ctk.CTkFrame:
            r = ctk.CTkFrame(popup, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=(0, 12))
            lbl = ctk.CTkLabel(
                r, text=label_text, text_color=FG_MUTED, font=app_font(12), anchor="w"
            )
            lbl.pack(anchor="w")
            Tooltip(lbl, tip_text)
            return r

        model_row = setting_row(
            "Model", "How accurate the transcription is. Bigger = more accurate but slower. 'Small' is a good default."
        )
        ctk.CTkOptionMenu(
            model_row,
            variable=self.model_var,
            values=["Tiny", "Base", "Small", "Medium"],
            fg_color=BG_SIDEBAR,
            button_color=BG_SIDEBAR,
            button_hover_color=BG_SIDEBAR_HOVER,
            font=app_font(12),
            dropdown_font=app_font(12),
        ).pack(fill="x", pady=(4, 0))

        chunk_row = setting_row(
            "Chunk length (s)", "How often new text appears. Lower = updates faster but choppier. Higher = smoother but slower to show up."
        )
        ctk.CTkEntry(
            chunk_row, textvariable=self.chunk_var, fg_color=BG_SIDEBAR,
            border_color=BG_SIDEBAR_HOVER, font=app_font(12),
        ).pack(fill="x", pady=(4, 0))

        target_row = setting_row(
            "Capturing for",
            (
                "Only this program's audio will be captured and transcribed, when "
                "possible. If it can't be isolated, AutoScript will ask before falling "
                "back to capturing everything."
            ) if PROC_TAP_AVAILABLE else (
                "Which open program you're calling in. This is just a check that it's "
                "open before you hit Start - it still records your whole speaker output "
                "either way, not only that program."
            ),
        )
        self.target_menu = ctk.CTkButton(
            target_row,
            text=f"{self.target_display_name}  ▾",
            command=self._toggle_target_dropdown,
            fg_color=BG_SIDEBAR,
            hover_color=BG_SIDEBAR_HOVER,
            font=app_font(12),
            anchor="w",
            text_color=FG_TEXT,
            corner_radius=6,
        )
        self.target_menu.pack(fill="x", pady=(4, 0))
        self._set_recording_mode(self.recording_mode_var.get())

        plugins_row = setting_row(
            "Plugins",
            "Optional extensions installed beside AutoScript. Enable only plugins you trust; "
            "they run locally with the same permissions as AutoScript.",
        )
        self.plugin_menu = ctk.CTkButton(
            plugins_row,
            command=self._toggle_plugin_dropdown,
            fg_color=BG_SIDEBAR,
            hover_color=BG_SIDEBAR_HOVER,
            font=app_font(12),
            anchor="w",
            text_color=FG_TEXT,
            corner_radius=6,
        )
        self.plugin_menu.pack(fill="x", pady=(4, 0))
        self._refresh_plugin_menu()

        keep_row = ctk.CTkFrame(popup, fg_color="transparent")
        keep_row.pack(fill="x", padx=16, pady=(0, 16))
        keep_cb = ctk.CTkCheckBox(
            keep_row,
            text="Keep audio chunks",
            variable=self.keep_audio_var,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=app_font(12),
        )
        keep_cb.pack(anchor="w")
        Tooltip(keep_cb, "Saves the raw audio clips instead of deleting them after transcribing.")

        microphone_row = ctk.CTkFrame(popup, fg_color="transparent")
        microphone_row.pack(fill="x", padx=16, pady=(0, 16))
        microphone_cb = ctk.CTkCheckBox(
            microphone_row,
            text="Include microphone input (label as You)",
            variable=self.include_microphone_var,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=app_font(12),
        )
        microphone_cb.pack(anchor="w")
        Tooltip(microphone_cb, "Adds your default microphone as a separate source. Its transcribed lines are marked You.")

        # customtkinter's .geometry() takes a *logical* (DPI-unscaled) size but a
        # *physical* position, so on a scaled display reqwidth/reqheight don't
        # match the actual on-screen footprint. Render once at a rough position,
        # measure the true physical size, then clamp/reposition using that.
        popup.update()
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        gx = self.gear_btn.winfo_rootx()
        gy = self.gear_btn.winfo_rooty()
        popup.geometry(f"{w}x{h}+{gx}+{max(gy - h - 8, 0)}")
        popup.update()

        actual_w = popup.winfo_width()
        actual_h = popup.winfo_height()
        win_top = self.winfo_rooty()
        win_bottom = win_top + self.winfo_height()
        win_left = self.winfo_rootx()
        win_right = win_left + self.winfo_width()

        margin = 12
        x = min(gx, win_right - actual_w - margin)
        x = max(x, win_left + margin)
        y = gy - actual_h - 8
        y = max(y, win_top + margin)
        y = min(y, win_bottom - actual_h - margin)

        popup.geometry(f"{w}x{h}+{x}+{y}")
        _round_window_corners(popup)
        popup.attributes("-alpha", 1.0)

        self.settings_popup = popup
        self._refresh_target_options()
        self.bind_all("<Button-1>", self._maybe_close_settings, add="+")

    def _maybe_close_settings(self, event):
        popup = self.settings_popup
        if not popup or not popup.winfo_exists():
            return
        widgets = [popup, self.gear_btn]
        if self.target_dropdown and self.target_dropdown.winfo_exists():
            widgets.append(self.target_dropdown)
        if self.plugin_dropdown and self.plugin_dropdown.winfo_exists():
            widgets.append(self.plugin_dropdown)
        for widget in widgets:
            wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
            ww, wh = widget.winfo_width(), widget.winfo_height()
            if wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh:
                return
        self._close_settings_panel()

    def _close_settings_panel(self):
        self._close_target_dropdown()
        self._close_plugin_dropdown()
        if self.settings_popup and self.settings_popup.winfo_exists():
            self.settings_popup.destroy()
        self.settings_popup = None
        self.target_menu = None
        self.plugin_menu = None
        self.unbind_all("<Button-1>")

    def _truncate_target_label(self, text: str, max_width: int) -> str:
        """Return `text` shortened with an ellipsis to fit the menu's text area."""
        font = tkfont.Font(family=FONT_FAMILY, size=12)
        if font.measure(text) <= max_width:
            return text

        ellipsis = "..."
        while text and font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return text.rstrip() + ellipsis

    def _refresh_target_options(self):
        """Refresh the capture-target dropdown with visible applications."""
        if not self.target_menu or not self.target_menu.winfo_exists():
            return

        self.target_menu.update_idletasks()
        # Reserve room for CustomTkinter's dropdown-arrow button and padding.
        label_width = max(self.target_menu.winfo_width() - 54, 80)
        options: dict[str, tuple[str, str, int]] = {}
        for title, process_name, pid in list_open_windows():
            label = self._truncate_target_label(title, label_width)
            # A title collision is uncommon, but every choice still needs a
            # unique backing value for CTkOptionMenu's callback.
            if label in options:
                label = self._truncate_target_label(
                    f"{title} [{process_name}]", label_width
                )
            if label in options:
                label = self._truncate_target_label(f"{title} [{pid}]", label_width)
            options[label] = (title, process_name, pid)

        self._target_options = options
        selected_label = self._truncate_target_label(
            self.target_display_name, label_width
        )
        self.target_menu.configure(text=f"{selected_label}  ▾")

    def _toggle_target_dropdown(self):
        if self.target_dropdown and self.target_dropdown.winfo_exists():
            self._close_target_dropdown()
            return

        self._refresh_target_options()
        if not self.target_menu or not self.target_menu.winfo_exists():
            return

        dropdown = ctk.CTkToplevel(self)
        dropdown.overrideredirect(True)
        dropdown.configure(fg_color=BG_CONTROL)
        dropdown.attributes("-topmost", True)
        dropdown.transient(self.settings_popup or self)
        _mark_as_tool_window(dropdown)
        _round_window_corners(dropdown)

        content = ctk.CTkScrollableFrame(dropdown, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=6, pady=6)
        if not self._target_options:
            ctk.CTkLabel(
                content,
                text="No open programs found.",
                text_color=FG_FAINT,
                font=app_font(12),
            ).pack(padx=10, pady=10)
        else:
            for label, (title, process_name, pid) in self._target_options.items():
                ctk.CTkButton(
                    content,
                    text=label,
                    anchor="w",
                    command=lambda t=title, p=process_name, i=pid: self._pick_target(t, p, i),
                    fg_color="transparent",
                    hover_color=BG_SIDEBAR_HOVER,
                    text_color=FG_TEXT,
                    font=app_font(12),
                    corner_radius=5,
                    height=32,
                ).pack(fill="x", pady=1)

        dropdown.update_idletasks()
        logical_width = self.target_menu.winfo_reqwidth()
        logical_height = min(dropdown.winfo_reqheight(), 250)
        x = self.target_menu.winfo_rootx()
        y = self.target_menu.winfo_rooty() + self.target_menu.winfo_height() + 4
        dropdown.geometry(f"{logical_width}x{logical_height}+{x}+{y}")
        _round_window_corners(dropdown)
        self.target_dropdown = dropdown

    def _close_target_dropdown(self):
        if self.target_dropdown and self.target_dropdown.winfo_exists():
            self.target_dropdown.destroy()
        self.target_dropdown = None

    def _pick_target(self, title: str, process_name: str, pid: int):
        self.target_process_name = process_name
        self.target_pid = pid
        self.target_display_name = title
        if self.target_menu and self.target_menu.winfo_exists():
            self._refresh_target_options()
        self._close_target_dropdown()

    # ---------- optional plugins ----------

    def _refresh_plugin_menu(self):
        if not self.plugin_menu or not self.plugin_menu.winfo_exists():
            return
        plugins = list(self.plugin_manager.plugins.values())
        enabled = sum(plugin.enabled for plugin in plugins)
        if not plugins:
            label = "Plugins (none)  ▾"
        elif enabled:
            label = f"Plugins ({enabled}/{len(plugins)} enabled)  ▾"
        else:
            label = f"Plugins ({len(plugins)})  ▾"
        self.plugin_menu.configure(text=label)

    def _toggle_plugin_dropdown(self):
        if self.plugin_dropdown and self.plugin_dropdown.winfo_exists():
            self._close_plugin_dropdown()
            return
        if not self.plugin_menu or not self.plugin_menu.winfo_exists():
            return

        # A plugin can be copied in while AutoScript is open, so refresh manifests
        # whenever this list is opened. Discovery never imports plugin code.
        self.plugin_manager.discover()
        self._refresh_plugin_menu()
        dropdown = ctk.CTkToplevel(self)
        dropdown.overrideredirect(True)
        dropdown.configure(fg_color=BG_CONTROL)
        dropdown.attributes("-topmost", True)
        dropdown.transient(self.settings_popup or self)
        _mark_as_tool_window(dropdown)
        _round_window_corners(dropdown)

        content = ctk.CTkScrollableFrame(dropdown, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=6, pady=6)
        self._plugin_toggle_vars = {}
        self._plugin_status_labels = {}

        plugins = list(self.plugin_manager.plugins.values())
        if not plugins:
            ctk.CTkLabel(
                content,
                text="No plugins found. Add plugin folders beside AutoScript.exe.",
                text_color=FG_FAINT,
                font=app_font(12),
                wraplength=300,
                justify="left",
            ).pack(padx=10, pady=10, anchor="w")
        else:
            for plugin in plugins:
                row = ctk.CTkFrame(content, fg_color="transparent")
                row.pack(fill="x", pady=3, padx=4)
                enabled_var = ctk.BooleanVar(value=plugin.enabled)
                self._plugin_toggle_vars[plugin.plugin_id] = enabled_var
                can_toggle = plugin.status not in {"Incompatible", "Invalid manifest", "Duplicate ID"}
                checkbox = ctk.CTkCheckBox(
                    row,
                    text=f"{plugin.name}  v{plugin.version}",
                    variable=enabled_var,
                    command=lambda plugin_id=plugin.plugin_id: self._set_plugin_enabled(plugin_id),
                    fg_color=ACCENT,
                    hover_color=ACCENT_HOVER,
                    text_color=FG_TEXT,
                    font=app_font(12),
                )
                checkbox.pack(anchor="w", side="left")
                if not can_toggle:
                    checkbox.configure(state="disabled")
                if plugin.instance is not None and callable(
                    getattr(plugin.instance, "open_settings", None)
                ):
                    ctk.CTkButton(
                        row,
                        text="Configure",
                        command=lambda plugin_id=plugin.plugin_id: self._open_plugin_settings(plugin_id),
                        fg_color="transparent",
                        hover_color=BG_SIDEBAR_HOVER,
                        text_color=ACCENT,
                        font=app_font(10),
                        width=76,
                        height=24,
                    ).pack(anchor="e", side="right")
                detail = plugin.detail or plugin.description or plugin.status
                status = ctk.CTkLabel(
                    row,
                    text=f"{plugin.status} — {detail}" if detail else plugin.status,
                    text_color=FG_FAINT if plugin.status in {"Disabled", "Ready"} else FG_MUTED,
                    font=app_font(10),
                    anchor="w",
                    justify="left",
                    wraplength=300,
                )
                status.pack(fill="x", padx=(28, 0), pady=(0, 3))
                self._plugin_status_labels[plugin.plugin_id] = status

        dropdown.update_idletasks()
        logical_width = max(self.plugin_menu.winfo_reqwidth(), 350)
        logical_height = min(dropdown.winfo_reqheight(), 300)
        x = self.plugin_menu.winfo_rootx()
        y = self.plugin_menu.winfo_rooty() + self.plugin_menu.winfo_height() + 4
        dropdown.geometry(f"{logical_width}x{logical_height}+{x}+{y}")
        _round_window_corners(dropdown)
        self.plugin_dropdown = dropdown

    def _set_plugin_enabled(self, plugin_id: str):
        enabled = self._plugin_toggle_vars[plugin_id].get()
        plugin = self.plugin_manager.set_enabled(plugin_id, enabled)
        # A failed activation remains checked: it reflects the user's saved
        # intent and lets a repaired plugin start automatically next launch.
        label = self._plugin_status_labels.get(plugin_id)
        if label and label.winfo_exists():
            detail = plugin.detail or plugin.description or plugin.status
            label.configure(
                text=f"{plugin.status} — {detail}" if detail else plugin.status,
                text_color=FG_FAINT if plugin.status in {"Disabled", "Ready"} else FG_MUTED,
            )
        self._refresh_plugin_menu()
        # Enabling a configurable plugin creates its instance. Rebuild the
        # small dropdown immediately so its Configure button appears without
        # making the user close and reopen Settings themselves.
        if enabled and plugin.instance is not None:
            self._close_plugin_dropdown()
            self.after_idle(self._toggle_plugin_dropdown)

    def _open_plugin_settings(self, plugin_id: str):
        plugin = self.plugin_manager.open_settings(plugin_id, self)
        label = self._plugin_status_labels.get(plugin_id)
        if label and label.winfo_exists():
            detail = plugin.detail or plugin.description or plugin.status
            label.configure(text=f"{plugin.status} — {detail}" if detail else plugin.status)

    def _close_plugin_dropdown(self):
        if self.plugin_dropdown and self.plugin_dropdown.winfo_exists():
            self.plugin_dropdown.destroy()
        self.plugin_dropdown = None
        self._plugin_toggle_vars = {}
        self._plugin_status_labels = {}

    def _set_recording_mode(self, value: str) -> None:
        """Reflect the selected source in the controls without changing it mid-recording."""
        self_transcription = value == "Self transcription"
        if self.target_menu and self.target_menu.winfo_exists():
            self.target_menu.configure(state="disabled" if self_transcription else "normal")
        if hasattr(self, "start_btn") and not self.running and not self.starting:
            self.start_btn.configure(text="●  Start Voice Note" if self_transcription else "●  Start Recording")

    def start(self):
        if self.running or self.starting:
            return
        if self._disclosure_popup and self._disclosure_popup.winfo_exists():
            return
        if self.recording_mode_var.get() != "Self transcription":
            self._show_device_disclosure_dialog()
            return
        self._begin_recording(None)

    def _begin_recording(self, disclosure_confirmed_at: str | None) -> None:
        """Begin capture only after the standalone-device disclosure gate passes."""
        if self.running or self.starting:
            return
        try:
            chunk_seconds = int(self.chunk_var.get())
        except ValueError:
            chunk_seconds = 10
        # Read every Tk-backed setting on the GUI thread before handing work
        # to the background thread.
        model_size = self.model_var.get().lower()
        keep_audio = bool(self.keep_audio_var.get())
        include_microphone = bool(self.include_microphone_var.get())
        self_transcription = self.recording_mode_var.get() == "Self transcription"
        target_pid = None if self_transcription else self.target_pid
        target_process_name = None if self_transcription else self.target_process_name
        target_label = "Voice Note" if self_transcription else self.target_display_name
        self.starting = True
        self._start_cancelled.clear()
        self.start_btn.configure(state="disabled")
        if self.recording_mode_toggle:
            self.recording_mode_toggle.configure(state="disabled")
        self.stop_btn.configure(state="normal", text="Cancel")
        self._set_busy("Preparing recording...")
        threading.Thread(
            target=self._start_backend,
            args=(chunk_seconds, model_size, keep_audio, include_microphone, self_transcription, target_pid, target_process_name, target_label, disclosure_confirmed_at),
            daemon=True,
        ).start()

    def _queue_ui(self, action) -> None:
        """Schedule a GUI action without calling Tkinter from a worker."""
        self.ui_queue.put(action)

    def _cancel_start_if_requested(self, recorder) -> bool:
        if not self._start_cancelled.is_set():
            return False
        try:
            recorder.stop()
        except Exception:
            pass
        self._queue_ui(self._reset_buttons)
        return True

    def _start_backend(self, chunk_seconds: int, model_size: str, keep_audio: bool,
                       include_microphone: bool, self_transcription: bool,
                       target_pid: int | None, target_process_name: str | None,
                       target_label: str, disclosure_confirmed_at: str | None) -> None:
        # This method deliberately performs no Tkinter calls.  It runs on a
        # worker thread while _poll_queue executes queued UI work on Tk's
        # main thread.
        try:
            from transcriber import TranscriptionWorker, load_model
            CHUNKS_DIR.mkdir(exist_ok=True)
            TRANSCRIPTS_DIR.mkdir(exist_ok=True)
        except Exception as exc:
            self._queue_ui(lambda message=str(exc): self.status_var.set(f"Recording setup failed: {message}"))
            self._queue_ui(self._reset_buttons)
            return

        if target_process_name and not process_running(target_process_name):
            self._queue_ui(lambda: self.status_var.set(f"{target_label} not detected - capture may not isolate its audio"))

        chunk_q: "queue.Queue" = queue.Queue()
        self._queue_ui(lambda: self._set_busy("Checking microphone..." if self_transcription else f"Checking {target_label} audio..."))
        try:
            recorder, mode, reason = make_recorder(
                chunk_seconds, CHUNKS_DIR, chunk_q,
                target_pid=target_pid, on_error=self._on_capture_error,
                include_microphone=include_microphone,
                microphone_only=self_transcription,
            )
        except Exception as exc:
            self._queue_ui(lambda message=str(exc): self.status_var.set(f"Capture failed: {message}"))
            self._queue_ui(self._reset_buttons)
            return
        if self._cancel_start_if_requested(recorder):
            return

        opening_note = None
        if mode == "full" and target_pid is not None:
            decision = {"proceed": None}
            confirmed = threading.Event()
            self._queue_ui(lambda: self._show_capture_fallback_dialog(target_label, reason, decision, confirmed))
            # Do not leave Cancel waiting behind a modal confirmation prompt.
            while not confirmed.wait(0.1):
                if self._start_cancelled.is_set():
                    try:
                        recorder.stop()
                    except Exception:
                        pass
                    self._queue_ui(self._reset_buttons)
                    return
            if not decision["proceed"] or self._cancel_start_if_requested(recorder):
                try:
                    recorder.stop()
                except Exception:
                    pass
                self._queue_ui(self._reset_buttons)
                return
            opening_note = f"**[Capturing full system output — could not isolate {target_label} ({reason})]**\n\n"

        self._queue_ui(lambda: self._set_busy("Loading transcription model..."))
        try:
            model, device = load_model(model_size)
        except Exception as exc:
            self._queue_ui(lambda message=str(exc): self.status_var.set(f"Model load failed: {message}"))
            try:
                recorder.stop()
            except Exception:
                pass
            self._queue_ui(self._reset_buttons)
            return
        if self._cancel_start_if_requested(recorder):
            return

        self._queue_ui(lambda: self._set_busy("Starting audio capture..."))

        today = date.today().isoformat()
        stamp = datetime.now().strftime("%d%b%y_%H%M%S").upper()
        path = TRANSCRIPTS_DIR / f"{_safe_filename_stem(target_label)}_{stamp}.asenc"
        header_title = "Voice Note" if self_transcription else "Transcript"
        disclosure_note = (
            f"**[Operator confirmed participants were informed before recording at {disclosure_confirmed_at}.]**\n\n"
            if disclosure_confirmed_at else ""
        )
        header = f"## {header_title} — {today}\n\n" + disclosure_note + (opening_note or "")
        token = new_token()
        record_id = secrets.token_urlsafe(16)
        try:
            self.token_vault.save(record_id, token)
            encrypted_log = EncryptedLog(path, token, record_id, header)
            worker = TranscriptionWorker(
                model, chunk_q, path,
                delete_chunks=not keep_audio,
                on_line=lambda line: self.line_queue.put(line),
                write_line=encrypted_log.append,
            )
            recorder.start()
            worker.start()
        except Exception as exc:
            try:
                recorder.stop()
            except Exception:
                pass
            path.unlink(missing_ok=True)
            self._queue_ui(lambda message=str(exc): self.status_var.set(f"Recording failed to start: {message}"))
            self._queue_ui(self._reset_buttons)
            return

        self.transcript_path = path
        self._active_encrypted_log = encrypted_log
        self.recorder = recorder
        self.worker = worker
        self.running = True
        self.active_capture_mode = mode
        self.active_self_transcription = self_transcription
        self.active_include_microphone = include_microphone
        self._queue_ui(lambda: self._copy_new_transcript_token(token))
        self._queue_ui(lambda: self.file_var.set(str(path)))
        self._queue_ui(self._refresh_history)
        self._queue_ui(lambda: self.status_var.set(f"Model '{model_size.title()}' on {device}"))
        self._queue_ui(self._on_recording_started)

    def _show_device_disclosure_dialog(self) -> None:
        """Require a fresh, explicit disclosure acknowledgement for device capture."""
        popup = ctk.CTkToplevel(self)
        self._disclosure_popup = popup
        popup.title("Before recording")
        popup.configure(fg_color=BG_CONTROL)
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        _round_window_corners(popup)

        ctk.CTkLabel(
            popup,
            text="Before you record",
            text_color=FG_TEXT,
            font=app_font(17, "bold"),
        ).pack(anchor="w", padx=22, pady=(20, 8))
        ctk.CTkLabel(
            popup,
            text=(
                "Device capture may transcribe people other than you. Tell every "
                "participant that transcription is about to begin before continuing."
            ),
            text_color=FG_MUTED,
            font=app_font(12),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", padx=22, pady=(0, 10))
        ctk.CTkLabel(
            popup,
            text=(
                "Starting records your confirmation in this encrypted transcript. "
                "It does not replace consent requirements or platform rules."
            ),
            text_color=FG_FAINT,
            font=app_font(11),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", padx=22, pady=(0, 18))

        def resolve(proceed: bool) -> None:
            if not popup.winfo_exists():
                return
            try:
                popup.grab_release()
            except tk.TclError:
                pass
            popup.destroy()
            self._disclosure_popup = None
            if proceed:
                self._begin_recording(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        popup.protocol("WM_DELETE_WINDOW", lambda: resolve(False))
        buttons = ctk.CTkFrame(popup, fg_color="transparent")
        buttons.pack(fill="x", padx=22, pady=(0, 20))
        ctk.CTkButton(
            buttons, text="Cancel", command=lambda: resolve(False),
            fg_color=BG_SIDEBAR, hover_color=BG_SIDEBAR_HOVER, width=105,
            font=app_font(12),
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="I've informed participants", command=lambda: resolve(True),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, width=200,
            font=app_font(12, "bold"),
        ).pack(side="right")

        popup.update_idletasks()
        width = popup.winfo_reqwidth()
        height = popup.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        popup.geometry(f"{width}x{height}+{x}+{y}")

    def _install_segmented_transcript_scrollbar(self) -> None:
        """Keep CTkTextbox scrolling but replace only its visual thumb."""
        old_scrollbar = self.textbox._y_scrollbar
        old_scrollbar.destroy()
        self.textbox._y_scrollbar = SegmentedScrollbar(
            self.textbox,
            width=14,
            height=0,
            border_spacing=0,
            minimum_pixel_length=28,
            fg_color=BG_MAIN,
            button_color=FG_FAINT,
            button_hover_color=FG_MUTED,
            orientation="vertical",
            command=self.textbox._textbox.yview,
        )
        self.textbox._textbox.configure(yscrollcommand=self.textbox._y_scrollbar.set)
        self.textbox._create_grid_for_text_and_scrollbars(re_grid_y_scrollbar=True)

    def _show_capture_fallback_dialog(self, target_display_name: str, reason: str, decision: dict, confirmed: threading.Event):
        self._set_busy("Audio source needs confirmation...")
        popup = ctk.CTkToplevel(self)
        self._capture_fallback_popup = popup
        popup.overrideredirect(True)
        popup.configure(fg_color=BG_CONTROL)
        popup.attributes("-topmost", True)
        _mark_as_tool_window(popup)
        popup.grab_set()

        ctk.CTkLabel(
            popup,
            text=f"Couldn't isolate {target_display_name}'s audio ({reason}).",
            text_color=FG_TEXT,
            font=app_font(13, "bold"),
            wraplength=320,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 6))

        ctk.CTkLabel(
            popup,
            text="Start recording full system audio instead?",
            text_color=FG_MUTED,
            font=app_font(12),
            wraplength=320,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 14))

        def resolve(proceed: bool):
            decision["proceed"] = proceed
            confirmed.set()
            self._capture_fallback_popup = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", lambda: resolve(False))

        btn_row = ctk.CTkFrame(popup, fg_color="transparent")
        btn_row.pack(padx=16, pady=(0, 14))
        ctk.CTkButton(
            btn_row, text="Cancel", command=lambda: resolve(False), fg_color=BG_SIDEBAR,
            hover_color=BG_SIDEBAR_HOVER, width=110, font=app_font(12),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Capture Everything Instead", command=lambda: resolve(True),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, width=190, font=app_font(12),
        ).pack(side="left")

        # DPI-scaling-safe two-pass measure-then-clamp positioning - see
        # _rename_transcript for the same pattern and why it's needed.
        popup.update()
        w = popup.winfo_reqwidth()
        h = popup.winfo_reqheight()
        popup.geometry(f"{w}x{h}+0+0")
        popup.update()
        actual_w = popup.winfo_width()
        actual_h = popup.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - actual_w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - actual_h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")
        _round_window_corners(popup)

    def _on_recording_started(self):
        self.starting = False
        self._clear_busy()
        if self.active_self_transcription:
            detail = " — microphone only"
        else:
            detail = " — full system output" if self.active_capture_mode == "full" else f" — {self.target_display_name} only"
        if not self.active_self_transcription and getattr(self, "active_include_microphone", False):
            detail += " + microphone (You)"
        self.status_var.set(
            f"{'Voice note recording' if self.active_self_transcription else 'Recording'}...{detail} — first transcript update in about {self.chunk_var.get() or '10'} seconds"
        )
        self.title_var.set("Voice Note..." if self.active_self_transcription else "Recording...")
        self.title("AutoScript - Voice Note" if self.active_self_transcription else "AutoScript - Recording")
        self.stop_btn.configure(state="normal", text="Stop")
        self.mode = "new"
        self.selected_history_path = None
        self._refresh_history()

    def _copy_new_transcript_token(self, token: str):
        self.clipboard_clear()
        self.clipboard_append(token)
        self.update()
        self.status_var.set("Recording encrypted — unique sharing token copied to clipboard")
        self.after(60000, lambda: self._clear_token_from_clipboard(token))

    def _clear_token_from_clipboard(self, token: str):
        try:
            if self.clipboard_get() == token:
                self.clipboard_clear()
                self.update()
        except tk.TclError:
            pass

    def stop(self):
        if self.starting:
            self._start_cancelled.set()
            if self._capture_fallback_popup and self._capture_fallback_popup.winfo_exists():
                self._capture_fallback_popup.grab_release()
                self._capture_fallback_popup.destroy()
            self._capture_fallback_popup = None
            self.stop_btn.configure(state="disabled")
            self._set_busy("Cancelling recording setup...")
            return
        if not self.running:
            return
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping - finishing current chunk...")
        self.title("AutoScript - Stopping...")
        threading.Thread(target=self._stop_backend, daemon=True).start()

    def _stop_backend(self):
        if self.recorder:
            self.recorder.stop()
        if self.worker:
            self.worker.stop(drain=True)
        self.running = False
        self._queue_ui(self._reset_buttons)

    def _reset_buttons(self):
        self.starting = False
        self._clear_busy()
        self.start_btn.configure(state="normal")
        if self.recording_mode_toggle:
            self.recording_mode_toggle.configure(state="normal")
        self.stop_btn.configure(state="disabled", text="Stop")
        self.status_var.set("Idle")
        self.title("AutoScript - Idle")
        if self.transcript_path:
            self.title_var.set(self._display_title(self.transcript_path))
            self._load_live_transcript()
        self._refresh_history()

    def _on_capture_error(self, message: str, fatal: bool):
        # called from the recorder's own background thread - marshal to the
        # main thread, and never join/stop the recorder from itself here.
        def apply():
            self.status_var.set(message)
            if not fatal:
                return
            if self.transcript_path:
                try:
                    if self._active_encrypted_log:
                        self._active_encrypted_log.append(f"**[{message}]**\n")
                except OSError:
                    pass
            if self.worker:
                self.worker.stop(drain=False)
            self.running = False
            self._reset_buttons()

        self._queue_ui(apply)

    # ---------- live update polling ----------

    def _poll_queue(self):
        got_line = False
        got_ui_work = False
        try:
            while True:
                action = self.ui_queue.get_nowait()
                try:
                    action()
                except Exception:
                    # Keep the UI event loop alive if a nonessential status
                    # update fails while a window is closing.
                    pass
                got_ui_work = True
        except queue.Empty:
            pass
        try:
            while True:
                self.line_queue.get_nowait()
                got_line = True
        except queue.Empty:
            pass
        now = time.monotonic()
        if got_line or got_ui_work or now - self._last_history_scan >= 2.0:
            if self.mode == "new" and self.running:
                self._load_live_transcript()
            self._refresh_history()
            self._last_history_scan = now
        self.after(250, self._poll_queue)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Quit", "Recording is in progress. Stop and quit?"):
                return
            self._stop_backend()
        self.plugin_manager.shutdown()
        self.destroy()


def _load_data_dir() -> Path | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return Path(cfg["data_dir"])
    except (OSError, ValueError, KeyError):
        return None


def _save_data_dir(path: Path):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"data_dir": str(path)}), encoding="utf-8")
    except OSError:
        pass


def _migrate_legacy_config():
    """Adopt existing settings and encrypted plugin configuration after rebranding."""
    if CONFIG_DIR.exists() or not LEGACY_CONFIG_DIR.exists():
        return
    try:
        shutil.copytree(LEGACY_CONFIG_DIR, CONFIG_DIR)
    except OSError:
        pass


def _run_first_time_setup(default_dir: Path) -> Path:
    """One-time picker for where recordings/transcripts get stored. Shown
    only when there's no saved choice and no existing data at the default
    location (see `main`)."""
    chosen = {"path": None}
    userprofile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    documents = userprofile / "Documents" / "AutoScript"
    desktop = userprofile / "Desktop" / "AutoScript"

    dlg = ctk.CTk()
    dlg.title("AutoScript Setup")
    dlg.geometry("480x360")
    dlg.configure(fg_color=BG_MAIN)
    if ICON_PATH.exists():
        try:
            dlg.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

    ctk.CTkLabel(
        dlg, text="Where should recordings be saved?",
        font=app_font(16, "bold"), text_color=FG_TEXT,
    ).pack(padx=24, pady=(24, 6), anchor="w")
    ctk.CTkLabel(
        dlg,
        text="Transcripts and settings will live in a folder here.\n"
             "This is asked once - it won't come up again.",
        font=app_font(12), text_color=FG_MUTED, justify="left",
    ).pack(padx=24, anchor="w")

    def pick(path: Path):
        chosen["path"] = path
        dlg.destroy()

    def browse():
        folder = filedialog.askdirectory(
            title="Choose a folder for AutoScript data", parent=dlg
        )
        if folder:
            pick(Path(folder) / "AutoScript")

    btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_frame.pack(padx=24, pady=20, fill="x")

    options = [
        ("Default (next to this program)", default_dir),
        ("Documents", documents),
        ("Desktop", desktop),
    ]
    for label, path in options:
        row = ctk.CTkButton(
            btn_frame, text=label, anchor="w", fg_color=BG_CONTROL,
            hover_color=BG_CONTROL_HOVER, text_color=FG_TEXT, font=app_font(13),
            height=40, command=lambda p=path: pick(p),
        )
        row.pack(fill="x", pady=4)
        Tooltip(row, str(path))

    ctk.CTkButton(
        btn_frame, text="Choose a different folder...", anchor="w", fg_color=BG_CONTROL,
        hover_color=BG_CONTROL_HOVER, text_color=FG_TEXT, font=app_font(13),
        height=40, command=browse,
    ).pack(fill="x", pady=4)

    # closing the window without picking = just use the default rather than
    # leaving the app unable to start at all
    dlg.protocol("WM_DELETE_WINDOW", lambda: pick(default_dir))
    dlg.mainloop()
    return chosen["path"] or default_dir


def main():
    global ROOT, CHUNKS_DIR, TRANSCRIPTS_DIR, PINNED_FILE, NAMES_FILE

    _migrate_legacy_config()
    default_root = ROOT
    saved = _load_data_dir()
    if saved is not None:
        data_dir = saved
    elif TRANSCRIPTS_DIR.exists():
        # data already exists at the pre-this-feature default - adopt it
        # silently rather than surprising an existing user with a prompt
        data_dir = default_root
        _save_data_dir(data_dir)
    else:
        data_dir = _run_first_time_setup(default_root)
        _save_data_dir(data_dir)

    ROOT = data_dir
    CHUNKS_DIR = ROOT / "chunks"
    TRANSCRIPTS_DIR = ROOT / "transcripts"
    PINNED_FILE = TRANSCRIPTS_DIR / ".pinned.json"
    NAMES_FILE = TRANSCRIPTS_DIR / ".names.json"
    ROOT.mkdir(parents=True, exist_ok=True)
    # Whisper needs short-lived WAV chunks while it transcribes. Remove leftovers
    # from an interrupted prior session before any new recording starts.
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    for stale_chunk in CHUNKS_DIR.glob("*.wav"):
        try:
            stale_chunk.unlink()
        except OSError:
            pass

    app = TranscriberApp()
    app.mainloop()


if __name__ == "__main__":
    main()
