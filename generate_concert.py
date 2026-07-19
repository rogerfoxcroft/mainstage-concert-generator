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
  1. Explicit `mapping.json` entry → use that .cst from the legacy SOUNDS
     bank concert bundle.
  2. SOUND BANK scan matches an EXS → synthesize a fresh .cst via
     sound_bank.synthesize_cst, place under SOUNDS.patch/Auto.patch/, and
     alias into every song patch that references this cue.
  3. Placeholder → emit an empty leaf patch (no channels), report it.
"""

import plistlib
import os
import shutil
import sys
import json
import base64
import uuid as uuidlib
import copy

# ---- Paths -----------------------------------------------------------------
BASE = os.environ.get(
    "CONCERT_BUILDER_BASE",
    "/Users/roger/Music/Mainstage Concert Builder",
)
TEMPLATE_BLOBS_JSON = f"{BASE}/_generator/template_blobs.json"
CUES_JSON           = f"{BASE}/_generator/cues.json"
MAPPING_JSON        = f"{BASE}/_generator/mapping.json"
COMMON_NAMES_JSON   = f"{BASE}/_generator/common_names.json"

# Legacy SOUNDS bank concert bundle — still used for cues resolved via
# explicit mapping.json entries. Optional: leave empty to force EXS-only.
SOUNDS_BANK_CONCERT = f"{BASE}/Footloose K2.concert"

# SOUND BANK roots — folders each containing Sampler Instruments/ and
# Samples/. Overridable via SOUND_BANK_ROOTS env var (colon-separated).
_default_roots = [os.path.expanduser("~/Music/Audio Music Apps")]
_env_roots = os.environ.get("SOUND_BANK_ROOTS", "")
SOUND_BANK_ROOTS = [p for p in _env_roots.split(":") if p] or _default_roots

OUTPUT_CONCERT = f"{BASE}/Footloose K2 GENERATED.concert"

# Per Roger's conventions
GENERIC_ICON = 4505
# What the Sampler template .cst currently references (the .exs stem to swap
# out when synthesizing). Kept in sync with what extract_template_blobs.py
# packages as `sampler_template_cst`.
SAMPLER_TEMPLATE_EXS_STEM = "Harp"


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


def apply_conventions_to_channel(src_ch, display_name, output_index, alias_blobs):
    """Turn a SOUNDS-bank channel dict into a proper song-patch alias."""
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
    new_ch["layer"] = alias_blobs["layer"]
    new_ch["metaInfo"] = alias_blobs["metaInfo"]
    # Manual preset display
    new_ch["Channel_chaStrName"] = None
    new_ch["Channel_chaStrFullPath"] = None
    new_ch["Channel_chaStrCategory"] = ""
    return new_ch


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


def resolve_cues(songs, mapping, inventory, catalog, aliases, sound_bank_mod):
    """Return {cue_name: source_spec}, where source_spec is one of:
      {"tier": "map", "cat": ..., "fn": ...}
      {"tier": "exs", "exs_stem": ...}
      {"tier": "placeholder"}

    Priority (best first):
      1. Curated EXS match  — SOUND BANK gives us a real, not-SoundFont hit.
      2. Explicit mapping.json entry that resolves in the legacy SOUNDS bank.
      3. SoundFont-backup EXS match — better than nothing.
      4. Placeholder.

    Rationale: mapping.json's purpose is to override cases where auto scan
    picks the wrong instrument, or to cover cases where the SOUND BANK
    scan can only offer a SoundFont fallback. So mapping.json is the
    'trust me' override that ranks between curated-EXS and SF-EXS.
    """
    all_cues = sorted({c["channel_name"] for s in songs for c in s.get("cues", [])})
    name_index = (sound_bank_mod.build_name_index(catalog)
                  if catalog and sound_bank_mod else None)
    resolved = {}
    for cue in all_cues:
        exs_hit = (sound_bank_mod.match_cue(cue, name_index, aliases or {}, catalog)
                   if name_index is not None else None)
        curated_exs = exs_hit if exs_hit and not sound_bank_mod._is_sf_backup(exs_hit) else None
        sf_exs = exs_hit if exs_hit and sound_bank_mod._is_sf_backup(exs_hit) else None

        # Tier 1: curated EXS beats everything
        if curated_exs:
            resolved[cue] = {"tier": "exs", "exs_stem": curated_exs["name"]}
            continue
        # Tier 2: explicit map (override for SF-fallbacks and misses)
        if cue in mapping:
            cat, fn = mapping[cue]
            if (cat, fn) in inventory:
                resolved[cue] = {"tier": "map", "cat": cat, "fn": fn}
                continue
        # Tier 3: SoundFont-backup EXS fallback
        if sf_exs:
            resolved[cue] = {"tier": "exs", "exs_stem": sf_exs["name"]}
            continue
        # Tier 4: nothing
        resolved[cue] = {"tier": "placeholder"}
    return resolved


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
    sound_bank_mod = None
    try:
        sys.path.insert(0, os.path.dirname(TEMPLATE_BLOBS_JSON))
        import sound_bank
        sound_bank_mod = sound_bank
        if os.path.exists(COMMON_NAMES_JSON):
            with open(COMMON_NAMES_JSON) as f:
                aliases = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        catalog = sound_bank.scan_sound_banks(SOUND_BANK_ROOTS)
        distinct_cues = sorted({c["channel_name"] for s in songs for c in s.get("cues", [])})
        print()
        print("=" * 70)
        print(sound_bank.report(catalog, distinct_cues, aliases))
        print("=" * 70)
        print()
    except Exception as e:
        print(f"[SOUND BANK scan skipped: {e}]\n")

    # Resolve each cue to a source (map / exs / placeholder)
    cue_sources = resolve_cues(songs, mapping, inventory, catalog, aliases,
                               sound_bank_mod)
    tier_counts = {"map": 0, "exs": 0, "placeholder": 0}
    for s in cue_sources.values():
        tier_counts[s["tier"]] += 1
    print(f"Cue resolution: "
          f"map={tier_counts['map']}, "
          f"exs={tier_counts['exs']}, "
          f"placeholder={tier_counts['placeholder']}")

    # For each unique EXS-resolved sound: allocate UUID/instID, synthesize .cst
    exs_needed = {}  # exs_stem → {"uuid": ..., "instid": ..., "cst": bytes}
    next_instid = 10000
    for cue, src in cue_sources.items():
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
    placeholders = []  # (song_num, bar, cue) tuples for reporting at the end
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

        by_bar = {}
        for c in sorted(cues, key=lambda c: c["bar"]):
            by_bar.setdefault(c["bar"], []).append(c)
        m_folder_names = [f"m{b}.patch" for b in sorted(by_bar)]

        set_dict = make_set_patch_dict(
            f"{num}. {clean_title}",
            m_folder_names,
            blobs["set_patch_data_plist"],
        )
        write_plist(f"{song_folder}/data.plist", set_dict)

        if tacet:
            continue

        for bar, bar_cues in by_bar.items():
            m_folder = f"{song_folder}/m{bar}.patch"
            os.makedirs(m_folder, exist_ok=True)

            channels = []
            for cue in bar_cues:
                cue_name = cue["channel_name"]
                src = cue_sources.get(cue_name, {"tier": "placeholder"})

                if src["tier"] == "map":
                    cat, fn = src["cat"], src["fn"]
                    src_ch = inventory[(cat, fn)]
                    src_cst = (f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch/"
                               f"{cat}.patch/{fn}")
                    shutil.copy(src_cst, f"{m_folder}/{fn}")
                    channels.append(apply_conventions_to_channel(
                        src_ch,
                        display_name=cue_name,
                        output_index=0,
                        alias_blobs=alias_blobs,
                    ))

                elif src["tier"] == "exs":
                    stem = src["exs_stem"]
                    entry = exs_needed[stem]
                    filename = f"{stem}.cst"
                    write_bytes(f"{m_folder}/{filename}", entry["cst"])
                    src_ch = build_auto_source_channel(
                        sampler_channel_template, stem,
                        entry["uuid"], entry["instid"],
                    )
                    channels.append(apply_conventions_to_channel(
                        src_ch,
                        display_name=cue_name,
                        output_index=0,
                        alias_blobs=alias_blobs,
                    ))

                else:
                    placeholders.append((num, bar, cue_name))

            leaf_dict = make_leaf_patch_dict(
                f"m{bar}", channels, blobs["leaf_patch_data_plist"],
            )
            write_plist(f"{m_folder}/data.plist", leaf_dict)

    # SOUNDS.patch — build from scratch, category by category, so we only
    # ship source channels the song patches actually alias.
    #
    #   - Legacy categories (Keyboards, Strings, …): keep ONLY the (cat, fn)
    #     pairs referenced by map-tier resolutions. The category's original
    #     data.plist is filtered to the used channels; the .cst files for
    #     those channels are copied over. No orphaned Sampler instances,
    #     no wasted RAM at concert load.
    #   - Auto.patch: one channel per unique EXS-resolved stem.
    sounds_dst = f"{concert_patch_path}/SOUNDS.patch"
    os.makedirs(sounds_dst, exist_ok=True)
    category_nodes = []

    used_map = {}  # cat -> set of filenames actually aliased
    for src in cue_sources.values():
        if src["tier"] == "map":
            used_map.setdefault(src["cat"], set()).add(src["fn"])

    for cat in sorted(used_map):
        cat_folder = f"{sounds_dst}/{cat}.patch"
        os.makedirs(cat_folder, exist_ok=True)
        used_files = used_map[cat]
        src_pl_path = (f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch/"
                       f"{cat}.patch/data.plist")
        with open(src_pl_path, "rb") as f:
            cat_pl = plistlib.loads(f.read())
        cat_pl["channels"] = [ch for ch in cat_pl.get("channels", [])
                              if ch.get("Filename") in used_files]
        for fn in used_files:
            shutil.copy(
                f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch/{cat}.patch/{fn}",
                f"{cat_folder}/{fn}",
            )
        write_plist(f"{cat_folder}/data.plist", cat_pl)
        category_nodes.append(f"{cat}.patch")
        print(f"  SOUNDS.patch/{cat}.patch: {len(used_files)} sources")

    if exs_needed:
        auto_folder = f"{sounds_dst}/Auto.patch"
        os.makedirs(auto_folder, exist_ok=True)
        auto_channels = []
        for stem in sorted(exs_needed):
            entry = exs_needed[stem]
            write_bytes(f"{auto_folder}/{stem}.cst", entry["cst"])
            auto_channels.append(build_auto_source_channel(
                sampler_channel_template, stem, entry["uuid"], entry["instid"],
            ))
        auto_dict = make_leaf_patch_dict(
            "Auto", auto_channels, blobs["leaf_patch_data_plist"],
        )
        write_plist(f"{auto_folder}/data.plist", auto_dict)
        category_nodes.append("Auto.patch")
        print(f"  SOUNDS.patch/Auto.patch: {len(exs_needed)} sources")

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
    print(f"  Sets:                {len(songs)} (+ SOUNDS)")
    print(f"  Patches:             {n_patches}")
    print(f"  Auto SOUNDS entries: {len(exs_needed)}")
    print(f"  Placeholders:        {len(placeholders)}")
    if placeholders:
        print(f"  Placeholder cues (need manual fill-in):")
        for num, bar, cue in placeholders[:10]:
            print(f"    song {num} m{bar}: {cue!r}")
        if len(placeholders) > 10:
            print(f"    ... and {len(placeholders)-10} more")
    print(f"  Output: {OUTPUT_CONCERT}")


if __name__ == "__main__":
    main()
