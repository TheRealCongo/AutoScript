# AutoScript

AutoScript is a free, open-source Windows application for local voice-to-text.
It records desktop audio or your own microphone, transcribes speech on your
computer, and stores each transcript locally.

Audio and transcript text stay on the recording computer. AutoScript downloads a
selected transcription model only when that model is not already installed.
The optional Discord plugin connects to Discord only while its bot is running.

## Recording modes

Use the **Device ↔ Notes** switch above Settings to choose the source.

- **Device** captures a selected application when Windows can isolate it, or the
  full system output when the operator explicitly approves that fallback.
- **Notes** captures only the default microphone for private voice notes. Lines
  from microphone capture are labeled **You**.

Before every Device recording, AutoScript requires the operator to confirm that
participants have been informed. The confirmation time is retained inside that
recording's transcript. Notes mode does not show this confirmation.

## Transcript privacy

The **Plain / Encrypted** switch in the sidebar controls the format for new
AutoScript recordings. It is enabled by default. Encrypted recordings use
AES-256-GCM and are saved as `TargetProgram_DDMMMYY_HHMMSS.asenc` or
`VoiceNote_DDMMMYY_HHMMSS.asenc`. Turning the switch off saves a standard
Markdown `.md` transcript instead.

Each transcript has its own high-entropy sharing token. AutoScript copies that
token to the recording computer's clipboard when recording starts and stores it
locally using Windows DPAPI. Use **Copy Token** from a transcript's gear menu if
you need to share it again.

To share a transcript, send the `.asenc` file and its token separately to the
recipient. A recipient opens the file in AutoScript and enters the token once;
AutoScript keeps that successful unlock in the recipient's local Windows vault.
Decryption happens in memory and does not create a plaintext copy.

The Discord bot token is never used to encrypt or decrypt transcripts.

## Using the app

1. Download `AutoScript.exe` from the project Releases page.
2. Choose a local folder for transcripts on first run.
3. Select **Device** or **Notes** in the sidebar footer.
4. Open Settings to choose the model, segment length, Device target, optional
   microphone input, retained audio chunks, and installed plugins.
5. Start recording. Transcript text appears as each audio segment is processed.

The sidebar lists recent calls and notes. You can search, rename, pin, delete,
copy a transcript token, or unlock a shared encrypted transcript.

## Optional Discord plugin

The AutoScript Discord Voice Transcriber joins a Discord server voice channel and
records each Discord member as a separately labeled local transcript. It uses the
same encrypted `.asenc` format and per-transcript sharing tokens as the base app.

## Responsible use

Tell participants before recording or transcribing them, follow applicable law
and platform rules, and protect transcript files as you would any sensitive
recording. See [LEGAL_GUIDELINES.md](LEGAL_GUIDELINES.md).

## Run from source

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe gui.py
```

## Build the Windows executable

```powershell
.\build_exe.ps1
```

The build produces `dist\AutoScript.exe` with the required capture,
transcription, encryption, and interface dependencies bundled.

## Project files

- `gui.py` — desktop interface, recording workflow, encrypted history, and
  first-run setup
- `capture.py` — Device and microphone capture
- `transcriber.py` — local speech transcription worker
- `transcript_security.py` — encrypted transcript format and Windows token vault
- `plugin_api.py` / `plugin_manager.py` — optional plugin contract and loader
- `plugins/` — install location for optional extensions

## License

[MIT](LICENSE)
