# Changelog

## 4.0.3 — 2026-09-06

### Added

- Added a sidebar toggle for choosing encrypted `.asenc` or plaintext `.md`
  storage for new AutoScript recordings. Encryption remains enabled by default.

## 4.0.2 — 2026-09-06

### Changed

- Added a subtle release-version label beside Open Folder so users can identify the installed executable.

## 4.0.1 — 2026-09-06

### Fixed

- Corrected Windows DPAPI secret handling in the packaged application so the
  Discord plugin and AutoScript can safely use the same local protection code.
- Preserved compatibility with bot tokens and transcript tokens saved by earlier
  releases.
