# Changelog

## 2026-11-13: first public release

- Glass panel with live captions (faster-whisper `large-v3-turbo`, GPU), two level meters (microphone and computer audio), pause / resume, title entry, and a "Notes ready" view with links to the notes and the folder.
- After-meeting pipeline (`meeting_notes_process_v1.py`): final `large-v3` transcript, optional sherpa-onnx speaker separation, echo removal, verified Opus audio, notes through the `claude` CLI with `codex` as a fallback.
- Settings file `meeting_notes_settings.json` plus `MN_<KEY>` environment overrides. Defaults: local time zone, output in `Documents\Meetings`, cache in `%LOCALAPPDATA%\MeetingNotes`, native-minutes lookup and ledger hook off.
- Recording-consent reminder on the panel and in the README.
- `--demo <state> <png>` renders sample states on a plain background. `MN_TEST_FEED` replaces computer audio with a WAV file for tests.
- Safe reprocessing: a full run is refused with exit code 3 when the session is busy, finished, already holds saved results, lacks WAVs, or all three candidate output names are occupied. A refusal leaves meeting files, status and logs unchanged; only the session lock file may be created.
- Output-name collisions try the session timestamp with seconds, then a short id suffix.
- Retries: `--notes-only [--force]` rewrites only the notes from the saved `segments.json` and keeps earlier good notes if the retry fails. `--audio-only` re-encodes audio that failed to encode.
- WAV chunks are deleted only after verified audio and successful notes.
- Result files and session metadata are written through a temporary file.
- A Codex notes attempt counts only with exit code 0 and its own new output file.
- `--resume` finishes an interrupted session by running only its missing steps, keeping finished files, good notes and `--keep-wav`. A full run is refused once a transcript is saved.
- At start, the panel resumes one unfinished session, runs a session from the start only when nothing was saved yet, and cleans up leftover WAVs of finished sessions.
- Ownership: each recording has a random id stored in `session.json`, and its folder marker holds that id. Folders from older versions, or with another id, are left read-only.
- Each meeting is processed under a lock, so two processes can't work on one meeting at once.
- Before a transcript or notes file is replaced, its checksum is recorded, so a crash right after the write is recognized on resume.
- A failed notes attempt never replaces an existing `notes.md`.
- Only the WAV chunks that went into the saved audio are deleted. A chunk that appears later is kept.
- The panel processes a meeting only after its recorder has closed every file, and never replaces an unreadable `session.json`. It shows when the audio was not saved.
- The recorder never reuses an existing session folder: if no new one can be created, it does not record.
- A full run exits 0 only when both the notes and the audio are saved.
- An existing `status.json` that cannot be read (for example held by a virus scanner) is retried for a few seconds, then the run stops without rewriting it. It is never treated as empty.
- WAV chunks are read to the end of the file, so samples written just before a crash are kept even if the header was not updated.
- The built-in Markdown ledger writer is locked across processes, so two meetings finishing at once both get their row.
- A failed command records its error while still holding the meeting's lock, so it can never overwrite a newer command's state (such as `--keep-wav`).
