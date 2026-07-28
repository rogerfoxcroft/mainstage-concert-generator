# mainstage-concert-generator

Generate an Apple MainStage `.concert` bundle from a PDF keyboard score.

Target context: musical-theatre pit keyboards (Keyboard 1/2/3 books).
Goal: automate ~90% of the concert build so the ~10% left is fine-tuning of
unusual patches.

## What's in this repo

### Code

- **`generate_concert.py`** — the generator. Reads a per-show cue list
  and optional mapping, resolves each sound against a user SOUND BANK,
  synthesizes any missing channel-strip `.cst` files, and writes a
  valid MainStage `.concert` bundle. Supports layered patches and
  two-zone keyboard splits at C3.
- **`sound_bank.py`** — scanner + matcher for user SOUND BANK folders
  (a tree of `Sampler Instruments/*.exs` + peer `Samples/`). Catalogs
  available EXS instruments and matches book-cue names via fuzzy
  substring + variant + alias lookup, preferring curated instruments
  over SoundFont-backup fallbacks. Also synthesizes new `.cst` files
  from a Sampler template by substituting EXS name and UUID.
- **`extract_template_blobs.py`** — one-time helper that pulls the
  opaque structural bits (root channel-strip `.cst` binaries, plist
  skeletons, workspace layout, alias-metadata bplists, Sampler `.cst`
  template) out of a known-good reference concert and freezes them
  as `template_blobs.json`. Rerun when the template needs refreshing.

### Data

- **`template_blobs.json`** — 15 base64-encoded blobs the generator
  needs: root channel-strip `.cst` binaries (Master, Metronome,
  Output 1-2, Reverb), the top-level `data.plist`, `base.plistZ`,
  `workspace.layout`, concert/set/leaf `data.plist` skeletons, the
  three alias-metadata NSKeyedArchive bplists (`mappings`, `layer`,
  `metaInfo`), and a Sampler-based `.cst` template + its source
  channel dict used to synthesize any EXS-backed source at run-time.
- **`common_names.json`** — persistent name-alias file. Maps each
  canonical book-cue name to (a) a list of variation names the SOUND
  BANK scanner should treat as equivalent, and (b) an instrument
  family (`_families` section) used for colour-coding and bucketing
  channels into `SOUNDS.patch/<Family>.patch/`. Grow this as new
  shows surface new naming patterns.

## Usage

The generator is show-parameterized. Set `SHOW` to the human-readable
show name; the tool reads `<SHOW>.cues.json` (and optionally
`<SHOW>.mapping.json`) from `$CONCERT_BUILDER_BASE/_generator/` and
writes `<SHOW> GENERATED.concert` alongside the source PDF.

```
CONCERT_BUILDER_BASE=/path/to/workdir \
  SHOW="Footloose K2" \
  SOUND_BANK_ROOTS="$HOME/Music/Audio Music Apps" \
  python3 _generator/generate_concert.py
```

Per-show input files (`<show>.cues.json`, `<show>.mapping.json`) live
alongside each show's PDF in the workdir — they are not tracked here
(see `.gitignore`).

`SOUND_BANK_ROOTS` is a colon-separated list of folders each containing
`Sampler Instruments/` and `Samples/`. The scanner reports which cues
have a curated match, which fall back to a SoundFont-backup instrument,
and which don't match at all.

Cue resolution order per sound: curated EXS match > mapping.json
override > SoundFont-backup EXS > placeholder (empty leaf).

## Layers and splits

Cues.json v2 supports layered and split patches:

```json
{"bar": 4,  "channel_name": "Slow Strings + Voices",
            "layers": ["Slow Strings", "Voices"],
            "zone": null, "split_note": null}

{"bar": 38, "channel_name": "Warm Pad + Synth Strings",
            "layers": ["Warm Pad", "Synth Strings"],
            "zone": "RH", "split_note": 60}
```

A layered patch becomes one aliased channel per sound, all across the
full keyboard. A zoned cue (`"RH"` or `"LH"`) constrains its channels
to notes above/below the split point (currently forced to C3 = MIDI
60, non-overlapping). Zones carry across bars — an RH cue at m38
followed by an LH cue at m41 emits an m41 patch with both zones active.

## Design docs

The reverse-engineered `.concert` format, Roger's conventions, and the
PDF annotation model are captured in a Claude project (not this repo).
