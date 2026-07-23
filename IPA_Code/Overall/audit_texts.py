#!/usr/bin/env python3
"""Audit Raw_Texts. Four mechanical, judgement-free checks:

  1. Character census    - exhaustive codepoint inventory, on the NFD form
                           the engine will actually match against
  2. Normalization audit - decomposition vs canonical reordering, and any
                           Hebrew presentation forms (U+FB1D-FB4F)
  3. Allowlist gate      - every codepoint must be previously classified.
                           A codepoint we have never seen fails the run.
  4. Checksum check      - ripped files must match the manifest

Nothing here is judged, guessed or authored. The census bounds the mapping
tables: it is the finite list of codepoints that must be given an IPA value.
"""
import json, hashlib, unicodedata, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
OUT = ROOT / "Raw_Texts"
ALLOWLIST = OUT / "codepoint_allowlist.json"

HEBREW_PRESENTATION = (0xFB1D, 0xFB4F)
CATGROUP = {"Lu": "letter", "Ll": "letter", "Lo": "letter", "Lt": "letter",
            "Mn": "mark", "Mc": "mark", "Me": "mark"}

# Codepoints that look anomalous but are verified legitimate. Recorded so a
# future reader does not "clean" them away.
NOTES = {
    0x034F: "COMBINING GRAPHEME JOINER - blocks canonical reordering of "
            "Masoretic marks. Load-bearing: never strip.",
    0x05C0: "PASEQ - phrase divider; WLC encodes it attached to the preceding "
            "word, preceded by a space.",
    0x0020: "SPACE inside `raw` - occurs only as part of the paseq encoding.",
    0x2019: "RIGHT SINGLE QUOTATION MARK - Greek elision apostrophe; part of "
            "the word, phonetically meaningful.",
    0x05BE: "MAQQEF - joins words; the following `after` is empty by design.",
}


def describe(ch):
    return {"char": ch, "name": unicodedata.name(ch, "<unnamed>"),
            "category": unicodedata.category(ch),
            "combining": unicodedata.combining(ch)}


def load(fn):
    return [json.loads(l) for l in open(OUT / fn, encoding="utf-8")]


def main():
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))

    by_edition = defaultdict(list)
    for o in manifest["outputs"]:
        by_edition[o["edition"]].append(o["file"])

    # ---- 4. checksums -----------------------------------------------------
    bad_sums = [o["file"] for o in manifest["outputs"]
                if hashlib.sha256((OUT / o["file"]).read_bytes()).hexdigest()
                != o["sha256"]]

    report, failures = {}, list(bad_sums)
    for ed, files in by_edition.items():
        recs = [r for f in files for r in load(f)]
        raws = [r["raw"] for r in recs]

        # ---- 2. normalization audit (on the as-stored text) ---------------
        decomposed = reordered = 0
        presentation = Counter()
        for w in raws:
            nfd = unicodedata.normalize("NFD", w)
            if nfd != w:
                if len(nfd) != len(w):
                    decomposed += 1
                else:
                    reordered += 1
            for ch in w:
                if HEBREW_PRESENTATION[0] <= ord(ch) <= HEBREW_PRESENTATION[1]:
                    presentation[ch] += 1

        # ---- 1. census on the NFD form ------------------------------------
        census = Counter()
        for w in raws:
            census.update(unicodedata.normalize("NFD", w))
        sep = Counter()
        for r in recs:
            sep.update(r["after"])

        report[ed] = {"files": files, "words": len(recs), "census": census,
                      "sep": sep, "presentation": presentation,
                      "decomposed": decomposed, "reordered": reordered}

    # ---- 3. allowlist gate -------------------------------------------------
    prior = (json.loads(ALLOWLIST.read_text(encoding="utf-8"))
             if ALLOWLIST.exists() else None)
    bootstrapped = prior is None

    allowlist = {"purpose": "frozen inventory of every codepoint known to occur "
                            "in Raw_Texts. A new codepoint fails the audit until "
                            "it is classified here.",
                 "editions": {}}
    gate = {}
    for ed, R in report.items():
        known = set()
        if prior and ed in prior.get("editions", {}):
            known = {int(k[2:], 16) for k in prior["editions"][ed]["codepoints"]}
        seen = {ord(c) for c in R["census"]}
        gate[ed] = {"new": sorted(seen - known) if not bootstrapped else [],
                    "absent": sorted(known - seen) if not bootstrapped else []}
        if gate[ed]["new"]:
            failures.append(f"{ed}: unclassified codepoints")

        entries = {}
        for ch, n in sorted(R["census"].items(), key=lambda x: ord(x[0])):
            e = describe(ch)
            e["count"] = n
            e["role"] = CATGROUP.get(e["category"], "punct/other")
            if ord(ch) in NOTES:
                e["note"] = NOTES[ord(ch)]
            entries[f"U+{ord(ch):04X}"] = e
        allowlist["editions"][ed] = {"words": R["words"],
                                     "distinct": len(entries),
                                     "codepoints": entries}
    # A failing gate must NEVER rewrite the allowlist. Otherwise one bad run
    # freezes the contamination as "known good" and the gate is dead forever.
    gate_clean = not any(g["new"] for g in gate.values())
    if bootstrapped or gate_clean:
        ALLOWLIST.write_text(json.dumps(allowlist, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ------------------------------------------------------------------ out
    print(f"{'edition':9s} {'words':>6s} {'cps':>5s}  {'letter':>6s} {'mark':>5s} "
          f"{'other':>6s}   {'decomp':>7s} {'reorder':>8s}  gate")
    print("-" * 78)
    for ed, R in report.items():
        g = defaultdict(int)
        for ch in R["census"]:
            g[CATGROUP.get(unicodedata.category(ch), "punct/other")] += 1
        status = ("BOOTSTRAP" if bootstrapped else
                  ("FAIL" if gate[ed]["new"] else "pass"))
        print(f"{ed:9s} {R['words']:6d} {len(R['census']):5d}  {g['letter']:6d} "
              f"{g['mark']:5d} {g['punct/other']:6d}   {R['decomposed']:7d} "
              f"{R['reordered']:8d}  {status}")

    print("\nseparators in `after`:")
    for ed, R in report.items():
        s = ", ".join(f"U+{ord(c):04X}×{n}" for c, n in
                      sorted(R["sep"].items(), key=lambda x: -x[1]) if c)
        print(f"  {ed:9s} {s or '(none)'}")

    print("\nannotated codepoints (verified legitimate, do not strip):")
    for ed, R in report.items():
        hits = [c for c in R["census"] if ord(c) in NOTES]
        for c in sorted(hits, key=ord):
            print(f"  {ed:9s} U+{ord(c):04X} ×{R['census'][c]:<4d} "
                  f"{unicodedata.name(c,'?')}")

    print("\npresentation forms (U+FB1D-FB4F):",
          sum(sum(R["presentation"].values()) for R in report.values()) or "none")
    print("checksum mismatches:", bad_sums or "none")

    if bootstrapped:
        print(f"\nallowlist BOOTSTRAPPED -> {ALLOWLIST.name} "
              f"({sum(a['distinct'] for a in allowlist['editions'].values())} "
              f"codepoints frozen). Future runs fail on anything new.")
    else:
        for ed, gg in gate.items():
            if gg["new"]:
                print(f"\n{ed} UNCLASSIFIED: " +
                      ", ".join(f"U+{cp:04X}" for cp in gg["new"]))
            if gg["absent"]:
                print(f"{ed} in allowlist but absent from corpus: " +
                      ", ".join(f"U+{cp:04X}" for cp in gg["absent"]))
        print("\nallowlist gate:", "FAIL" if any(g["new"] for g in gate.values())
              else "pass")

    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
