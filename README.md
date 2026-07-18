# mainstage-concert-generator

Generate an Apple MainStage `.concert` bundle from a PDF keyboard score.

Target context: musical-theatre pit keyboards (Keyboard 1/2/3 books).
Goal: automate ~90% of the concert build so the ~10% left is fine-tuning of
unusual patches.

## What's in this repo

### Code

- **`generate_concert.py`** — the generator. Reads a cue list, a
  cue-to-SOUNDS mapping, and template blobs, and writes a valid
  MainStage `.concert` bundle. Reference-independent: no runtime
  dependency on any specific concert bundle beyond the user's SOUNDS
  bank.
- **`extract_template_blobs.py`** — one-time helper that pulls the
  opaque structural bits (root channel-strip `.cst` binaries, plist
  skeletons, workspace layout, alias-metadata bplists) out of a
  known-good reference concert and freezes them as `template_blobs.json`.
  Rerun when the template needs refreshing.
- **`sound_bank.py`** — scanner for user-supplied SOUND BANK folders
  (a folder tree of `Sampler Instruments/*.exs` + peer `Samples/`).
  Catalogs available EXS instruments and matches book-cue names against
  them via fuzzy substring + variant + alias lookup, preferring curated
  instruments over SoundFont-backup fallbacks.

### Data

- **`template_blobs.json`** — 13 base64-encoded blobs the generator
  needs: 4 root `.cst` binaries (Master, Metronome, Output 1-2, Reverb),
  the top-level `data.plist`, `base.plistZ`, `workspace.layout`,
  concert/set/leaf `data.plist` skeletons, and the three alias-metadata
  NSKeyedArchive bplists (`mappings`, `layer`, `metaInfo`). ~527 KB.
- **`common_names.json`** — persistent name-alias file mapping canonical
  book-cue names to a list of variations the scanner should treat as
  equivalent. Grow this over time as new shows surface new naming
  patterns.

### Example per-show input (Footloose Keyboard 2)

- **`cues.json`** — extracted patch cues per song, keyed by bar number,
  produced by the vision-based PDF extractor.
- **`mapping.json`** — cue-text → `(SOUNDS category, .cst filename)`.
  Currently references the Footloose K2 SOUNDS bank; each show gets its
  own mapping, potentially auto-generated in future.

## Usage

Point the generator at a folder that contains a SOUNDS bank concert and
run:

```
CONCERT_BUILDER_BASE=/path/to/workdir \
  SOUND_BANK_ROOTS="$HOME/Music/Audio Music Apps" \
  python3 generate_concert.py
```

The generator expects to find, under `$CONCERT_BUILDER_BASE/_generator/`,
`template_blobs.json`, `cues.json`, `mapping.json`, and (optionally)
`common_names.json`. It writes the resulting `.concert` bundle at
`$CONCERT_BUILDER_BASE/Footloose K2 GENERATED.concert` (currently
hardcoded — will parameterise).

`SOUND_BANK_ROOTS` is a colon-separated list of folders each containing
`Sampler Instruments/` and `Samples/`. The scanner reports which cues
have a curated match, which fall back to a SoundFont-backup instrument,
and which don't match at all.

## Design docs

The reverse-engineered `.concert` format, Roger's conventions, and the
PDF annotation model are captured in a Claude project (not this repo).

## Known follow-ups

- Currently the generator still copies channel strips from a reference
  SOUNDS.patch bundle. The next architectural step is synthesising
  channel-strip `.cst` files from matched EXS files, breaking the last
  dependency on a curated `.cst` library.
- `mapping.json` should be derivable from `common_names.json` + a SOUND
  BANK scan — currently hand-authored per show.
- Per-sound alias-metadata blobs (each alias currently reuses Hard Rock
  m8's `mappings` / `layer` / `metaInfo`).
