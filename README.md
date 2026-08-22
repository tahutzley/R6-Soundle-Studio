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

Python 3.11 or newer is recommended. No Python packages are required.

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
stored in `studio.db`; processed media is stored under `media\processed`.

## Process a two-POV capture

Install FFmpeg and ensure `ffmpeg` and `ffprobe` are on `PATH`, then run:

```powershell
python processor\process_capture.py `
  --listener path\listener.mp4 `
  --runner path\runner.mp4 `
  --capture-id capture-name
```

The processor estimates the offset from the audio envelopes unless
`--offset-ms` is supplied. It writes:

- `listener.jpg` — a single frame at the start of the aligned listener POV;
- `listener.m4a` — the listener audio used by the game;
- `replay.mp4` — the aligned runner POV with listener audio;
- `capture.json` — durations, offset, confidence, hashes, and source paths.

Raw inputs are preserved by default. After reviewing all outputs, pass
`--remove-raw` to remove the two source recordings.

## Run checks

```powershell
python -m unittest discover -s tests -v
python processor\process_capture.py --self-test
```

## Next milestones

The local data model and media contract are intentionally ready for a hosted
room service. Next work is the signed room-code relay, resumable uploads, and a
single-file Windows recorder build so recording helpers do not need this repo.

