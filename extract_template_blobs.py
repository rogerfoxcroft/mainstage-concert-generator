#!/usr/bin/env python3
"""
Extract every opaque bit the generator needs from Footloose K2 (plus a
Harp Gliss template concert) into a single template_blobs.json. Runs once.

After this, the generator has no runtime dependency on any reference
concert bundle — it only needs template_blobs.json + the user's SOUNDS
bank + the extracted cues.

Blobs extracted:
- top_data_plist                 — bundle-root data.plist
- base_plistZ                    — WsSplitKeyedArchiver session state
- workspace_layout               — workspace.layout/setupZ.layout
- concert_patch_data_plist       — Concert.patch/data.plist (with Orch Rev
                                    renamed to Reverb in advance)
- set_patch_data_plist           — a set-level data.plist skeleton
- leaf_patch_data_plist          — a leaf-level data.plist skeleton
- root_cst_master, root_cst_metronome, root_cst_output_1_2,
  root_cst_reverb                — the four concert-level channel-strip
                                    .cst binaries (Reverb is Orch Rev
                                    verbatim; the name is set in the
                                    plist above)
- alias_mappings, alias_layer,
  alias_meta_info                — the three NSKeyedArchive bplists that
                                    every song-patch alias needs
- sampler_template_cst           — a Sampler-based .cst template (Harp)
                                    with UUID + EXS filename substitution
                                    points for synthesising new SOUNDS
- sampler_source_channel_plist   — the source channel dict for the sampler
                                    template
- harp_gliss_template_cst        — a Harp channel strip with the Scripter
                                    plugin loaded from harp_gliss.scripter
                                    .js, pulled from a Harp Gliss reference
                                    concert. Used verbatim as the SOURCE
                                    .cst whenever a cue asks for 'Harp
                                    Gliss'; song patches alias into it.
- harp_gliss_source_channel_plist — the channel dict for the harp gliss
                                    template (same role as the sampler
                                    source channel dict, above)
"""
import plistlib
import base64
import json
import os
import sys

BASE = os.environ.get(
    "CONCERT_BUILDER_BASE",
    "/Users/roger/Music/Mainstage Concert Builder",
)
REF = f"{BASE}/Footloose K2.concert"
HARP_GLISS_REF = f"{BASE}/Harp Gliss.concert"
OUT = f"{BASE}/_generator/template_blobs.json"


def b64_file(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def main():
    blobs = {}

    # Verbatim binary file blobs
    for key, path in [
        ("top_data_plist", f"{REF}/data.plist"),
        ("base_plistZ", f"{REF}/base.plistZ"),
        ("workspace_layout", f"{REF}/workspace.layout/setupZ.layout"),
        ("root_cst_master", f"{REF}/Concert.patch/Master.cst"),
        ("root_cst_metronome", f"{REF}/Concert.patch/Metronome.cst"),
        ("root_cst_output_1_2", f"{REF}/Concert.patch/Output 1-2.cst"),
        # Orch Rev's binary is what we want as Reverb — filename change is
        # handled in the plist below and by where we write it in the generator
        ("root_cst_reverb", f"{REF}/Concert.patch/Orch Rev.cst"),
        # Sampler-based .cst template. Contains a Sampler plugin instance
        # with 'Harp.exs' referenced in two null-padded chunks and a single
        # 16-byte embedded UUID; `synthesize_cst` in sound_bank.py
        # substitutes both to produce .cst files for arbitrary EXS
        # instruments.
        ("sampler_template_cst",
         f"{REF}/Concert.patch/SOUNDS.patch/Strings.patch/Harp.cst"),
        ("set_patch_data_plist",
         f"{REF}/Concert.patch/1. On any Given Sunday.patch/data.plist"),
        ("leaf_patch_data_plist",
         f"{REF}/Concert.patch/1. On any Given Sunday.patch/m8.patch/data.plist"),
    ]:
        blobs[key] = b64_file(path)
        print(f"  {key:<30} {len(blobs[key]):>8} b64 chars")

    # Concert.patch/data.plist — pre-rename Orch Rev → Reverb so the template
    # ships clean and the generator doesn't need a runtime rename.
    pl = plistlib.load(open(f"{REF}/Concert.patch/data.plist", "rb"))
    for ch in pl.get("channels", []):
        if ch.get("Channel_name") == "Orch Rev":
            ch["Channel_name"] = "Reverb"
            ch["Filename"] = "Reverb.cst"
    buf = plistlib.dumps(pl, fmt=plistlib.FMT_BINARY)
    blobs["concert_patch_data_plist"] = base64.b64encode(buf).decode("ascii")
    print(f"  concert_patch_data_plist       {len(blobs['concert_patch_data_plist']):>8} b64 chars (Orch Rev → Reverb applied)")

    # Alias metadata bplists from Hard Rock m8 — same source as before
    m8 = plistlib.load(open(
        f"{REF}/Concert.patch/1. On any Given Sunday.patch/m8.patch/data.plist",
        "rb"
    ))
    ch = m8["channels"][0]
    for key, blob_key in [
        ("alias_mappings", "mappings"),
        ("alias_layer", "layer"),
        ("alias_meta_info", "metaInfo"),
    ]:
        blobs[key] = base64.b64encode(ch[blob_key]).decode("ascii")
        print(f"  {key:<30} {len(blobs[key]):>8} b64 chars")

    # SOUNDS-bank channel-dict template — the Harp entry from Roger's Strings
    # category. Used as the base for synthesized Auto/<sound> entries so we
    # inherit the color blob, standard routing defaults, and everything else
    # a proper SOUNDS-bank channel needs. Serialized as its own binary plist
    # so nested bytes (like the color bplist) survive the trip through JSON.
    strings_pl = plistlib.load(open(
        f"{REF}/Concert.patch/SOUNDS.patch/Strings.patch/data.plist", "rb"
    ))
    harp_ch = next(c for c in strings_pl["channels"] if c["Filename"] == "Harp.cst")
    ch_plist_bytes = plistlib.dumps(harp_ch, fmt=plistlib.FMT_BINARY)
    blobs["sampler_source_channel_plist"] = base64.b64encode(ch_plist_bytes).decode("ascii")
    print(f"  sampler_source_channel_plist   {len(blobs['sampler_source_channel_plist']):>8} b64 chars")

    # Harp Gliss template — the one-and-only channel strip in Roger's
    # Harp Gliss reference concert. Harp EXS + Scripter plugin loaded from
    # harp_gliss.scripter.js. We ship the .cst verbatim and the source
    # channel dict from its containing patch's data.plist.
    if os.path.isdir(HARP_GLISS_REF):
        # Find the single patch folder and its channel dict
        gliss_concert = plistlib.load(open(
            f"{HARP_GLISS_REF}/Concert.patch/data.plist", "rb"
        ))
        # The one leaf/set folder under Concert.patch that isn't SOUNDS
        gliss_patch_name = next(
            n for n in gliss_concert.get("nodes", [])
            if n != "SOUNDS.patch" and n.endswith(".patch")
        )
        gliss_leaf = plistlib.load(open(
            f"{HARP_GLISS_REF}/Concert.patch/{gliss_patch_name}/data.plist", "rb"
        ))
        gliss_ch = gliss_leaf["channels"][0]
        gliss_cst = f"{HARP_GLISS_REF}/Concert.patch/{gliss_patch_name}/{gliss_ch['Filename']}"
        blobs["harp_gliss_template_cst"] = b64_file(gliss_cst)
        print(f"  harp_gliss_template_cst        {len(blobs['harp_gliss_template_cst']):>8} b64 chars")
        gliss_ch_bytes = plistlib.dumps(gliss_ch, fmt=plistlib.FMT_BINARY)
        blobs["harp_gliss_source_channel_plist"] = base64.b64encode(gliss_ch_bytes).decode("ascii")
        print(f"  harp_gliss_source_channel_plist{len(blobs['harp_gliss_source_channel_plist']):>8} b64 chars")
    else:
        print(f"  [Harp Gliss reference concert not at {HARP_GLISS_REF} — "
              f"skipping harp_gliss blobs; the generator will fall through "
              f"to placeholder for HARP GLISS cues]")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(blobs, f)

    total = sum(len(v) for v in blobs.values())
    print(f"\nSaved to {OUT}")
    print(f"Total: {len(blobs)} blobs, {total:,} b64 chars ({total * 3 // 4:,} raw bytes)")


if __name__ == "__main__":
    main()
