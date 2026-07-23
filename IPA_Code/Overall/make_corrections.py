#!/usr/bin/env python3
"""Propose corrections for upstream character defects in Raw_Texts.

`raw` in Raw_Texts is NEVER edited: it stays a byte-faithful copy of the
source. Defects introduced upstream are recorded here instead, as an explicit
override layer applied at load time.

Nothing here is a judgement call. A correction is proposed only when:

  1. the stored character is outside the edition's allowed codepoint set, and
  2. it is a known visual homoglyph of a specific allowed character, and
  3. the corrected word reproduces the value recorded in the SOURCE's own
     gematria column - arithmetic the source computed before the corruption
     was introduced, and which therefore encodes the intended letters.

A proposal failing (3) is emitted as REJECTED, never silently applied.
This script only proposes; audit_texts.py re-verifies independently.
"""
import json, csv, unicodedata, sys
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
TEXTS = ROOT / "Raw_Texts"
RAW = ROOT / "Git_Ignored_Stuff/Raw_Downloads"
META = TEXTS / "MISC_INFO"        # manifest, allowlist, corrections
OUT = META / "corrections.json"

# Greek isopsephy. The 24 classical letters only; the archaic numerals
# (digamma 6, koppa 90, sampi 900) do not occur in this corpus.
GEMATRIA = {
    "Α": 1, "Β": 2, "Γ": 3, "Δ": 4, "Ε": 5, "Ζ": 7, "Η": 8, "Θ": 9,
    "Ι": 10, "Κ": 20, "Λ": 30, "Μ": 40, "Ν": 50, "Ξ": 60, "Ο": 70,
    "Π": 80, "Ρ": 100, "Σ": 200, "Τ": 300, "Υ": 400, "Φ": 500,
    "Χ": 600, "Ψ": 700, "Ω": 800,
}

# Visual homoglyphs -> the allowed character they impersonate. Pure shape
# identity, not linguistics; every use is then proved by gematria.
HOMOGLYPH = {
    0x0041: 0x0391,   # LATIN A          -> GREEK ALPHA
    0x0048: 0x0397,   # LATIN H          -> GREEK ETA
    0x0049: 0x0399,   # LATIN I          -> GREEK IOTA
    0x004E: 0x039D,   # LATIN N          -> GREEK NU
    0x004F: 0x039F,   # LATIN O          -> GREEK OMICRON
    0x0054: 0x03A4,   # LATIN T          -> GREEK TAU
    0x1F49: 0x039F,   # OMICRON W/ DASIA -> GREEK OMICRON (spurious breathing)
}

ALLOWED_NRV = {cp for cp in range(0x0391, 0x03A9 + 1) if cp != 0x03A2}


def value(word):
    """Isopsephy of a word; None if any character has no value."""
    total = 0
    for ch in word:
        if ch not in GEMATRIA:
            return None
        total += GEMATRIA[ch]
    return total


def name(cp):
    return unicodedata.name(chr(cp), "<unnamed>")


def main():
    # source gematria, keyed by (book, ch, v, word-index-within-verse)
    src_val, seen = {}, {}
    with open(RAW / "livinggreeknt_new.tsv", encoding="utf-8") as fh:
        for r in list(csv.reader(fh, delimiter="\t"))[1:]:
            if len(r) > 9 and r[7]:
                key = (r[0], int(r[1]), int(r[2]))
                seen[key] = seen.get(key, 0) + 1
                src_val[(r[0], int(r[1]), int(r[2]), seen[key])] = r[9]

    # NRV OSIS -> source label, to look the value back up
    osis_to_label = {
        "Matt": "Matt", "Mark": "Mark", "Luke": "Luke", "John": "John",
        "Acts": "Acts", "Rom": "Rom", "1Cor": "1 Cor", "2Cor": "2 Cor",
        "Gal": "Gal", "Eph": "Eph", "Phil": "Phlp", "Col": "Col",
        "1Thess": "1 Ths", "2Thess": "2 Ths", "1Tim": "1 Tim",
        "2Tim": "2 Tim", "Titus": "Titus", "Phlm": "Phlm", "Heb": "Heb",
        "Jas": "James", "1Pet": "1 Pet", "2Pet": "2 Pet", "1John": "1 John",
        "2John": "2 John", "3John": "3 John", "Jude": "Jude", "Rev": "Rev",
    }

    proposed, rejected = [], []
    for f in sorted((TEXTS / "Greek/NRV").glob("NRV.*.jsonl")):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            bad = [(i, ord(c)) for i, c in enumerate(r["raw"])
                   if ord(c) not in ALLOWED_NRV]
            if not bad:
                continue

            corrected, changes, unmapped = r["raw"], [], False
            for i, cp in bad:
                if cp not in HOMOGLYPH:
                    unmapped = True
                    continue
                changes.append({"pos": i, "from": f"U+{cp:04X}",
                                "from_name": name(cp),
                                "to": f"U+{HOMOGLYPH[cp]:04X}",
                                "to_name": name(HOMOGLYPH[cp])})
            corrected = "".join(chr(HOMOGLYPH.get(ord(c), ord(c)))
                                for c in r["raw"])

            label = osis_to_label[r["book"]]
            recorded = src_val.get((label, r["ch"], r["v"], r["wi"]))
            got = value(corrected)
            entry = {"id": r["id"], "book": r["book"], "ch": r["ch"],
                     "v": r["v"], "wi": r["wi"], "stored": r["raw"],
                     "corrected": corrected, "changes": changes,
                     "source_gematria": recorded,
                     "corrected_gematria": got}

            if unmapped:
                entry["reason"] = "codepoint has no known homoglyph mapping"
                rejected.append(entry)
            elif recorded is None or got is None or str(got) != str(recorded):
                entry["reason"] = (f"gematria mismatch: corrected form computes "
                                   f"{got}, source records {recorded}")
                rejected.append(entry)
            else:
                entry["proof"] = (f"corrected form computes {got}, matching the "
                                  f"source's own recorded value; the stored "
                                  f"form cannot, since its characters carry no "
                                  f"Greek numeric value")
                proposed.append(entry)

    doc = {
        "purpose": "Corrections for upstream character defects. `raw` in "
                   "Raw_Texts is never edited; these are applied at load.",
        "policy": "A correction is proposed only for a character outside the "
                  "edition's allowed set that is a known visual homoglyph, and "
                  "only when the corrected word reproduces the source's own "
                  "recorded gematria. audit_texts.py re-verifies independently.",
        "homoglyph_map": {f"U+{k:04X}": f"U+{v:04X}"
                          for k, v in sorted(HOMOGLYPH.items())},
        "corrections": proposed,
        "rejected": rejected,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print(f"{'id':22s} {'stored':16s} {'corrected':16s} {'value':>6s}  proof")
    print("-" * 78)
    for e in proposed:
        print(f"{e['id']:22s} {e['stored']:16s} {e['corrected']:16s} "
              f"{e['corrected_gematria']:>6}  gematria matches source")
    for e in rejected:
        print(f"{e['id']:22s} {e['stored']:16s} {'-':16s} {'':>6}  "
              f"REJECTED: {e['reason']}")
    print("-" * 78)
    print(f"{len(proposed)} proposed, {len(rejected)} rejected -> {OUT.name}")
    return not rejected


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
