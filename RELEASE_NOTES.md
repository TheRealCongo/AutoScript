## CDCT v2.0.0 — Per-Process Audio Capture

CDCT can now capture and transcribe one selected program instead of mixing all
speaker output together.

### What changed

- **Real per-program capture:** choose an app in Settings → **Capturing for**.
  CDCT captures that app's process tree through Windows WASAPI process
  loopback.
- **Honest fallback:** if CDCT cannot isolate the selected app, it asks before
  switching to full-system capture. It never falls back silently.
- **Cleaner selector:** the capture target is now a rounded dropdown in
  Settings. Long titles are shortened to fit, and CDCT/Windows system windows
  are excluded.
- **Reliable shutdown detection:** if the selected app closes while recording,
  CDCT stops capture and reports it instead of silently recording silence.

### Getting started

1. Run **CDCT.exe**.
2. Open the Settings gear and choose the program whose audio you want.
3. Start recording. The status line confirms `Recording... — [program] only`.
4. Stop when finished; the markdown transcript is saved automatically.

### Important scope

Capture is per **process**, not per window or per speaker. Two windows from
the same browser may share one process tree and cannot be split apart. CDCT
does not provide speaker diarization.

### Please read before recording others

Transcription is subject to the same consent and platform rules as recording.
Tell participants before recording, follow applicable law, and handle the
resulting transcript responsibly. See `LEGAL_GUIDELINES.md` in the repository
for project guidance.

### Requirements

Windows 10 version 2004 or newer, or Windows 11.

### Heads up

Windows may show a SmartScreen warning on first run because CDCT is not
commercially code-signed. Use **More info → Run anyway** only after reviewing
the public source code and deciding you trust the release.
