# Future Exploration

## Arma Reforger Identity Bridge

**Status:** Deferred; feasibility test required before implementation.

Explore a paired Arma Reforger addon and optional AutoScript plugin that labels locally captured Reforger voice traffic with each speaker's in-game display name.

Proposed local-only flow:

1. A Reforger addon maps connected player IDs to in-game display names.
2. The addon writes timestamped local voice-activity events to a local file.
3. A AutoScript plugin watches that file while capturing Reforger audio and uses matching timestamps to label transcript segments.

The first gate is to verify whether Reforger's VoN API exposes per-speaker voice activity and/or raw per-speaker streams to addon scripting. A mixed process-audio capture cannot reliably separate overlapping proximity or radio speakers after the fact. If raw per-speaker streams are unavailable, scope the feature to best-effort labels for non-overlapping transmissions and disclose that limitation.

Privacy requirement: no cloud service, telemetry, audio relay, or external transcript processing. Any communication between the addon and AutoScript must remain local to the user's machine.
