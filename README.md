# R6 Soundle Studio

R6 Soundle Studio is the authoring and capture workspace for the game. It keeps
recording, media processing, map coordination, set editing, and publishing out
of the public game repository.

The current milestone is a local-first studio:

- The Studio library separates three-round daily sets from one-round How to
  Play examples. Difficulty is not part of either model.
- How to Play examples include an authored guess ping; Studio displays its
  dashed result line and calculates distance and points with the game-owned
  scoring rules.
- All three rounds use the set's map, but each may use any floor.
- A runner end can optionally accept one additional floor for staircase landings
  that legitimately belong to either blueprint; guesses on either authored floor
  avoid the wrong-floor penalty.
- Listener evidence is stored as one JPEG from the beginning of the aligned
  POV plus an M4A audio track.
- The replay is runner video with the aligned listener audio.
- Production uploads publish a versioned challenge as soon as all media is verified.

## Start the studio

Python 3.11 or newer is recommended. Install the pinned timezone database once
so legacy release and preview-date calculations behave identically on Windows
and Unix:

```powershell
python -m pip install -r requirements.txt
```

`requirements.lock` is the Phase 9 clean-environment authority and currently
contains the same single, fully pinned runtime dependency.

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

Use the **Examples** button beside the new-set button to switch the sidebar to
one-round How to Play authoring. Examples share the map, operator, marker, and
processed-capture editor, but they cannot be uploaded through the production
publisher. Switch back with **Daily sets**.

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

The short capture names `casino`, `kafe`, `nighthaven`, and `theme` are
supported. Studio imports those map-set folders as Calypso Casino, Kafe
Dostoyevsky, Nighthaven Labs, and Theme Park respectively, while preserving the
short folder names and capture IDs. For example, `2-casino-1-listener.mp4` is
processed under `daily sets\2-casino` and imported with the game's canonical
`calypso-casino` map slug.

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
there is no separate capture-approval step. If changed content is already
attached to a published set, Studio requires an explicit stale acknowledgment
and advances the draft version so the existing production challenge remains
immutable.

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
Phase 5 added inert publish-attempt fields; Phase 6 now uses an additive SQLite
migration to persist the immutable request, remote attempt/release IDs,
per-object progress, retries, errors, and completion. Drafts and captures remain
SQLite-only owner state and are never imported automatically into production.

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
use unsupported contract versions.

## Upload a challenge to production

Studio loads `R6_STUDIO_PUBLISHER_URL` and `R6_STUDIO_PUBLISHER_TOKEN` from the
ignored repository-root `.env` file when it starts. Values may be unquoted:

```dotenv
R6_STUDIO_PUBLISHER_URL=https://r6soundle.com
R6_STUDIO_PUBLISHER_TOKEN=<publisher credential>
```

An explicitly set process environment variable or `--publisher-url` takes
precedence over `.env`. Studio only loads these two allowlisted keys and never
prints the token. Keep `.env` local and untracked.

For local simulation, start the Phase 6 game service on loopback and point
Studio at it:

```powershell
$env:R6_STUDIO_PUBLISHER_URL = "http://127.0.0.1:4190"
python studio_server.py
```

Choose an approved set in **Upload to production**, then select **Upload to
production**. Studio rechecks all capture sizes and SHA-256 values, negotiates
the publisher capability contract, uploads exactly the pending
still/audio/replay objects for all three rounds, asks the server to HEAD-verify
size/hash/MIME, and publishes the challenge immediately after all nine objects
pass. There is no release date or midnight wait. Closing or restarting Studio
is safe: the next action resumes the persisted remote attempt with fresh short
upload authorizations. A stale map-asset version blocks publication until the
set is reviewed and approved again. While the request runs, the dialog reports
media transfer counts, verification, and finalization separately. A failed
attempt remains visible with its saved error and whether all nine objects are
already verified, so retrying can resume instead of uploading them again.

Production assigns an internal compatibility slot and a stable challenge ID;
those details are not scheduling controls. More than one challenge can be
published on the same day, and existing dated release history remains readable.

The Phase 7 publisher bearer credential is required and read only from
`R6_STUDIO_PUBLISHER_TOKEN`; it is never written to `studio.db`, browser state,
logs, or an object-store request. Do not disable the Phase 7 bearer check or
expose local simulation outside loopback.

The game owns the legacy `contracts/publish-v1.schema.json` contract and the
immediate `contracts/publish-v2.schema.json` contract. Studio vendors both with
source hashes under `schemas/`. The publisher capability response must
advertise v2 immediate challenges before Studio creates or resumes a new
production upload.

The same dialog lists every challenge currently live in the configured
production service, including challenges uploaded by an older Studio database.
Select **Remove** and enter an audit reason to hide a challenge immediately.
Removal revokes public challenge and media access while retaining immutable
release and audit history; it does not hard-delete production records.

## Run checks

```powershell
python -m unittest discover -s tests -v
python -m unittest tests.test_preview_bridge -v
python -m unittest tests.test_studio tests.test_daily_set_import -v
python -m unittest tests.test_process_capture tests.test_capture_contract -v
python -m unittest tests.test_publish_client -v
python studio_server.py --self-test
python processor\process_capture.py --self-test
python processor\index_captures.py --root tests\fixtures\legacy-daily-sets --dry-run
gitleaks dir . --no-banner --redact
```

The Studio CI also checks out the private `R6-Soundle` contract authority. Add
an Actions secret named `R6_SOUNDLE_READ_TOKEN` in the Studio repository. It
must be a fine-grained personal access token or GitHub App token with
**Contents: Read-only** access to the `R6-Soundle` repository only. Do not use
the default `GITHUB_TOKEN`: it cannot read a separate private repository.

The secret scan excludes only the exact ignored local database, recorder
configuration, raw video, and generated-media paths. The repository safety
test separately prevents those paths from entering Git, while CI scans both
the current tree and each pushed commit range.

Phase 9 CI checks out the game contract authority as a sibling, installs both
locked dependency graphs, runs full Studio discovery and native self-tests,
checks the legacy fixture index, and repeats the in-process interrupted/resumed
publisher simulation with fake identities, media, filesystem storage, and a
non-secret test bearer token. It never opens `studio.db`, `daily sets`, `videos`,
`media/raw`, `media/processed`, `.env`, or `config.local.json` from an owner
workspace.

Feature branches that change a shared game contract must use the same branch
name in both repositories. Push the game branch first, then the Studio branch;
Studio CI checks out the matching game branch so contract drift and the
in-process publisher simulation validate the paired changes rather than the
game's default branch.

Phase 10's entry point is `tools/run_launch_rehearsal.py` in the sibling game
repository. `rehearsal_support.py` owns Studio's synthetic-only portion: it
creates named fake recordings under the caller's temporary root, drives the
real atomic processor through isolated media-tool shims, imports exactly three
rounds, authors fixture coordinates, approves a version, and opens an immutable
real-game preview. The processor's hidden `--failure-after` hook refuses to run
unless `R6_PROCESSOR_FAULT_INJECTION=YES`; it exists only for this isolated
rehearsal and its atomicity tests.

## Next milestones

The local data model and media contract support authenticated resumable release
publishing, and Phase 9 CI now exercises the Studio/game integration from clean
sibling checkouts. The next autonomous milestone is the isolated Phase 10
launch rehearsal; signed room-code relay and a single-file Windows recorder
remain deferred product work.
