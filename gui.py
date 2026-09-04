"""CDCT (Call Data & Conversation Transcripts) - local voice-to-text dictation.

Captures your PC's audio output and transcribes it live to a markdown file.
Works with any audio source - a Discord/Zoom/Teams call, a video, or just
your own voice - not tied to any single application.

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
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image
from tkinter import filedialog, messagebox

from capture import LoopbackRecorder, list_open_windows, process_running

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

CHUNKS_DIR = ROOT / "chunks"
TRANSCRIPTS_DIR = ROOT / "transcripts"
ICON_PATH = RESOURCES / "icon.ico"
LOGO_PATH = RESOURCES / "logo.png"
PINNED_FILE = TRANSCRIPTS_DIR / ".pinned.json"
NAMES_FILE = TRANSCRIPTS_DIR / ".names.json"

# Small per-user config (just "where's the data") that lives in a fixed OS
# location regardless of where the user picks to store everything else -
# otherwise there'd be nowhere reliable to remember that choice from.
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "CDCT"
CONFIG_FILE = CONFIG_DIR / "config.json"

FONT_FAMILY = "Cascadia Mono"


def app_font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


# --- palette: near-black, warm-neutral, single accent ---
BG_MAIN = "#171716"
BG_SIDEBAR = "#0E0E0D"
BG_SIDEBAR_HOVER = "#202020"
BG_SIDEBAR_ACTIVE = "#2A2A27"
BG_TEXTBOX = "#101010"
BG_CONTROL = "#262623"
BG_CONTROL_HOVER = "#343430"
FG_TEXT = "#EDEBE6"
FG_MUTED = "#9C9A94"
FG_FAINT = "#68685F"
FG_TIMESTAMP = "#7A8A99"
ACCENT = "#0762CB"
ACCENT_HOVER = "#0654AD"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

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
        self.title("CDCT - Idle")
        self.geometry("1040x670")
        self.minsize(780, 480)
        self.configure(fg_color=BG_MAIN)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        self.recorder: LoopbackRecorder | None = None
        self.worker: TranscriptionWorker | None = None
        self.transcript_path: Path | None = None
        self.line_queue: "queue.Queue[str]" = queue.Queue()
        self.running = False
        self.mode = "new"  # "new" or "history"
        self.selected_history_path: Path | None = None
        self.history_buttons: dict[Path, ctk.CTkButton] = {}
        self.current_raw_text = ""
        self.pinned: set[str] = self._load_pinned()
        self.names: dict[str, str] = self._load_names()

        # settings state - lives here so it persists whether or not the
        # settings popover happens to be open
        self.model_var = ctk.StringVar(value="Small")
        self.chunk_var = ctk.StringVar(value="30")
        self.keep_audio_var = ctk.BooleanVar(value=False)
        self.target_process_name: str | None = "Discord.exe"
        self.target_display_name = "Discord"
        self.settings_popup = None

        self._build_sidebar()
        self._build_main()
        self.show_new_recording()
        self._refresh_history()
        self._poll_queue()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI construction ----------

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=250, fg_color=BG_SIDEBAR, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(24, 18))
        if LOGO_PATH.exists():
            pil_logo = Image.open(LOGO_PATH)
            w, h = pil_logo.size
            display_w = 210
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
                text="Call Data & Conversation Transcripts",
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
            corner_radius=8,
            height=36,
            anchor="w",
            font=app_font(13, "bold"),
        ).pack(fill="x", padx=12, pady=(0, 14))

        ctk.CTkLabel(
            sidebar,
            text="RECENT CALLS",
            text_color=FG_FAINT,
            font=app_font(11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 4))

        footer = ctk.CTkFrame(sidebar, fg_color="transparent", height=56)
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
        self.gear_btn.pack(side="left", padx=16, pady=10)
        Tooltip(self.gear_btn, "Settings: model, chunk length, capture target, keep audio.")

        self.history_scroll = ctk.CTkScrollableFrame(
            sidebar, fg_color="transparent", scrollbar_button_color=BG_SIDEBAR_HOVER
        )
        self.history_scroll.pack(fill="both", expand=True, padx=6, pady=(0, 8))

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        main.pack(side="left", fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(22, 4))
        self.title_var = ctk.StringVar(value="New Recording")
        title_label = ctk.CTkLabel(
            header,
            textvariable=self.title_var,
            font=app_font(20, "bold"),
            text_color=FG_TEXT,
            cursor="hand2",
        )
        title_label.pack(side="left")
        title_label.bind("<Button-1>", self._on_title_click)
        Tooltip(title_label, "Click to rename this session.")
        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            header, textvariable=self.status_var, text_color=FG_MUTED, font=app_font(12)
        ).pack(side="right")

        search_row = ctk.CTkFrame(main, fg_color="transparent")
        search_row.pack(fill="x", padx=28, pady=(6, 10))
        # NOTE: customtkinter silently ignores placeholder_text when a
        # textvariable is bound, so this drives filtering off the entry's
        # own .get() via <KeyRelease> instead of a StringVar + trace.
        self.search_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Search Transcript",
            fg_color=BG_TEXTBOX,
            border_color=BG_CONTROL,
            text_color=FG_TEXT,
            height=32,
            font=app_font(13),
        )
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self._apply_filter())
        Tooltip(self.search_entry, "Type to find lines in this transcript.")

        self.controls = ctk.CTkFrame(main, fg_color="transparent")
        self.controls.pack(fill="x", padx=28, pady=(0, 14))

        self.start_btn = ctk.CTkButton(
            self.controls,
            text="Start",
            command=self.start,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            width=90,
            font=app_font(13),
        )
        self.start_btn.pack(side="left", padx=(0, 6))
        Tooltip(self.start_btn, "Start recording and transcribing.")
        self.stop_btn = ctk.CTkButton(
            self.controls,
            text="Stop",
            command=self.stop,
            fg_color=BG_CONTROL,
            hover_color=BG_CONTROL_HOVER,
            width=90,
            state="disabled",
            font=app_font(13),
        )
        self.stop_btn.pack(side="left")
        Tooltip(self.stop_btn, "Stop recording.")

        self.textbox = ctk.CTkTextbox(
            main,
            fg_color=BG_TEXTBOX,
            text_color=FG_TEXT,
            corner_radius=10,
            font=app_font(13),
            wrap="word",
        )
        self.textbox.pack(fill="both", expand=True, padx=28, pady=(0, 14))
        self.textbox.configure(state="disabled")
        self.textbox._textbox.tag_config("ts", foreground=FG_TIMESTAMP)
        self.textbox._textbox.tag_config("system", foreground=FG_FAINT)
        self.textbox._textbox.tag_config("body", foreground=FG_TEXT)

        bottom = ctk.CTkFrame(main, fg_color="transparent")
        bottom.pack(fill="x", padx=28, pady=(0, 18))
        self.file_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            bottom, textvariable=self.file_var, text_color=FG_FAINT, font=app_font(11)
        ).pack(side="left")
        ctk.CTkButton(
            bottom,
            text="Open Folder",
            command=self.open_folder,
            fg_color=BG_CONTROL,
            hover_color=BG_CONTROL_HOVER,
            width=110,
            font=app_font(12),
        ).pack(side="right")

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
        paths = TRANSCRIPTS_DIR.glob("transcript_*.md")
        return sorted(
            paths,
            key=lambda p: (p.name not in self.pinned, -p.stat().st_mtime),
        )

    def _refresh_history(self, force: bool = False):
        paths = self._sorted_history_paths()
        order_changed = [p for p in self.history_buttons] != paths
        if force or set(paths) != set(self.history_buttons.keys()) or order_changed:
            for btn in self.history_buttons.values():
                btn.destroy()
            self.history_buttons = {}
            for p in paths:
                btn = ctk.CTkButton(
                    self.history_scroll,
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
                btn.pack(fill="x", pady=2)
                btn.bind("<Button-3>", lambda e, p=p: self._show_context_menu(e, p))
                self.history_buttons[p] = btn
        else:
            for p, btn in self.history_buttons.items():
                btn.configure(text=self._history_label(p))
        self._update_history_highlight()

    def _history_label(self, path: Path) -> str:
        if self.running and path == self.transcript_path:
            prefix = "●  "
        elif path.name in self.pinned:
            prefix = "📌 "
        else:
            prefix = "    "
        return f"{prefix}{self._display_title(path)}"

    def _show_context_menu(self, event, path: Path):
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
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_pin(self, path: Path):
        if path.name in self.pinned:
            self.pinned.discard(path.name)
        else:
            self.pinned.add(path.name)
        self._save_pinned()
        self._refresh_history(force=True)

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
        self.title_var.set(self._display_title(path))
        self.status_var.set("")
        self.file_var.set(str(path))
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = "(could not read this transcript file)"
        self._set_textbox_content(content)
        self._update_history_highlight()

    def _load_live_transcript(self):
        if self.transcript_path and self.transcript_path.exists():
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
            "Which open program you're calling in. This is just a check that it's "
            "open before you hit Start - it still records your whole speaker output "
            "either way, not only that program.",
        )
        self.target_btn = ctk.CTkButton(
            target_row,
            text=f"{self.target_display_name}  ▾",
            command=self._open_target_picker,
            anchor="w",
            fg_color=BG_SIDEBAR,
            hover_color=BG_SIDEBAR_HOVER,
            text_color=FG_TEXT,
            font=app_font(12),
        )
        self.target_btn.pack(fill="x", pady=(4, 0))

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

        self.settings_popup = popup
        self.bind_all("<Button-1>", self._maybe_close_settings, add="+")

    def _maybe_close_settings(self, event):
        popup = self.settings_popup
        if not popup or not popup.winfo_exists():
            return
        for widget in (popup, self.gear_btn):
            wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
            ww, wh = widget.winfo_width(), widget.winfo_height()
            if wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh:
                return
        self._close_settings_panel()

    def _close_settings_panel(self):
        if self.settings_popup and self.settings_popup.winfo_exists():
            self.settings_popup.destroy()
        self.settings_popup = None
        self.unbind_all("<Button-1>")

    def _open_target_picker(self):
        popup = ctk.CTkToplevel(self)
        popup.title("Select target program")
        popup.geometry("360x420")
        popup.configure(fg_color=BG_MAIN)
        popup.transient(self)
        popup.grab_set()

        ctk.CTkLabel(
            popup,
            text="Open programs",
            text_color=FG_MUTED,
            font=app_font(11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 4))

        scroll = ctk.CTkScrollableFrame(popup, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        windows = list_open_windows()
        if not windows:
            ctk.CTkLabel(
                scroll, text="No open windows found.", text_color=FG_FAINT, font=app_font(12)
            ).pack(pady=10)
        for title, pname in windows:
            label = title if len(title) <= 42 else title[:39] + "..."
            btn = ctk.CTkButton(
                scroll,
                text=label,
                anchor="w",
                fg_color="transparent",
                hover_color=BG_SIDEBAR_HOVER,
                text_color=FG_TEXT,
                font=app_font(12),
                corner_radius=6,
                height=36,
                command=lambda t=title, p=pname: self._pick_target(t, p, popup),
            )
            btn.pack(fill="x", pady=2)

        ctk.CTkButton(
            popup,
            text="Cancel",
            command=popup.destroy,
            fg_color=BG_CONTROL,
            hover_color=BG_CONTROL_HOVER,
            width=90,
            font=app_font(12),
        ).pack(pady=(0, 12))

    def _pick_target(self, title: str, process_name: str, popup):
        self.target_process_name = process_name
        display = title if len(title) <= 22 else title[:19] + "..."
        self.target_display_name = display
        self.target_btn.configure(text=f"{display}  ▾")
        popup.destroy()

    def start(self):
        if self.running:
            return
        try:
            chunk_seconds = int(self.chunk_var.get())
        except ValueError:
            chunk_seconds = 30
        self.start_btn.configure(state="disabled")
        self.status_var.set("Loading model (first run downloads it)...")
        threading.Thread(target=self._start_backend, args=(chunk_seconds,), daemon=True).start()

    def _start_backend(self, chunk_seconds: int):
        # deferred from module load - see the comment near the top of the file
        from transcriber import TranscriptionWorker, load_model

        CHUNKS_DIR.mkdir(exist_ok=True)
        TRANSCRIPTS_DIR.mkdir(exist_ok=True)

        process_name = self.target_process_name
        target_label = self.target_display_name
        if process_name and not process_running(process_name):
            self.after(
                0,
                lambda: self.status_var.set(
                    f"{target_label} not detected - capturing full output anyway"
                ),
            )

        today = date.today().isoformat()
        self.transcript_path = TRANSCRIPTS_DIR / f"transcript_{today}_{int(time.time())}.md"
        self.transcript_path.write_text(f"## Transcript — {today}\n\n", encoding="utf-8")
        self.after(0, self._refresh_history)
        self.after(0, lambda: self.file_var.set(str(self.transcript_path)))

        try:
            model, device = load_model(self.model_var.get().lower())
        except Exception as exc:
            self.after(0, lambda: self.status_var.set(f"Model load failed: {exc}"))
            self.after(0, self._reset_buttons)
            return
        self.after(0, lambda: self.status_var.set(f"Model '{self.model_var.get()}' on {device}"))

        chunk_q: "queue.Queue" = queue.Queue()
        try:
            self.recorder = LoopbackRecorder(
                chunk_seconds, CHUNKS_DIR, chunk_q, on_error=self._on_capture_error
            )
        except Exception as exc:
            self.after(0, lambda: self.status_var.set(f"Capture failed: {exc}"))
            self.after(0, self._reset_buttons)
            return

        self.worker = TranscriptionWorker(
            model,
            chunk_q,
            self.transcript_path,
            delete_chunks=not self.keep_audio_var.get(),
            on_line=lambda line: self.line_queue.put(line),
        )

        self.recorder.start()
        self.worker.start()
        self.running = True
        self.after(0, self._on_recording_started)

    def _on_recording_started(self):
        self.status_var.set("Recording...")
        self.title_var.set("Recording...")
        self.title("CDCT - Recording")
        self.stop_btn.configure(state="normal")
        self.mode = "new"
        self.selected_history_path = None
        self._refresh_history()

    def stop(self):
        if not self.running:
            return
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping - finishing current chunk...")
        self.title("CDCT - Stopping...")
        threading.Thread(target=self._stop_backend, daemon=True).start()

    def _stop_backend(self):
        if self.recorder:
            self.recorder.stop()
        if self.worker:
            self.worker.stop(drain=True)
        self.running = False
        self.after(0, self._reset_buttons)

    def _reset_buttons(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Idle")
        self.title("CDCT - Idle")
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
                    with open(self.transcript_path, "a", encoding="utf-8") as f:
                        f.write(f"**[{message}]**\n\n")
                except OSError:
                    pass
            if self.worker:
                self.worker.stop(drain=False)
            self.running = False
            self._reset_buttons()

        self.after(0, apply)

    # ---------- live update polling ----------

    def _poll_queue(self):
        got_line = False
        try:
            while True:
                self.line_queue.get_nowait()
                got_line = True
        except queue.Empty:
            pass
        if got_line:
            if self.mode == "new" and self.running:
                self._load_live_transcript()
            self._refresh_history()
        self.after(250, self._poll_queue)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Quit", "Recording is in progress. Stop and quit?"):
                return
            self._stop_backend()
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


def _run_first_time_setup(default_dir: Path) -> Path:
    """One-time picker for where recordings/transcripts get stored. Shown
    only when there's no saved choice and no existing data at the default
    location (see `main`)."""
    chosen = {"path": None}
    userprofile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    documents = userprofile / "Documents" / "CDCT"
    desktop = userprofile / "Desktop" / "CDCT"

    dlg = ctk.CTk()
    dlg.title("CDCT Setup")
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
            title="Choose a folder for CDCT data", parent=dlg
        )
        if folder:
            pick(Path(folder) / "CDCT")

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

    app = TranscriberApp()
    app.mainloop()


if __name__ == "__main__":
    main()
