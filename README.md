# R6 Soundle Studio

R6 Soundle Studio is the authoring and capture workspace for the game. It keeps
recording, media processing, map coordination, set editing, and scheduling out
of the public game repository.

The current milestone is a local-first studio:

- A set always contains exactly three neutral slots: Round 1, Round 2, and
  Round 3. Difficulty is not part of the model.
- All three rounds use the set's map, but each may use any floor.
- Listener evidence is stored as one JPEG from the beginning of the aligned
  POV plus an M4A audio track.
- The replay is runner video with the aligned listener audio.
- Scheduling records a versioned set for midnight in `America/New_York`.
- Published puzzle responses are time-gated by the server clock.

## Start the studio

Python 3.11 or newer is recommended. Install the pinned timezone database once
so `America/New_York` release calculations behave identically on Windows and
Unix:

```powershell
python -m pip install -r requirements.txt
```

```powershell
scripts\run-studio.bat
```

The launcher expects the game repository at `..\R6-Soundle` by default and
opens <http://127.0.0.1:4180>. You can specify another checkout:

```powershell
python studio_server.py --game-repo D:\Projects\R6-Soundle
```

The game checkout supplies the current map manifest, blueprint images, and
operator catalog while the authoring bundle is being separated. Studio data is
stored in `studio.db`; newly processed capture media is organized under
`daily sets`.

## Capture with OBS

Run the two-computer recorder from Studio:

```powershell
scripts\run-obs-sync.bat
```

Before connecting, open OBS and enable **Tools > WebSocket Server Settings**.
On both computers, use **Install / Update 1080p60 Profile** once. It creates
the `R6 Soundle 1080p60` profile and writes each raw recording directly to the
flat `videos` folder. No per-session capture directories or timing sidecars are
created. Raw videos are intentionally ignored by Git. The OBS WebSocket
password is saved locally in the ignored `config.local.json` file after use.

The recorder registers `F7` as a global synchronized-session shortcut:
press it once to send **READY**, then press it again while recording to request
**STOP BOTH**. When both players are ready, the recorder counts down three
seconds and starts both OBS recordings at the same synchronized boundary. The
installed OBS profile intentionally clears OBS's own start/stop bindings for
this key so one keypress cannot start and immediately stop a local recording.

To use another storage drive, pass `--capture-directory D:\captures` or set
`R6_SOUNDLE_CAPTURE_DIR` before launching the recorder.

## Process named two-POV captures

Install FFmpeg and ensure `ffmpeg` and `ffprobe` are on `PATH`, then run:

```powershell
python processor\process_capture.py
```

With no input arguments, the processor finds MP4s and ZIP archives under the
Studio `videos` directory. It reads ZIP metadata to build the complete queue,
then extracts only one runner/listener pair at a time while processing.

Use `--input` to instead supply any combination of ZIP archives, MP4 files, and
directories. Directories are searched recursively. Every MP4 must use this
naming format:

```text
<mapset-number>-<map>-<slot>-<listener|runner>.mp4
```

For example, `1-clubhouse-3-listener.mp4` and
`1-clubhouse-3-runner.mp4` are one pair. Before processing starts, the command
checks that every key has exactly one listener and one runner. The processor
then estimates each pair's offset from its audio envelopes unless `--offset-ms`
is supplied.

To check the complete queue without running FFmpeg:

```powershell
python processor\process_capture.py --validate-only
```

Each pair writes exactly three files. The example above is written to
`daily sets\1-clubhouse\3`:

- `listener.jpg` — a single frame at the start of the aligned listener POV;
- `listener.m4a` — the listener audio used by the game;
- `replay.mp4` — the aligned runner POV with listener audio.

The processor builds these files in a temporary sibling directory, probes and
hashes them, validates the version-1 contract in `schemas\capture.schema.json`,
and commits `capture.json` last. A failed command therefore never leaves an
importable partial round. Existing output is never overwritten by default.
Use `--replace` to reprocess intentionally; the prior directory is retained as
a timestamped sibling backup after the atomic promotion succeeds.

To process one explicitly selected pair, use:

```powershell
python processor\process_capture.py `
  --listener path\1-clubhouse-3-listener.mp4 `
  --runner path\1-clubhouse-3-runner.mp4
```

Raw inputs are preserved by default. After reviewing all outputs, pass
`--remove-raw` to remove ordinary source MP4s. Recordings inside ZIP archives
cannot be removed by the processor.

## Index legacy processed captures

The legacy indexer validates the complete map-set/slot tree before it writes
anything. Its default and explicit `--dry-run` modes only report missing
manifests and hash the existing bytes:

```powershell
python processor\index_captures.py --root "daily sets" --dry-run
```

After backing up and reviewing a real daily-set directory, opt in with
`--write-manifests`. Write mode probes existing media and adds only missing
`capture.json` files; it never re-encodes, renames, or deletes media. Conflicting
manifests, partial rounds, extra files, symlinks, duplicate identities, and
noncanonical directory names stop the entire preflight. Repeating a successful
index is a no-op.

Do not run write mode against owner media until its separate backup has been
confirmed. The automated fixture check is safe:

```powershell
python processor\index_captures.py --root tests\fixtures\legacy-daily-sets --dry-run
```

## Import one complete daily set

Use **Import daily set** on the active set (or **Import daily set** in the
sidebar), enter only the processed folder name such as `1-clubhouse`,
and select **Scan three rounds**. Studio resolves it inside `daily sets`
automatically. The active-set
action preselects that compatible draft as the import target. The scan is
read-only and accepts only configured import roots. It requires exactly the
`1`, `2`, and `3` round directories; validates every version-1 manifest,
media size/hash, duration, identity, and map slug; and reports compatible
existing drafts before enabling import.

The confirm action revalidates the scan fingerprint and commits exactly three
capture upserts plus one new or compatible draft in a single SQLite
transaction. An identical retry is a no-op. Captures are available immediately;
there is no separate capture-approval step. If changed content is already attached to a
scheduled set, Studio requires an explicit stale acknowledgment and advances
the draft version so the old schedule cannot silently publish it.

The compatibility single-manifest importer remains available for one phase and
uses the same capture-v1 validation. Additional processed roots can be allowed
explicitly when starting Studio:

```powershell
python studio_server.py --import-root "D:\processed-r6-soundle"
```

Import infers the set number, map, slot, processing version, and optional
operator identity. The owner must still review media and author the fields that
cannot be inferred safely: runner operator when absent, listener position and
direction, runner start, and runner target. Future recorder metadata may supply
the optional `operatorId` and `recordingSessionId` capture-v1 source fields;
coordinates and listener direction remain explicit authoring inputs until a
trusted telemetry contract exists.

Existing Studio databases migrate additively on startup. Capture rows gain a
content fingerprint, schema/processing versions, map-set/slot identity, and an
import-source label; existing rows and local owner state are preserved.
Phase 5 also adds inert local publish-attempt and remote release-ID fields so a
later resumable publisher can record progress without putting database or
object-store credentials in Studio. Drafts and captures remain SQLite-only
owner state and are never imported automatically into production.

## Preview a draft in the real game

Choose a preview date on an open draft and select **Preview**. Studio saves the
current draft, snapshots that exact set version, and opens the game repository's
real `index.html` and JavaScript modules in a new tab. The preview date may be in
the future; this does not weaken the public game's future-date check.

Preview URLs contain a random session identifier and expire after 30 minutes.
Studio serves only the game index, allowlisted browser assets, and the listener
still/audio plus replay attached to that immutable session snapshot. Preview play state is stored under one
session-specific key, while accounts, leaderboards, and statistics are disabled.
Replay clears only that key. Incomplete rounds, missing media, stale map assets,
expired sessions, and incompatible contract/scoring versions are shown as
explicit preview failures or warnings. Closing a preview never edits its draft.

The game repository owns `contracts/preview-v1.schema.json`. Studio vendors the
supported contract under `schemas/` with source revision and canonical hash
metadata; the preview bridge test rejects unreviewed drift.

Studio also vendors the game-owned `release-v1` schema and provenance metadata.
Phase 4 establishes that compatibility boundary; the remote publisher does not
use it until the later resumable-publishing phase.

## Run checks

```powershell
python -m unittest discover -s tests -v
python -m unittest tests.test_preview_bridge -v
python -m unittest tests.test_studio tests.test_daily_set_import -v
python -m unittest tests.test_process_capture tests.test_capture_contract -v
python studio_server.py --self-test
python processor\process_capture.py --self-test
python processor\index_captures.py --root tests\fixtures\legacy-daily-sets --dry-run
gitleaks dir . --no-banner --redact
```

The secret scan excludes only the exact ignored local database, recorder
configuration, raw video, and generated-media paths. The repository safety
test separately prevents those paths from entering Git, while CI scans both
the current tree and each pushed commit range.

## Next milestones

The local data model and media contract are intentionally ready for a hosted
room service. Next work is the signed room-code relay, resumable uploads, and a
single-file Windows recorder build so recording helpers do not need this repo.
