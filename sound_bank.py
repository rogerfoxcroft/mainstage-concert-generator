#!/usr/bin/env python3
"""
Sound bank utilities: scan a SOUND BANK folder (Sampler Instruments + Samples
peers) for available .exs sampler instruments, and match book-cue names to
what's there using a persistent common-name-alias file.

A SOUND BANK folder looks like:
  <root>/
    Sampler Instruments/
      <instrument>.exs                       (may be nested — e.g. Autosampled/)
    Samples/
      <instrument>/
        *.wav | *.aif | *.caf

The .exs file references samples at ../Samples/<instrument>/.

Multiple SOUND BANK roots may live on the user's machine (typical default is
~/Music/Audio Music Apps/). The scanner accepts multiple roots.
"""
import os
import json
import re
import unicodedata
import uuid as uuidlib


SAMPLE_EXTS = (".wav", ".aif", ".aiff", ".caf")


def scan_sound_bank(root):
    """Catalog every .exs in root/Sampler Instruments/ (recursive).

    Returns a list of dicts:
      {
        "name": "Steinway Piano",              # bare .exs filename, no extension
        "exs_path": "/path/to/Steinway Piano.exs",
        "exs_relpath": "Autosampled/Steinway Piano.exs",   # relative to Sampler Instruments/
        "samples_dir": "/path/to/Samples/Steinway Piano",  # None if not present
        "sample_count": 128,                   # 0 if none found
        "root": "/path/to/AMA",                # which SOUND BANK root this came from
      }
    """
    catalog = []
    si_root = os.path.join(root, "Sampler Instruments")
    samples_root = os.path.join(root, "Samples")
    if not os.path.isdir(si_root):
        return catalog

    for dirpath, _dirs, files in os.walk(si_root):
        for f in files:
            if not f.lower().endswith(".exs"):
                continue
            name = f[:-4]
            exs_path = os.path.join(dirpath, f)
            exs_relpath = os.path.relpath(exs_path, si_root)
            samples_dir = os.path.join(samples_root, name)
            sample_count = 0
            if os.path.isdir(samples_dir):
                sample_count = sum(
                    1 for x in os.listdir(samples_dir)
                    if x.lower().endswith(SAMPLE_EXTS)
                )
            catalog.append({
                "name": name,
                "exs_path": exs_path,
                "exs_relpath": exs_relpath,
                "samples_dir": samples_dir if sample_count > 0 else None,
                "sample_count": sample_count,
                "root": root,
            })
    return catalog


def scan_sound_banks(roots):
    """Scan every root and return one combined catalog (later roots' entries
    with duplicate names come after earlier ones, so the first root wins on
    lookups)."""
    combined = []
    for r in roots:
        combined.extend(scan_sound_bank(r))
    return combined


# ----- Name normalization + matching ---------------------------------------

def _split_compound(s):
    """Split camelCase / PascalCase compounds into space-separated words
    so 'PianoTeq' → 'Piano Teq' and 'DrawBar' → 'Draw Bar'."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", s)
    return s


def _norm(s):
    """Lowercase, split compounds, strip punctuation except digits/spaces,
    collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _split_compound(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_sf_backup(entry):
    return ".sf2.bk" in entry.get("exs_relpath", "")


def _entry_base_priority(entry):
    """Bucket lower number = better. Curated > SoundFont backup. Sample count
    is not a discriminator — many valid Auto-Sampled .exs source from
    plugin libraries rather than a peer Samples/<name>/ folder."""
    return 1 if _is_sf_backup(entry) else 0


def build_name_index(catalog):
    """Return {normalized_name: [catalog_entry sorted best-first]}.

    Every entry is indexed under all of its name variants, so that a cue
    matching e.g. 'Solo Cello' still resolves to 'EWSO - Solo Cello (No Rev)'
    at the exact-match step, ahead of any SoundFont-backup entry named just
    'Cello'."""
    idx = {}
    for entry in catalog:
        seen = set()
        for variant in _entry_name_variants(entry["name"]):
            nv = _norm(variant)
            if nv in seen:
                continue
            seen.add(nv)
            idx.setdefault(nv, []).append(entry)
    for k in idx:
        idx[k].sort(key=lambda e: (_entry_base_priority(e), len(e.get("exs_relpath", ""))))
    return idx


def _candidates_for(cue_name, aliases):
    """Expand a cue name to the list of candidate names to search for,
    starting from the most specific."""
    out = [cue_name]
    if cue_name in aliases:
        out.extend(aliases[cue_name])
    else:
        for canonical, variations in aliases.items():
            if cue_name in variations:
                out.append(canonical)
                out.extend(variations)
                break
    # dedupe preserving order
    seen = set()
    result = []
    for c in out:
        n = _norm(c)
        if n and n not in seen:
            seen.add(n)
            result.append(c)
    return result


def _entry_name_variants(entry_name):
    """Generate alternative forms of a catalog entry's name so that vendor
    prefixes and common decorations don't hide the underlying instrument.

    Applies transformations iteratively until no new variants appear —
    e.g. 'EWSO - Solo Cello (No Rev)' → { full, 'Solo Cello (No Rev)',
    'EWSO - Solo Cello', 'Solo Cello' }."""
    def transforms(name):
        outs = set()
        # 1) After first " - "
        if " - " in name:
            outs.add(name.split(" - ", 1)[1])
        # 2) Split on plain dashes, keep last segment
        parts = [p.strip() for p in name.split("-") if p.strip()]
        if len(parts) > 1:
            outs.add(parts[-1])
        # 3) Strip decorations
        for dec in ("(No Rev)", "(No Reverb)", "(One Mic)"):
            if dec in name:
                outs.add(name.replace(dec, "").strip())
        # 4) Trailing + (e.g. Full Brass+)
        if name.endswith("+"):
            outs.add(name.rstrip("+").strip())
        return outs

    variants = {entry_name}
    frontier = {entry_name}
    while frontier:
        new_frontier = set()
        for v in frontier:
            for t in transforms(v):
                if t and t not in variants:
                    variants.add(t)
                    new_frontier.add(t)
        frontier = new_frontier
    return variants


def _match_score(candidate_norm, entry_norm):
    """Score how well an entry name matches a candidate name.
    Higher = better match. 0 means no match."""
    if candidate_norm == entry_norm:
        return 100  # exact
    words = entry_norm.split()
    if candidate_norm in words:
        return 80
    if entry_norm.endswith(" " + candidate_norm) or entry_norm.endswith("-" + candidate_norm):
        return 70
    if entry_norm.startswith(candidate_norm + " "):
        return 60
    if candidate_norm in entry_norm:
        return 30 + min(20, len(candidate_norm))
    if entry_norm in candidate_norm and len(entry_norm) >= 4:
        return 25
    return 0


def _score_entry(entry, ncands):
    """Best score across all name variants of an entry against all
    candidate cue names."""
    best = 0
    for variant in _entry_name_variants(entry["name"]):
        vnorm = _norm(variant)
        s = max((_match_score(nc, vnorm) for nc in ncands), default=0)
        if s > best:
            best = s
    return best


def match_cue(cue_name, name_index, aliases, catalog=None):
    """Try to match a book cue to a catalog entry.

    Two-phase:
    1. Fast exact match against the pre-built name_index for the cue and
       each alias variation.
    2. If nothing exact, fuzzy match: score every catalog entry against
       every candidate name, return the highest-scoring entry (with
       curated-bank tiebreaker via _entry_base_priority)."""
    candidates = _candidates_for(cue_name, aliases)
    ncands = [_norm(c) for c in candidates]

    # 1) Exact match — collect hits from ALL candidates, then prefer curated.
    # Otherwise the first candidate to hit wins even if a later candidate
    # would have found a curated entry.
    exact_hits = []
    seen_ids = set()
    for nc in ncands:
        for e in name_index.get(nc, []):
            key = id(e)
            if key not in seen_ids:
                seen_ids.add(key)
                exact_hits.append(e)
    if exact_hits:
        exact_hits.sort(key=lambda e: (_entry_base_priority(e), len(e.get("exs_relpath", ""))))
        return exact_hits[0]

    # 2) Fuzzy scoring — score curated first; only fall back to SoundFonts
    # if no curated hit reaches the threshold.
    if catalog is None:
        return None

    CURATED_THRESHOLD = 40   # anything with a plausible substring / suffix
    SF_THRESHOLD     = 30    # SoundFont fallback only from a real hit

    best_curated, curated_score = None, 0
    best_sf, sf_score = None, 0
    for entry in catalog:
        s = _score_entry(entry, ncands)
        if s == 0:
            continue
        if _is_sf_backup(entry):
            if s > sf_score:
                sf_score, best_sf = s, entry
        else:
            if s > curated_score:
                curated_score, best_curated = s, entry

    if best_curated and curated_score >= CURATED_THRESHOLD:
        return best_curated
    if best_sf and sf_score >= SF_THRESHOLD:
        return best_sf
    if best_curated and curated_score >= 25:
        return best_curated
    return None


# ----- Report --------------------------------------------------------------

def report(catalog, cues, aliases):
    """Return a text report showing matches / non-matches for a cue list."""
    name_index = build_name_index(catalog)
    n_curated = sum(1 for e in catalog if not _is_sf_backup(e))
    lines = []
    lines.append(f"SOUND BANK catalog: {len(catalog)} .exs instruments discovered")
    lines.append(f"  of which curated (not SoundFont-backup):  {n_curated}")
    lines.append(f"Book cues to match: {len(cues)}")
    lines.append("")
    matched, unmatched = 0, 0
    n_curated_match = 0
    for cue in cues:
        hit = match_cue(cue, name_index, aliases, catalog)
        if hit:
            is_backup = _is_sf_backup(hit)
            marker = "?" if is_backup else "✓"
            if not is_backup:
                n_curated_match += 1
            lines.append(f"  {marker} {cue!r:<32} → {hit['name']!r}  ({hit['exs_relpath']})")
            matched += 1
        else:
            lines.append(f"  ✗ {cue!r:<32} → NO MATCH")
            unmatched += 1
    lines.append("")
    lines.append(f"Legend: ✓ curated match · ? SoundFont-backup fallback · ✗ no match")
    lines.append(f"Matched: {matched} / {len(cues)} ({100*matched/max(len(cues),1):.0f}%)")
    lines.append(f"  curated matches:            {n_curated_match} / {len(cues)}")
    lines.append(f"  fell back to SoundFont:     {matched - n_curated_match} / {len(cues)}")
    return "\n".join(lines)


# ----- .cst synthesis -------------------------------------------------------
#
# A Sampler-based .cst file contains the loaded EXS filename in two
# null-padded fields — one in a config chunk near the top and one inside a
# `MELCPMAS1MAS` chunk — plus a single UUID field embedded in an
# NSKeyedArchive blob preceded by the marker `UUIDBytes\x80\x04\x4f\x10\x10`.
#
# Both substitutions preserve total file size (fields are null-padded to a
# fixed length, and the UUID field is exactly 16 bytes).

_UUID_MARKER = b"UUIDBytes\x80\x04\x4f\x10\x10"


def _substitute_exs_in_cst(cst_bytes, old_exs_stem, new_exs_stem):
    """Replace every occurrence of `<old_stem>.exs` with `<new_stem>.exs`,
    keeping each null-padded field's length constant."""
    old = (old_exs_stem + ".exs").encode("utf-8")
    new = (new_exs_stem + ".exs").encode("utf-8")
    out = bytearray(cst_bytes)
    pos = 0
    while True:
        i = out.find(old + b"\x00", pos)
        if i < 0:
            break
        end = i + len(old)
        while end < len(out) and out[end] == 0:
            end += 1
        field_size = end - i
        if len(new) + 1 > field_size:
            raise ValueError(
                f"New EXS name '{new_exs_stem}.exs' ({len(new)+1} bytes) does "
                f"not fit in .cst field at 0x{i:x} ({field_size} bytes)"
            )
        for j in range(i, end):
            out[j] = 0
        out[i : i + len(new)] = new
        pos = i + len(new) + 1
    return bytes(out)


def _substitute_uuid_in_cst(cst_bytes, new_uuid_str):
    """Replace the single embedded UUID (found via `UUIDBytes` marker) with
    the 16 bytes of `new_uuid_str`."""
    out = bytearray(cst_bytes)
    i = out.find(_UUID_MARKER)
    if i < 0:
        raise ValueError("UUID marker not found in .cst template")
    offset = i + len(_UUID_MARKER)
    out[offset : offset + 16] = uuidlib.UUID(new_uuid_str).bytes
    return bytes(out)


def synthesize_cst(template_bytes, template_original_exs_stem,
                   target_exs_stem, target_uuid_str):
    """Turn a Sampler-based .cst template into a new .cst that loads
    `<target_exs_stem>.exs` with the identity `target_uuid_str`.

    - `template_bytes` — bytes of a known-good Sampler-based .cst
      (currently: Roger's Harp.cst).
    - `template_original_exs_stem` — the .exs stem the template
      currently references (e.g. "Harp"). Both null-padded field slots
      containing this name get overwritten.
    - `target_exs_stem` — the .exs stem to load (must exist in a folder
      Sampler will search — the user's SOUND BANK roots).
    - `target_uuid_str` — the UUID to embed. Must match whatever the
      generator uses in the outer data.plist channel dict for aliasing
      to work.

    File size is preserved."""
    out = _substitute_exs_in_cst(template_bytes, template_original_exs_stem,
                                 target_exs_stem)
    out = _substitute_uuid_in_cst(out, target_uuid_str)
    return out


# ----- CLI ------------------------------------------------------------------

def _default_roots():
    """Reasonable defaults for macOS. Users may add more via CLI arg."""
    home = os.path.expanduser("~")
    return [os.path.join(home, "Music", "Audio Music Apps")]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--roots", nargs="*", default=_default_roots(),
                   help="SOUND BANK root folders (each containing Sampler "
                        "Instruments/ and Samples/). Default: ~/Music/Audio Music Apps")
    p.add_argument("--aliases", default=None,
                   help="Path to common_names.json (cue → variations map)")
    p.add_argument("--cues", default=None,
                   help="Optional: path to a JSON list of cue names to try to match")
    args = p.parse_args()

    print(f"Scanning SOUND BANK roots:")
    for r in args.roots:
        marker = "✓" if os.path.isdir(os.path.join(r, "Sampler Instruments")) else "✗"
        print(f"  {marker} {r}")

    catalog = scan_sound_banks(args.roots)
    print(f"\nFound {len(catalog)} .exs instruments")

    aliases = {}
    if args.aliases and os.path.exists(args.aliases):
        with open(args.aliases) as f:
            aliases = json.load(f)
        print(f"Loaded {len(aliases)} canonical name entries from {args.aliases}")

    if args.cues:
        with open(args.cues) as f:
            cues = json.load(f)
        # If cues is the structured songs JSON, flatten to a set of cue channel_names
        if isinstance(cues, list) and cues and isinstance(cues[0], dict) and "cues" in cues[0]:
            cue_names = sorted({c["channel_name"] for s in cues for c in s.get("cues", [])})
        else:
            cue_names = cues
        print()
        print(report(catalog, cue_names, aliases))
    else:
        # Just list the catalog
        print()
        for entry in sorted(catalog, key=lambda e: e["name"].lower()):
            marker = "✓" if entry["sample_count"] > 0 else "?"
            print(f"  {marker} {entry['name']}  ({entry['sample_count']} samples)")
