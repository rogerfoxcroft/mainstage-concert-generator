#!/usr/bin/env python3
"""
Generate a MainStage .concert bundle from:
- an extracted cue list (per song, ordered by bar)
- a cue → SOUNDS-bank mapping (optional, per-show overrides)
- template_blobs.json (opaque bits + a Sampler-based .cst template)
- a user SOUND BANK (folders holding Sampler Instruments/*.exs + Samples/)
- optionally, a legacy SOUNDS bank concert bundle (for cues resolved via
  explicit mapping.json entries)

Cue resolution tiers, most-preferred first:
  1. Curated EXS match — SOUND BANK gives a real, not-SoundFont hit.
  2. Explicit mapping.json entry that resolves in the legacy SOUNDS bank.
  3. SoundFont-backup EXS match.
  4. Placeholder.

Per-cue shape (v2 — supports layers + splits):
  {
    "bar": 40,
    "channel_name": "Warm Pad + Synth Strings",   # book-mirror display
    "layers": ["Warm Pad", "Synth Strings"],       # one entry per sound
    "zone": "RH" | "LH" | null,                    # null = full keyboard
    "split_note": 60 | null                        # C3 for a two-zone split
  }

State walker: within each song, walking cues in bar order, maintains
current_full / current_rh / current_lh. Each cue REPLACES its zone's
sounds; the other zones carry over. A patch is emitted at every bar
with any cue; the leaf contains one aliased channel per sound in each
active zone, with layer-bplist zone constraints applied.
"""

import plistlib
import os
import shutil
import sys
import json
import base64
import uuid as uuidlib
import copy

# ---- Paths + per-show configuration ---------------------------------------
BASE = os.environ.get(
    "CONCERT_BUILDER_BASE",
    "/Users/roger/Music/Mainstage Concert Builder",
)
# SHOW is the human-readable show name. It's the basis for both the cues
# file(s) the generator reads and the output concert bundle's filename.
SHOW = os.environ.get("SHOW", "Footloose K2")

TEMPLATE_BLOBS_JSON = f"{BASE}/_generator/template_blobs.json"
COMMON_NAMES_JSON   = f"{BASE}/_generator/common_names.json"

def _resolve_show_file(env_var, default_basename):
    """Look for the requested per-show file. Env-var override wins.
    Otherwise try `<SHOW>.<default_basename>` first, then the legacy
    plain-name `<default_basename>` in the _generator folder."""
    p = os.environ.get(env_var)
    if p:
        return p
    for candidate in (
        f"{BASE}/_generator/{SHOW}.{default_basename}",
        f"{BASE}/_generator/{default_basename}",
    ):
        if os.path.exists(candidate):
            return candidate
    # Return the show-prefixed path anyway so the missing-file error is
    # clearly per-show rather than pointing at the legacy shared file.
    return f"{BASE}/_generator/{SHOW}.{default_basename}"

CUES_JSON    = _resolve_show_file("CUES_FILE",    "cues.json")
MAPPING_JSON = _resolve_show_file("MAPPING_FILE", "mapping.json")

# Legacy SOUNDS bank concert bundle — used for cues resolved via explicit
# mapping.json entries. Defaults to `<SHOW>.concert` alongside the output;
# empty string forces EXS-only resolution (no map tier).
SOUNDS_BANK_CONCERT = os.environ.get(
    "SOUNDS_BANK_CONCERT",
    f"{BASE}/{SHOW}.concert",
)

# SOUND BANK roots — folders each containing Sampler Instruments/ and
# Samples/. Overridable via SOUND_BANK_ROOTS env var (colon-separated).
_default_roots = [os.path.expanduser("~/Music/Audio Music Apps")]
_env_roots = os.environ.get("SOUND_BANK_ROOTS", "")
SOUND_BANK_ROOTS = [p for p in _env_roots.split(":") if p] or _default_roots

OUTPUT_CONCERT = os.environ.get(
    "OUTPUT_CONCERT",
    f"{BASE}/{SHOW} GENERATED.concert",
)

# Per Roger's conventions
GENERIC_ICON = 4505
# What the Sampler template .cst currently references (the .exs stem to swap
# out when synthesizing). Kept in sync with what extract_template_blobs.py
# packages as `sampler_template_cst`.
SAMPLER_TEMPLATE_EXS_STEM = "Harp"

# ---- Instrument-family colours + bucketing --------------------------------
# Each Source channel (and every alias derived from it) is coloured by the
# instrument family it belongs to, and lives in SOUNDS.patch/<Family>.patch.
# Roger asked for "a different colour for each family" — the exact hues
# don't matter, they just have to be distinct.
FAMILY_COLORS = {
    "Keyboards":  (0.87, 0.24, 0.29),  # red
    "Strings":    (0.20, 0.44, 0.80),  # blue
    "Brass":      (0.95, 0.60, 0.20),  # orange
    "Woodwinds":  (0.30, 0.68, 0.35),  # green
    "Guitars":    (0.90, 0.72, 0.20),  # yellow-gold
    "Synths":     (0.60, 0.30, 0.80),  # purple
    "Percussion": (0.20, 0.65, 0.65),  # teal
    "Voices":     (0.95, 0.55, 0.75),  # pink
    "Other":      (0.50, 0.50, 0.50),  # neutral grey
}
FAMILY_SEQ_INDEX = {
    "Keyboards":  3,
    "Strings":    5,
    "Brass":      2,
    "Woodwinds":  4,
    "Guitars":    1,
    "Synths":     7,
    "Percussion": 6,
    "Voices":     10,
    "Other":      0,
}
FAMILY_ORDER = ["Keyboards", "Strings", "Brass", "Woodwinds", "Guitars",
                "Synths", "Percussion", "Voices", "Other"]


def build_color_bplist(r, g, b):
    """Encode an (r, g, b) triple (each 0.0-1.0) as an NSKeyedArchive
    NSColor bplist — the same shape MainStage stores in a channel's
    `color` field. Format is a small archive whose $objects[1] holds
    an NSRGB ASCII-triple like b'0.87 0.24 0.29\\x00'."""
    rgb = f"{r} {g} {b}".encode("ascii") + b"\x00"
    archive = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            {"NSRGB": rgb, "NSColorSpace": 1, "$class": plistlib.UID(2)},
            {"$classname": "NSColor", "$classes": ["NSColor", "NSObject"]},
        ],
    }
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


_family_color_cache = {}
def family_color_bplist(family):
    if family not in _family_color_cache:
        rgb = FAMILY_COLORS.get(family, FAMILY_COLORS["Other"])
        _family_color_cache[family] = build_color_bplist(*rgb)
    return _family_color_cache[family]


def apply_family_color(ch, family):
    """Overwrite `color` + `Channel_seqColorIndex` on a channel dict so
    it displays in the family's palette. Applied on both SOURCE channels
    (SOUNDS.patch entries) and every ALIAS in song patches — so families
    read consistently across the whole concert."""
    ch["color"] = family_color_bplist(family)
    ch["Channel_seqColorIndex"] = FAMILY_SEQ_INDEX.get(family, 0)
    return ch


# Family-signaling keywords used as a last-resort fallback in get_family.
# Any token in the sound name that matches a keyword here classifies the
# sound into that family. Ordered by check order isn't meaningful — first
# matching token wins (sounds with e.g. 'synth strings' pick up Synths
# not Strings because 'synth' appears first in the token list).
FAMILY_KEYWORDS = {
    # Keyboards
    "piano": "Keyboards", "rhodes": "Keyboards", "clavinet": "Keyboards",
    "clav": "Keyboards", "wurlitzer": "Keyboards", "harpsichord": "Keyboards",
    "celesta": "Keyboards", "organ": "Keyboards", "b3": "Keyboards",
    # Strings
    "strings": "Strings", "string": "Strings", "cello": "Strings",
    "celli": "Strings", "violin": "Strings", "harp": "Strings",
    "arco": "Strings", "tremolo": "Strings",
    # Brass
    "horn": "Brass", "horns": "Brass", "trumpet": "Brass",
    "trumpets": "Brass", "trombone": "Brass", "trombones": "Brass",
    "tuba": "Brass", "brass": "Brass",
    # Woodwinds
    "sax": "Woodwinds", "saxes": "Woodwinds", "saxophone": "Woodwinds",
    "clarinet": "Woodwinds", "flute": "Woodwinds", "oboe": "Woodwinds",
    "accordion": "Woodwinds",
    # Guitars
    "guitar": "Guitars", "strat": "Guitars", "gtr": "Guitars",
    # Synths
    "synth": "Synths", "pad": "Synths", "lead": "Synths", "fx": "Synths",
    # Percussion
    "marimba": "Percussion", "vibes": "Percussion", "vibraphone": "Percussion",
    "xylophone": "Percussion", "kalimba": "Percussion", "bells": "Percussion",
    "bell": "Percussion", "timpani": "Percussion", "shaker": "Percussion",
    # Voices
    "voice": "Voices", "voices": "Voices", "choir": "Voices",
    "vocals": "Voices", "aahs": "Voices", "oohs": "Voices",
}
# Iteration order tuned so more-specific tokens win over generic ones —
# 'synth strings' hits 'synth' (Synths) before it would hit 'strings'
# (Strings). This is the practical intent.
_FAMILY_KEYWORD_PRIORITY = [
    "synth", "pad", "lead", "fx",
    "piano", "rhodes", "clavinet", "clav", "wurlitzer", "harpsichord",
    "celesta", "organ", "b3",
    "sax", "saxes", "saxophone", "clarinet", "flute", "oboe", "accordion",
    "guitar", "strat", "gtr",
    "horn", "horns", "trumpet", "trumpets", "trombone", "trombones",
    "tuba", "brass",
    "arco", "tremolo", "cello", "celli", "violin", "harp",
    "strings", "string",
    "marimba", "vibes", "vibraphone", "xylophone", "kalimba", "bells",
    "bell", "timpani", "shaker",
    "voice", "voices", "choir", "vocals", "aahs", "oohs",
]


def get_family(sound_name, families_map, common_names_aliases):
    """Determine the instrument family for a sound name.
    - Direct hit in `_families` map wins.
    - Otherwise search common_names_aliases for a canonical name that
      lists this sound as a variant, then look up that canonical's family.
    - Otherwise tokenize (lower-cased, transposition stripped) and match
      any family keyword — 'Horn 8vb' → Brass via 'horn'.
    - Fall through to 'Other'.
    """
    if sound_name in families_map:
        return families_map[sound_name]
    for canonical, variants in common_names_aliases.items():
        if sound_name in variants:
            return families_map.get(canonical, "Other")
    # Tokenize: lowercase, split on non-alnum, strip transposition tokens.
    import re as _re
    tokens = _re.findall(r"[a-z0-9-]+", sound_name.lower())
    tokens = [t for t in tokens if t not in ("8va", "8vb", "15ma", "15mb", "loco")]
    token_set = set(tokens)
    for kw in _FAMILY_KEYWORD_PRIORITY:
        if kw in token_set:
            return FAMILY_KEYWORDS[kw]
    return "Other"


# ---- Helpers ---------------------------------------------------------------
def load_blobs():
    with open(TEMPLATE_BLOBS_JSON) as f:
        raw = json.load(f)
    return {k: base64.b64decode(v) for k, v in raw.items()}


def write_plist(path, obj):
    with open(path, "wb") as f:
        plistlib.dump(obj, f, fmt=plistlib.FMT_BINARY)


def write_bytes(path, b):
    with open(path, "wb") as f:
        f.write(b)


def load_sounds_inventory():
    """Read the legacy SOUNDS bank — SOUNDS.patch/<Category>.patch/data.plist
    from SOUNDS_BANK_CONCERT — and return {(category, filename) → channel-dict}."""
    inventory = {}
    if not SOUNDS_BANK_CONCERT or not os.path.isdir(SOUNDS_BANK_CONCERT):
        return inventory
    sounds_root = f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch"
    for cat in ["Keyboards", "Strings", "Brass", "Woodwinds", "Guitars", "Synths", "Percussion"]:
        pl_path = f"{sounds_root}/{cat}.patch/data.plist"
        if not os.path.exists(pl_path):
            continue
        with open(pl_path, "rb") as f:
            pl = plistlib.loads(f.read())
        for ch in pl.get("channels", []):
            inventory[(cat, ch["Filename"])] = ch
    return inventory


def build_zoned_layer_bplist(orig_layer_bytes, low_note, high_note):
    """Rewrite an alias's `layer` NSKeyedArchive so the alias is
    constrained to a [low_note, high_note] MIDI range with
    overrideParentsKeyZone=True.

    None on both args (or a full-range 0..127 pair) returns the archive
    unchanged, so full-keyboard aliases keep the exact byte-identical
    layer blob shipped in template_blobs.json.
    """
    if (low_note is None and high_note is None) or \
       (low_note == 0 and high_note == 127):
        return orig_layer_bytes
    layer_pl = plistlib.loads(orig_layer_bytes)
    root = layer_pl["$objects"][1]
    root["lowNote"] = 0 if low_note is None else low_note
    root["highNote"] = 127 if high_note is None else high_note
    root["overrideParentsKeyZone"] = True
    return plistlib.dumps(layer_pl, fmt=plistlib.FMT_BINARY)


def apply_conventions_to_channel(src_ch, display_name, output_index,
                                 alias_blobs, zone_bplist=None):
    """Turn a SOUNDS-bank channel dict into a song-patch alias.

    `zone_bplist`, when given (already-zoned via build_zoned_layer_bplist),
    replaces the default alias layer archive so this alias only responds
    to notes in its zone. Pass None for a full-keyboard alias.
    """
    new_ch = copy.deepcopy(src_ch)
    new_ch["Channel_name"] = display_name
    new_ch["Custom_name"] = True
    new_ch["Track_icon"] = GENERIC_ICON
    new_ch["Custom_icon"] = GENERIC_ICON
    new_ch["Channel_outputIndex"] = output_index
    # UUID + instID inherit from src_ch — this is the aliasing key
    new_ch["isAlias"] = True
    new_ch["aliasUUID"] = new_ch["UUID"]
    new_ch["mappings"] = alias_blobs["mappings"]
    new_ch["layer"] = zone_bplist if zone_bplist is not None else alias_blobs["layer"]
    new_ch["metaInfo"] = alias_blobs["metaInfo"]
    # Manual preset display
    new_ch["Channel_chaStrName"] = None
    new_ch["Channel_chaStrFullPath"] = None
    new_ch["Channel_chaStrCategory"] = ""
    return new_ch


# Roger's simplification for now: every split lives at C3 (MIDI 60),
# non-overlapping. RH = [60..127], LH = [0..59].
SPLIT_MIDI = 60
ZONE_RANGES = {
    "RH": (SPLIT_MIDI, 127),
    "LH": (0, SPLIT_MIDI - 1),
    None: (None, None),
}


def cue_sounds(cue):
    """Return the list of sound names a cue introduces on its zone.
    Layers is authoritative when present; otherwise fall back to
    treating channel_name as a single-layer sound (backward-compat
    with cues.json v1 like Footloose K2's)."""
    return list(cue.get("layers") or [cue["channel_name"]])


def collect_sound_names(songs):
    """Every distinct sound name across all cues in all songs — the set
    of names we need to resolve to a source (map/exs/placeholder)."""
    names = set()
    for s in songs:
        for c in s.get("cues", []):
            for sound in cue_sounds(c):
                names.add(sound)
    return sorted(names)


def build_auto_source_channel(sampler_channel_template, exs_stem, new_uuid,
                              new_instid):
    """Build a SOUNDS-bank-style channel dict for an Auto entry, starting
    from the template channel dict (Harp's, extracted at blob time) so
    the color blob and standard routing defaults come along.

    IMPORTANT: on a SOURCE channel (as opposed to a song-patch alias),
    MainStage calls -UTF8String on Channel_chaStrName/FullPath during
    document load — if these are NSNull it crashes hard. So we leave the
    Harp-template's channel-strip name/path/category untouched here.
    They're arbitrary display strings on the source; the alias copies in
    song patches will still show as 'Manual' because apply_conventions_
    to_channel nulls them out on the alias (which IS tolerated)."""
    ch = copy.deepcopy(sampler_channel_template)
    ch["Filename"] = f"{exs_stem}.cst"
    ch["Channel_name"] = exs_stem
    ch["UUID"] = new_uuid
    ch["Channel_instID"] = new_instid
    ch["Custom_name"] = True
    ch["Custom_icon"] = GENERIC_ICON
    ch["Track_icon"] = GENERIC_ICON
    return ch


def make_leaf_patch_dict(patch_name, channels, template_bytes):
    d = plistlib.loads(template_bytes)
    d["channels"] = channels
    d["patch"]["iconID"] = GENERIC_ICON
    d["patch"]["expanded"] = True
    d["patch"]["selected"] = False
    en = d["patch"]["engineNode"]
    en["name"] = patch_name
    en["hasProgramChange"] = False
    en["patchChangeNum"] = 0
    en["hasBankSelect"] = False
    en["bankSelectNumber"] = 0
    en["disabled"] = False
    en["hasFolderColor"] = False
    d["patch"]["patchMappings"] = {}
    d["patch"]["virtualMappings"] = {}
    return d


def make_set_patch_dict(set_name, child_names, template_bytes):
    d = plistlib.loads(template_bytes)
    d["nodes"] = list(child_names)
    d["channels"] = []
    d["patch"]["iconID"] = GENERIC_ICON
    d["patch"]["expanded"] = True
    d["patch"]["selected"] = False
    en = d["patch"]["engineNode"]
    en["name"] = set_name
    en["hasProgramChange"] = False
    en["patchChangeNum"] = 0
    en["disabled"] = False
    return d


def resolve_sounds(songs, mapping, inventory, catalog, aliases, sound_bank_mod):
    """Return {sound_name: source_spec}, where source_spec is one of:
      {"tier": "map", "cat": ..., "fn": ...}
      {"tier": "exs", "exs_stem": ...}
      {"tier": "placeholder"}

    Keyed by SOUND (individual layer) name — not by cue-channel name —
    so a layered cue like "Warm Pad + Synth Strings" contributes two
    separate lookups. Each layer becomes its own aliased channel.

    Priority (best first):
      1. Curated EXS match  — SOUND BANK gives a real, not-SoundFont hit.
      2. Explicit mapping.json entry that resolves in the legacy SOUNDS bank.
      3. SoundFont-backup EXS match — better than nothing.
      4. Placeholder.
    """
    all_sounds = collect_sound_names(songs)
    name_index = (sound_bank_mod.build_name_index(catalog)
                  if catalog and sound_bank_mod else None)
    resolved = {}
    for sound in all_sounds:
        exs_hit = (sound_bank_mod.match_cue(sound, name_index, aliases or {}, catalog)
                   if name_index is not None else None)
        curated_exs = exs_hit if exs_hit and not sound_bank_mod._is_sf_backup(exs_hit) else None
        sf_exs = exs_hit if exs_hit and sound_bank_mod._is_sf_backup(exs_hit) else None

        if curated_exs:
            resolved[sound] = {"tier": "exs", "exs_stem": curated_exs["name"]}
            continue
        if sound in mapping:
            cat, fn = mapping[sound]
            if (cat, fn) in inventory:
                resolved[sound] = {"tier": "map", "cat": cat, "fn": fn}
                continue
        if sf_exs:
            resolved[sound] = {"tier": "exs", "exs_stem": sf_exs["name"]}
            continue
        resolved[sound] = {"tier": "placeholder"}
    return resolved


def build_alias_channel(sound_name, zone, sound_source, family, inventory,
                        exs_needed, sampler_channel_template, alias_blobs,
                        m_folder, sounds_bank_concert):
    """Emit a fully-configured alias channel dict for one sound in one
    zone within a leaf patch. Also copies the source .cst into the leaf
    folder (map-tier) or writes the synthesized .cst there (exs-tier).

    Family colour is applied to the returned alias, so families read
    consistently across every patch that uses them.

    Returns (channel_dict, source_kind) where source_kind is 'map',
    'exs', or 'placeholder'. Placeholder returns (None, 'placeholder').
    """
    zone_low, zone_high = ZONE_RANGES[zone]
    zone_bplist = build_zoned_layer_bplist(alias_blobs["layer"], zone_low, zone_high)

    if sound_source["tier"] == "map":
        cat, fn = sound_source["cat"], sound_source["fn"]
        src_ch = inventory[(cat, fn)]
        src_cst = f"{sounds_bank_concert}/Concert.patch/SOUNDS.patch/{cat}.patch/{fn}"
        dst = f"{m_folder}/{fn}"
        if not os.path.exists(dst):
            shutil.copy(src_cst, dst)
        alias = apply_conventions_to_channel(
            src_ch, display_name=sound_name, output_index=0,
            alias_blobs=alias_blobs, zone_bplist=zone_bplist,
        )
        return apply_family_color(alias, family), "map"

    if sound_source["tier"] == "exs":
        stem = sound_source["exs_stem"]
        entry = exs_needed[stem]
        filename = f"{stem}.cst"
        dst = f"{m_folder}/{filename}"
        if not os.path.exists(dst):
            write_bytes(dst, entry["cst"])
        src_ch = build_auto_source_channel(
            sampler_channel_template, stem, entry["uuid"], entry["instid"],
        )
        alias = apply_conventions_to_channel(
            src_ch, display_name=sound_name, output_index=0,
            alias_blobs=alias_blobs, zone_bplist=zone_bplist,
        )
        return apply_family_color(alias, family), "exs"

    return None, "placeholder"


def walk_song_state(cues):
    """Iterator: yields (bar, state_channels) for each bar with any cue
    change, where state_channels is a list of (sound_name, zone) tuples
    representing the leaf patch's channels at that bar.

    State model:
      full : list[str] | None   # sounds active across the whole keyboard
      rh   : list[str] | None   # sounds active above the split (zoned RH)
      lh   : list[str] | None   # sounds active below the split (zoned LH)
    Full and (rh|lh) are mutually exclusive — a full cue clears rh/lh,
    and a zoned cue clears full. Zones carry across bars until replaced.
    """
    state_full = None
    state_rh = None
    state_lh = None

    by_bar = {}
    for c in cues:
        by_bar.setdefault(c["bar"], []).append(c)

    def _bar_sort_key(b):
        return (int(b), "") if isinstance(b, int) else (
            (int("".join(ch for ch in b if ch.isdigit()) or "0"),
             "".join(ch for ch in b if ch.isalpha()))
        )

    for bar in sorted(by_bar, key=_bar_sort_key):
        for cue in by_bar[bar]:
            zone = cue.get("zone")
            layers = cue_sounds(cue)
            if zone is None:
                state_full = list(layers)
                state_rh = None
                state_lh = None
            elif zone == "RH":
                state_full = None
                state_rh = list(layers)
                # LH carries over from the previous split cycle
            elif zone == "LH":
                state_full = None
                state_lh = list(layers)

        channels = []
        if state_full is not None:
            for s in state_full:
                channels.append((s, None))
        else:
            if state_rh is not None:
                for s in state_rh:
                    channels.append((s, "RH"))
            if state_lh is not None:
                for s in state_lh:
                    channels.append((s, "LH"))
        yield bar, channels


# ---- Main ------------------------------------------------------------------
def main():
    if os.path.exists(OUTPUT_CONCERT):
        print(f"Removing existing {OUTPUT_CONCERT}")
        shutil.rmtree(OUTPUT_CONCERT)

    print(f"Loading template blobs from {TEMPLATE_BLOBS_JSON}")
    blobs = load_blobs()
    alias_blobs = {
        "mappings": blobs["alias_mappings"],
        "layer": blobs["alias_layer"],
        "metaInfo": blobs["alias_meta_info"],
    }
    sampler_channel_template = plistlib.loads(blobs["sampler_source_channel_plist"])

    print(f"Loading cues from {CUES_JSON}")
    with open(CUES_JSON) as f:
        songs = json.load(f)

    if os.path.exists(MAPPING_JSON):
        print(f"Loading sound mapping from {MAPPING_JSON}")
        with open(MAPPING_JSON) as f:
            mapping = json.load(f)
    else:
        print(f"No {MAPPING_JSON} — every cue will fall through to EXS "
              f"synthesis or placeholder")
        mapping = {}

    print(f"Loading legacy SOUNDS bank inventory from {SOUNDS_BANK_CONCERT}")
    inventory = load_sounds_inventory()

    # SOUND BANK scan + reporting
    catalog = None
    aliases = {}
    families = {}
    sound_bank_mod = None
    try:
        sys.path.insert(0, os.path.dirname(TEMPLATE_BLOBS_JSON))
        import sound_bank
        sound_bank_mod = sound_bank
        if os.path.exists(COMMON_NAMES_JSON):
            with open(COMMON_NAMES_JSON) as f:
                cn = json.load(f)
            aliases = {k: v for k, v in cn.items() if not k.startswith("_")}
            fams = cn.get("_families", {})
            families = {k: v for k, v in fams.items() if not k.startswith("_")}
        catalog = sound_bank.scan_sound_banks(SOUND_BANK_ROOTS)
        distinct_sounds = collect_sound_names(songs)
        print()
        print("=" * 70)
        print(sound_bank.report(catalog, distinct_sounds, aliases))
        print("=" * 70)
        print()
    except Exception as e:
        print(f"[SOUND BANK scan skipped: {e}]\n")

    # Resolve each SOUND (individual layer) to a source (map/exs/placeholder)
    sound_sources = resolve_sounds(songs, mapping, inventory, catalog, aliases,
                                   sound_bank_mod)
    tier_counts = {"map": 0, "exs": 0, "placeholder": 0}
    for s in sound_sources.values():
        tier_counts[s["tier"]] += 1
    print(f"Sound resolution: "
          f"map={tier_counts['map']}, "
          f"exs={tier_counts['exs']}, "
          f"placeholder={tier_counts['placeholder']}")

    # For each unique EXS-resolved sound: allocate UUID/instID, synthesize .cst
    exs_needed = {}  # exs_stem → {"uuid": ..., "instid": ..., "cst": bytes}
    next_instid = 10000
    for sound, src in sound_sources.items():
        if src["tier"] != "exs":
            continue
        stem = src["exs_stem"]
        if stem in exs_needed:
            continue
        new_uuid = str(uuidlib.uuid4()).upper()
        exs_needed[stem] = {
            "uuid": new_uuid,
            "instid": next_instid,
            "cst": sound_bank_mod.synthesize_cst(
                blobs["sampler_template_cst"],
                SAMPLER_TEMPLATE_EXS_STEM,
                stem,
                new_uuid,
            ),
        }
        next_instid += 4
    if exs_needed:
        print(f"Synthesized {len(exs_needed)} Auto .cst entries "
              f"(EXS-resolved sounds).")

    # Build the bundle skeleton
    print(f"Creating {OUTPUT_CONCERT}")
    os.makedirs(OUTPUT_CONCERT, exist_ok=True)

    write_bytes(f"{OUTPUT_CONCERT}/data.plist", blobs["top_data_plist"])
    write_bytes(f"{OUTPUT_CONCERT}/base.plistZ", blobs["base_plistZ"])
    os.makedirs(f"{OUTPUT_CONCERT}/workspace.layout", exist_ok=True)
    write_bytes(
        f"{OUTPUT_CONCERT}/workspace.layout/setupZ.layout",
        blobs["workspace_layout"],
    )

    for media_folder in ("Impulse Responses", "Sampler Instruments"):
        src = f"{SOUNDS_BANK_CONCERT}/{media_folder}"
        dst = f"{OUTPUT_CONCERT}/{media_folder}"
        if os.path.exists(src):
            shutil.copytree(src, dst)
    samples_src = f"{SOUNDS_BANK_CONCERT}/Samples"
    if os.path.exists(samples_src):
        os.symlink(
            f"../{os.path.basename(SOUNDS_BANK_CONCERT)}/Samples",
            f"{OUTPUT_CONCERT}/Samples",
        )

    concert_patch_path = f"{OUTPUT_CONCERT}/Concert.patch"
    os.makedirs(concert_patch_path, exist_ok=True)

    write_bytes(f"{concert_patch_path}/Master.cst", blobs["root_cst_master"])
    write_bytes(f"{concert_patch_path}/Metronome.cst", blobs["root_cst_metronome"])
    write_bytes(f"{concert_patch_path}/Output 1-2.cst", blobs["root_cst_output_1_2"])
    write_bytes(f"{concert_patch_path}/Reverb.cst", blobs["root_cst_reverb"])

    # Song sets + leaf patches
    placeholders = []   # (song_num, bar, sound_name) for reporting at end
    layer_count = 0     # total alias channels emitted (splits + layers)
    split_patch_count = 0
    layered_patch_count = 0
    song_folder_names = []
    for song in songs:
        num = song["number"]
        title = song["title"]
        tacet = song.get("tacet", False)
        cues = song["cues"]

        clean_title = title.replace("/", "-").replace('"', "'")
        folder_name = f"{num}. {clean_title}.patch"
        song_folder_names.append(folder_name)

        song_folder = f"{concert_patch_path}/{folder_name}"
        os.makedirs(song_folder, exist_ok=True)

        # Materialise the state-walker output — one (bar, channels_spec)
        # entry per bar with any cue.
        walker = list(walk_song_state(cues))
        m_folder_names = [f"m{bar}.patch" for bar, _ in walker]

        set_dict = make_set_patch_dict(
            f"{num}. {clean_title}",
            m_folder_names,
            blobs["set_patch_data_plist"],
        )
        write_plist(f"{song_folder}/data.plist", set_dict)

        if tacet or not walker:
            continue

        for bar, channels_spec in walker:
            m_folder = f"{song_folder}/m{bar}.patch"
            os.makedirs(m_folder, exist_ok=True)

            zones_here = {z for _, z in channels_spec if z is not None}
            is_split = len(zones_here) >= 2
            is_layered = len(channels_spec) >= 2 and not is_split

            channels = []
            for sound_name, zone in channels_spec:
                src = sound_sources.get(sound_name, {"tier": "placeholder"})
                # Map-tier sound gets its family from the mapping category
                # (Roger's mapping.json already uses family names for cat).
                # Others go through get_family() → _families → 'Other'.
                if src["tier"] == "map":
                    family = src["cat"]
                else:
                    family = get_family(sound_name, families, aliases)
                ch, kind = build_alias_channel(
                    sound_name=sound_name,
                    zone=zone,
                    sound_source=src,
                    family=family,
                    inventory=inventory,
                    exs_needed=exs_needed,
                    sampler_channel_template=sampler_channel_template,
                    alias_blobs=alias_blobs,
                    m_folder=m_folder,
                    sounds_bank_concert=SOUNDS_BANK_CONCERT,
                )
                if ch is None:
                    placeholders.append((num, bar, sound_name))
                else:
                    channels.append(ch)
                    layer_count += 1

            if is_split:
                split_patch_count += 1
            elif is_layered:
                layered_patch_count += 1

            leaf_dict = make_leaf_patch_dict(
                f"m{bar}", channels, blobs["leaf_patch_data_plist"],
            )
            write_plist(f"{m_folder}/data.plist", leaf_dict)

    # SOUNDS.patch — bucketed by instrument FAMILY. Every source channel
    # (map-tier .cst pulled from the legacy SOUNDS bank + Auto-tier .cst
    # synthesized from EXS) lands in a SOUNDS.patch/<Family>.patch/ folder.
    # No more Auto.patch bucket; Keyboards/Strings/Brass/… are unified.
    #
    # Every source channel is coloured for its family so the SOUNDS set
    # and every song patch that aliases it read visually as one family.
    sounds_dst = f"{concert_patch_path}/SOUNDS.patch"
    os.makedirs(sounds_dst, exist_ok=True)

    # family → list of (source_channel_dict, cst_bytes_or_srcpath, filename,
    #                   is_src_path)
    # is_src_path=True means the "cst" value is a path on disk to copy from;
    # False means it's raw bytes to write out.
    family_sources = {}

    # Map-tier sources: cat name IS the family name in mapping.json's
    # scheme. One entry per (cat, fn) pair actually referenced.
    used_map_pairs = {(s["cat"], s["fn"]) for s in sound_sources.values()
                      if s["tier"] == "map"}
    for cat, fn in used_map_pairs:
        src_ch = copy.deepcopy(inventory[(cat, fn)])
        apply_family_color(src_ch, cat)
        cst_path = (f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch/"
                    f"{cat}.patch/{fn}")
        family_sources.setdefault(cat, []).append(
            (src_ch, cst_path, fn, True))

    # Auto-tier sources: family from get_family() on any sound that used
    # this stem. Multiple sounds may resolve to the same stem; take the
    # first-seen family, deterministic under sorted-sound iteration.
    stem_family = {}
    for sound in sorted(sound_sources):
        src = sound_sources[sound]
        if src["tier"] != "exs":
            continue
        stem = src["exs_stem"]
        if stem not in stem_family:
            stem_family[stem] = get_family(sound, families, aliases)

    for stem in sorted(exs_needed):
        entry = exs_needed[stem]
        family = stem_family.get(stem, "Other")
        src_ch = build_auto_source_channel(
            sampler_channel_template, stem, entry["uuid"], entry["instid"],
        )
        apply_family_color(src_ch, family)
        family_sources.setdefault(family, []).append(
            (src_ch, entry["cst"], f"{stem}.cst", False))

    # Emit one <Family>.patch per family that has sources, in the fixed
    # canonical order.
    category_nodes = []
    for family in FAMILY_ORDER:
        if family not in family_sources:
            continue
        fam_folder = f"{sounds_dst}/{family}.patch"
        os.makedirs(fam_folder, exist_ok=True)
        channels = []
        # Sort sources within a family by Channel_name so the mixer is stable
        for src_ch, cst, filename, is_path in sorted(
            family_sources[family], key=lambda t: t[0].get("Channel_name", "")
        ):
            dst_cst = f"{fam_folder}/{filename}"
            if is_path:
                shutil.copy(cst, dst_cst)
            else:
                write_bytes(dst_cst, cst)
            channels.append(src_ch)
        fam_dict = make_leaf_patch_dict(
            family, channels, blobs["leaf_patch_data_plist"],
        )
        write_plist(f"{fam_folder}/data.plist", fam_dict)
        category_nodes.append(f"{family}.patch")
        print(f"  SOUNDS.patch/{family}.patch: {len(channels)} sources")

    sounds_dict = make_set_patch_dict("SOUNDS", category_nodes,
                                      blobs["set_patch_data_plist"])
    write_plist(f"{sounds_dst}/data.plist", sounds_dict)

    song_folder_names.append("SOUNDS.patch")

    # Concert.patch/data.plist — synthesized from the blob template
    concert_dict = plistlib.loads(blobs["concert_patch_data_plist"])
    concert_dict["nodes"] = song_folder_names
    en = concert_dict["patch"]["engineNode"]
    en["hasProgramChange"] = False
    en["patchChangeNum"] = 0
    write_plist(f"{concert_patch_path}/data.plist", concert_dict)

    # Summary
    n_patches = sum(
        len({c["bar"] for c in s["cues"]})
        for s in songs
        if not s.get("tacet")
    )
    print(f"\nDone.")
    print(f"  Show:                {SHOW}")
    print(f"  Sets:                {len(songs)} (+ SOUNDS)")
    print(f"  Patches:             {n_patches}")
    print(f"  Split patches:       {split_patch_count}")
    print(f"  Layered patches:     {layered_patch_count}")
    print(f"  Alias channels:      {layer_count}")
    print(f"  Auto SOUNDS entries: {len(exs_needed)}")
    print(f"  Placeholders:        {len(placeholders)}")
    if placeholders:
        print(f"  Placeholder sounds (need manual fill-in):")
        for num, bar, cue in placeholders[:10]:
            print(f"    song {num} m{bar}: {cue!r}")
        if len(placeholders) > 10:
            print(f"    ... and {len(placeholders)-10} more")
    print(f"  Output: {OUTPUT_CONCERT}")


if __name__ == "__main__":
    main()
