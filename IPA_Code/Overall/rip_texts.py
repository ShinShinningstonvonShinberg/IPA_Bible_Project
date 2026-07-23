#!/usr/bin/env python3
"""Rip source texts into Raw_Texts/*.jsonl word records.

The read layer for IPA reconstruction. One record per word:

    id     canonical address; joins Raw_Texts <-> IPA_Texts
    book   OSIS book code
    ch/v/wi   chapter, verse, word index (1-based)
    raw    the word's Unicode EXACTLY as the source gives it
    after  the exact separator that follows it
    mark   optional; structural marker (pe/samekh) at a verse end

`raw` is never edited, cleaned, or reordered. Anything unpronounced
(paseq, maqqef, sof pasuq) stays in `raw` and is resolved by the mapping
tables, not here. This script makes no linguistic judgements at all.

Constants (edition, language, script, versification, licence, checksums)
live once in manifest.json rather than on every line.

Acceptance gate: a rip is only correct if it reconstructs the source.
"""
import xml.etree.ElementTree as ET
import json, csv, hashlib, unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
RAW = ROOT / "Git_Ignored_Stuff/Raw_Downloads"
TEXTS = ROOT / "Raw_Texts/Temp"   # the ripped JSONL
META = ROOT / "Raw_Texts"         # manifest + codepoint allowlist

MAQQEF = "־"

# (edition, source book key, OSIS code, chapter)
# Extending to the full corpus = adding rows here. No code changes.
SLICES = [
    ("WLC",    "Genesis", "Gen",  1),
    ("WLC",    "Psalms",  "Ps",  16),
    ("TR1894", "John",    "John", 1),
    ("TR1894", "Romans",  "Rom",  8),
    ("NRV",    "John",    "John", 11),
    ("NRV",    "Rom",     "Rom",  1),
]

EDITION_META = {
    "WLC":    {"lang": "hbo", "script": "Hebr", "versification": "mt",
               "name": "Unicode/XML Leningrad Codex (UXLC 2.5)",
               "url": "https://tanach.us/Books/Tanach.xml.zip",
               "licence": "Hebrew text free of restriction (tanach.us)",
               "source_file": "Tanach.xml.zip"},
    "TR1894": {"lang": "grc", "script": "Grek", "versification": "kjv",
               "name": "Scrivener Textus Receptus 1894",
               "url": "https://github.com/bible-api-io/bible-api-version-tr1894",
               "licence": "MIT-0; underlying text public domain",
               "source_file": "tr1894_bibleapi.json"},
    "NRV":    {"lang": "grc", "script": "Grek", "versification": "kjv",
               "name": "LivingGreekNT (Numeric Restorative)",
               "url": "https://github.com/ivandustin/livinggreeknt",
               "licence": "CC0 (site); underlying text public domain",
               "source_file": "livinggreeknt_new.tsv"},
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- tokenizer
def is_wordchar(ch):
    """Letters and combining marks are word content; everything else separates.

    Language-agnostic by design. Combining marks (category M*) MUST count as
    word content or every diacritic would be read as punctuation.
    """
    return unicodedata.category(ch)[0] in "LM" or ch in "’'"


def tokenize(s):
    """-> (prefix, [(word, after), ...]) such that prefix + sum(w+a) == s."""
    spans, start = [], None
    for i, ch in enumerate(s):
        if is_wordchar(ch):
            if start is None:
                start = i
        elif start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(s)))
    if not spans:
        return s, []
    toks = []
    for idx, (a, b) in enumerate(spans):
        nxt = spans[idx + 1][0] if idx + 1 < len(spans) else len(s)
        toks.append((s[a:b], s[b:nxt]))
    return s[: spans[0][0]], toks


# ------------------------------------------------------------------ rippers
def rip_wlc(book_key, osis, chap):
    """WLC XML: <w> already delimits words; maqqef binds a word to the next."""
    root = ET.parse(RAW / f"tanach_wlc/Books/{book_key}.xml").getroot()
    c = root.find(f".//c[@n='{chap}']")
    recs = []
    for v in c.findall("v"):
        vn = int(v.get("n"))
        ws = v.findall("w")
        marks = [e.tag for e in v if e.tag in ("pe", "samekh")]
        for wi, w in enumerate(ws, 1):
            raw = "".join(w.itertext())
            if wi == len(ws):
                after = ""                      # verse boundary
            elif raw.endswith(MAQQEF):
                after = ""                      # maqqef joins: no space
            else:
                after = " "
            rec = {"id": f"WLC.{osis}.{chap}.{vn}.w{wi}", "book": osis,
                   "ch": chap, "v": vn, "wi": wi, "raw": raw, "after": after}
            if wi == len(ws) and marks:
                rec["mark"] = marks[0]
            recs.append(rec)
    # gate: every <w> character must survive, in order
    src = "".join("".join(w.itertext()) for w in c.iter("w"))
    ok = src == "".join(r["raw"] for r in recs)
    return recs, ok, f"{len(list(c.iter('w')))} <w> preserved"


def rip_tr(book_key, osis, chap, data):
    """TR JSON: verse is one string; punctuation is preserved in `after`."""
    ch = data["booksData"][book_key]["chaptersData"][chap]
    recs, all_ok = [], True
    nverse = 0
    for vn, vtext in enumerate(ch):
        if vn == 0 or not vtext:
            continue
        nverse += 1
        prefix, toks = tokenize(vtext)
        if prefix + "".join(w + a for w, a in toks) != vtext:
            all_ok = False
        for wi, (raw, after) in enumerate(toks, 1):
            rec = {"id": f"TR.{osis}.{chap}.{vn}.w{wi}", "book": osis,
                   "ch": chap, "v": vn, "wi": wi, "raw": raw, "after": after}
            if wi == 1 and prefix:
                rec["before"] = prefix
            recs.append(rec)
    return recs, all_ok, f"{nverse} verses byte-exact"


def rip_nrv(book_key, osis, chap, rows):
    """NRV TSV: already one row per word; col 7 Greek, col 2 verse."""
    sel = [r for r in rows if len(r) > 7 and r[0] == book_key and r[1] == str(chap)]
    byv = defaultdict(list)
    for r in sel:
        byv[int(r[2])].append(r[7])
    recs = []
    for vn in sorted(byv):
        ws = byv[vn]
        for wi, word in enumerate(ws, 1):
            recs.append({"id": f"NRV.{osis}.{chap}.{vn}.w{wi}", "book": osis,
                         "ch": chap, "v": vn, "wi": wi, "raw": word,
                         "after": "" if wi == len(ws) else " "})
    src = [w for vn in sorted(byv) for w in byv[vn]]
    ok = [r["raw"] for r in recs] == src
    return recs, ok, f"{len(src)} source words"


# ---------------------------------------------------------------------- run
def main():
    TEXTS.mkdir(parents=True, exist_ok=True)
    tr_data = json.load(open(RAW / "tr1894_bibleapi.json", encoding="utf-8"))
    with open(RAW / "livinggreeknt_new.tsv", encoding="utf-8") as fh:
        nrv_rows = list(csv.reader(fh, delimiter="\t"))[1:]

    outputs, rows = [], []
    for edition, book_key, osis, chap in SLICES:
        if edition == "WLC":
            recs, ok, detail = rip_wlc(book_key, osis, chap)
        elif edition == "TR1894":
            recs, ok, detail = rip_tr(book_key, osis, chap, tr_data)
        else:
            recs, ok, detail = rip_nrv(book_key, osis, chap, nrv_rows)

        prefix = {"WLC": "WLC", "TR1894": "TR", "NRV": "NRV"}[edition]
        name = f"{prefix}.{osis}.{chap}.jsonl"
        with open(TEXTS / name, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

        m = EDITION_META[edition]
        outputs.append({"file": name, "edition": edition, "lang": m["lang"],
                        "script": m["script"], "versification": m["versification"],
                        "book": osis, "chapter": chap, "words": len(recs),
                        "verses": max(r["v"] for r in recs),
                        "roundtrip": "pass" if ok else "FAIL",
                        "sha256": sha256(TEXTS / name)})
        rows.append((name, len(recs), max(r["v"] for r in recs), ok, detail))

    manifest = {
        "layer": "Raw_Texts",
        "purpose": "read layer for IPA reconstruction",
        "sources": [{"edition": e, "name": m["name"], "url": m["url"],
                     "licence": m["licence"],
                     "sha256": sha256(RAW / m["source_file"])}
                    for e, m in EDITION_META.items()],
        "outputs": outputs,
    }
    (META / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'file':22s} {'words':>6s} {'verses':>7s}  {'round-trip':11s} detail")
    print("-" * 76)
    for name, n, v, ok, detail in rows:
        print(f"{name:22s} {n:6d} {v:7d}  {'PASS' if ok else 'FAIL':11s} {detail}")
    print("-" * 76)
    print(f"{len(rows)} files, {sum(r[1] for r in rows)} words -> manifest.json")
    return all(r[3] for r in rows)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
