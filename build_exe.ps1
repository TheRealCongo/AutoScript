# Rebuilds dist\AutoScript.exe from source.
# Run from the project root: .\build_exe.ps1
Set-Location $PSScriptRoot

.\venv\Scripts\python.exe -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    .\venv\Scripts\python.exe -m pip install pyinstaller -q
}

.\venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name AutoScript `
  --icon icon.ico `
  --add-data "icon.ico;." `
  --add-data "autoscript_logo.png;." `
  --collect-all ctranslate2 `
  --collect-all av `
  --collect-all onnxruntime `
  --collect-all pyaudiowpatch `
  --collect-all proctap `
  --collect-all customtkinter `
  --hidden-import http.cookies `
  --hidden-import audioop `
  --collect-data faster_whisper `
  --collect-data tokenizers `
  gui.py

Write-Host "`nBuilt: dist\AutoScript.exe"
