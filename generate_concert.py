#!/usr/bin/env python3
"""
Generate a MainStage .concert bundle from:
- an extracted cue list (per song, ordered by bar)
- a cue → SOUNDS-bank mapping
- the user's SOUNDS bank (a concert bundle providing SOUNDS.patch and
  its supporting Sampler Instruments / Samples / Impulse Responses)
- template_blobs.json (opaque bits: root .cst binaries, plist skeletons,
  workspace layout, alias metadata bplists)

No dependency on any reference "template" concert bundle at runtime —
everything the tool needs beyond user inputs lives in template_blobs.json.
"""

import plistlib
import os
import shutil
import sys
import json
import base64
import io
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

# The user's SOUNDS bank — the concert bundle that hosts SOUNDS.patch and
# the supporting Sampler Instruments / Samples / Impulse Responses folders.
# For now this is Footloose K2; over time Roger may curate a dedicated
# SOUNDS-bank concert.
SOUNDS_BANK_CONCERT = f"{BASE}/Footloose K2.concert"

# SOUND BANK roots — folders each containing Sampler Instruments/ and
# Samples/. Scanned at start-up to produce a match report against the show's
# cue list. Default is the macOS Audio Music Apps location. Overridable via
# SOUND_BANK_ROOTS env var (colon-separated), useful when running inside a
# containerised environment where ~ isn't the user's home directory.
_default_roots = [os.path.expanduser("~/Music/Audio Music Apps")]
_env_roots = os.environ.get("SOUND_BANK_ROOTS", "")
SOUND_BANK_ROOTS = [p for p in _env_roots.split(":") if p] or _default_roots

OUTPUT_CONCERT = f"{BASE}/Footloose K2 GENERATED.concert"

# Per Roger's conventions
GENERIC_ICON = 4505


# ---- Helpers ---------------------------------------------------------------
def load_blobs():
    with open(TEMPLATE_BLOBS_JSON) as f:
        raw = json.load(f)
    return {k: base64.b64decode(v) for k, v in raw.items()}


def load_plist_bytes(b):
    return plistlib.loads(b)


def write_plist(path, obj):
    with open(path, "wb") as f:
        plistlib.dump(obj, f, fmt=plistlib.FMT_BINARY)


def write_bytes(path, b):
    with open(path, "wb") as f:
        f.write(b)


def load_sounds_inventory():
    """Read the user's SOUNDS bank — SOUNDS.patch/<Category>.patch/data.plist
    from SOUNDS_BANK_CONCERT — and return {(category, filename) → channel-dict}."""
    inventory = {}
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
    # UUID + instID inherited from the SOUNDS-bank source (this is the aliasing key)
    new_ch["isAlias"] = True
    new_ch["aliasUUID"] = new_ch["UUID"]
    new_ch["mappings"] = alias_blobs["mappings"]
    new_ch["layer"] = alias_blobs["layer"]
    new_ch["metaInfo"] = alias_blobs["metaInfo"]
    # Manual preset display: strip preset identification
    new_ch["Channel_chaStrName"] = None
    new_ch["Channel_chaStrFullPath"] = None
    new_ch["Channel_chaStrCategory"] = ""
    return new_ch


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

    print(f"Loading cues from {CUES_JSON}")
    with open(CUES_JSON) as f:
        songs = json.load(f)

    print(f"Loading sound mapping from {MAPPING_JSON}")
    with open(MAPPING_JSON) as f:
        mapping = json.load(f)

    print(f"Loading SOUNDS bank inventory from {SOUNDS_BANK_CONCERT}")
    inventory = load_sounds_inventory()

    # SOUND BANK scan — catalog available .exs sampler instruments across the
    # configured SOUND BANK roots. Produces an informational report showing
    # which book cues could be served by an EXS instrument. Doesn't yet
    # change what gets written — that's the next architectural step.
    try:
        sys.path.insert(0, os.path.dirname(TEMPLATE_BLOBS_JSON))
        import sound_bank
        cn_path = COMMON_NAMES_JSON if os.path.exists(COMMON_NAMES_JSON) else None
        aliases = {}
        if cn_path:
            with open(cn_path) as f:
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

    # Sanity-check the mapping targets exist in the current SOUNDS bank
    missing = []
    for cn, (cat, fn) in mapping.items():
        if (cat, fn) not in inventory:
            missing.append((cn, cat, fn))
    if missing:
        print("WARNING — mapping targets missing from current SOUNDS bank:")
        for cn, cat, fn in missing:
            print(f"  {cn!r:<28} -> {cat}/{fn}")
        print("  (song patches referencing these sounds will be skipped)\n")

    # Build the bundle skeleton
    print(f"Creating {OUTPUT_CONCERT}")
    os.makedirs(OUTPUT_CONCERT, exist_ok=True)

    # Top-level document files — pure blob writes
    write_bytes(f"{OUTPUT_CONCERT}/data.plist", blobs["top_data_plist"])
    write_bytes(f"{OUTPUT_CONCERT}/base.plistZ", blobs["base_plistZ"])
    os.makedirs(f"{OUTPUT_CONCERT}/workspace.layout", exist_ok=True)
    write_bytes(
        f"{OUTPUT_CONCERT}/workspace.layout/setupZ.layout",
        blobs["workspace_layout"],
    )

    # Media folders — symlink to the SOUNDS-bank concert (relative target)
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

    # Concert.patch
    concert_patch_path = f"{OUTPUT_CONCERT}/Concert.patch"
    os.makedirs(concert_patch_path, exist_ok=True)

    # Four root channel-strip .cst files — written from blobs
    write_bytes(f"{concert_patch_path}/Master.cst", blobs["root_cst_master"])
    write_bytes(f"{concert_patch_path}/Metronome.cst", blobs["root_cst_metronome"])
    write_bytes(f"{concert_patch_path}/Output 1-2.cst", blobs["root_cst_output_1_2"])
    write_bytes(f"{concert_patch_path}/Reverb.cst", blobs["root_cst_reverb"])

    # Song sets + leaf patches
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

        # Group by bar (multi-channel patches share a bar)
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

        if not tacet:
            for bar, bar_cues in by_bar.items():
                m_folder = f"{song_folder}/m{bar}.patch"
                os.makedirs(m_folder, exist_ok=True)

                channels = []
                for cue in bar_cues:
                    ch_name = cue["channel_name"]
                    if ch_name not in mapping:
                        print(f"  WARNING: no mapping for {ch_name!r} — skipping")
                        continue
                    cat, fn = mapping[ch_name]
                    if (cat, fn) not in inventory:
                        continue  # already warned above
                    src_ch = inventory[(cat, fn)]

                    # Copy the .cst file byte-for-byte from the SOUNDS bank
                    src_cst = f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch/{cat}.patch/{fn}"
                    shutil.copy(src_cst, f"{m_folder}/{fn}")

                    channels.append(apply_conventions_to_channel(
                        src_ch,
                        display_name=ch_name,
                        output_index=0,
                        alias_blobs=alias_blobs,
                    ))

                leaf_dict = make_leaf_patch_dict(
                    f"m{bar}",
                    channels,
                    blobs["leaf_patch_data_plist"],
                )
                write_plist(f"{m_folder}/data.plist", leaf_dict)

    # SOUNDS.patch — copied verbatim from the SOUNDS-bank concert
    print("Copying SOUNDS.patch verbatim from SOUNDS bank")
    shutil.copytree(
        f"{SOUNDS_BANK_CONCERT}/Concert.patch/SOUNDS.patch",
        f"{concert_patch_path}/SOUNDS.patch",
    )
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
    print(f"  Sets:    {len(songs)} (+ SOUNDS)")
    print(f"  Patches: {n_patches}")
    print(f"  Output:  {OUTPUT_CONCERT}")


if __name__ == "__main__":
    main()
