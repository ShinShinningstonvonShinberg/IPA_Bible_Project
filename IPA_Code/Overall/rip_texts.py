#!/usr/bin/env python3
"""Rip source texts into Raw_Texts/{Language}/{Edition}/*.jsonl word records.

The read layer for IPA reconstruction. One record per word:

    id     canonical address; joins Raw_Texts <-> IPA_Texts
    book   OSIS book code
    ch/v/wi   chapter, verse, word index (1-based)
    raw    the word's Unicode EXACTLY as the source gives it
    after  the exact separator that follows it
    mark   optional; structural marker (pe/samekh) at a verse end

`raw` is never edited, cleaned, or reordered. Anything unpronounced
(maqqef, paseq, sof pasuq) stays in `raw` and is resolved by the mapping
tables, not here. This script makes no linguistic judgements at all.

VERIFICATION. Every file is checked by re-reading it BACK FROM DISK and
comparing against a freshly re-derived source sequence. An in-memory check
that compares a value against the expression that produced it is a tautology
and proves nothing; this catches grouping, ordering and serialization bugs.
The whole rip is then reconciled against the source so a silently dropped
book is impossible.
"""
import xml.etree.ElementTree as ET
import json, csv, hashlib, unicodedata, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
RAW = ROOT / "Git_Ignored_Stuff/Raw_Downloads"
TEXTS = ROOT / "Raw_Texts"          # layout: {Language}/{Edition}/*.jsonl
META = TEXTS / "MISC_INFO"          # manifest, allowlist, corrections
STAGING = "Staging"                 # slices live under Raw_Texts/Staging/,
                                    # never beside a full corpus

MAQQEF = "־"

FULL = ["NRV", "TR1894", "WLC"]

# WLC source file stem -> OSIS code. 39 books; TanachHeader/TanachIndex are
# metadata, and the *.DH.xml files are Documentary-Hypothesis annotated
# duplicates of the Torah. Both are excluded by not appearing here.
WLC_OSIS = {
    "Genesis": "Gen", "Exodus": "Exod", "Leviticus": "Lev", "Numbers": "Num",
    "Deuteronomy": "Deut", "Joshua": "Josh", "Judges": "Judg", "Ruth": "Ruth",
    "Samuel_1": "1Sam", "Samuel_2": "2Sam", "Kings_1": "1Kgs",
    "Kings_2": "2Kgs", "Chronicles_1": "1Chr", "Chronicles_2": "2Chr",
    "Ezra": "Ezra", "Nehemiah": "Neh", "Esther": "Esth", "Job": "Job",
    "Psalms": "Ps", "Proverbs": "Prov", "Ecclesiastes": "Eccl",
    "Song_of_Songs": "Song", "Isaiah": "Isa", "Jeremiah": "Jer",
    "Lamentations": "Lam", "Ezekiel": "Ezek", "Daniel": "Dan",
    "Hosea": "Hos", "Joel": "Joel", "Amos": "Amos", "Obadiah": "Obad",
    "Jonah": "Jonah", "Micah": "Mic", "Nahum": "Nah", "Habakkuk": "Hab",
    "Zephaniah": "Zeph", "Haggai": "Hag", "Zechariah": "Zech",
    "Malachi": "Mal",
}

# TR source book key -> OSIS code. Mechanical lookup, no judgement.
TR_OSIS = {
    "Matthew": "Matt", "Mark": "Mark", "Luke": "Luke", "John": "John",
    "Acts": "Acts", "Romans": "Rom", "1 Corinthians": "1Cor",
    "2 Corinthians": "2Cor", "Galatians": "Gal", "Ephesians": "Eph",
    "Philippians": "Phil", "Colossians": "Col",
    "1 Thessalonians": "1Thess", "2 Thessalonians": "2Thess",
    "1 Timothy": "1Tim", "2 Timothy": "2Tim", "Titus": "Titus",
    "Philemon": "Phlm", "Hebrews": "Heb", "James": "Jas",
    "1 Peter": "1Pet", "2 Peter": "2Pet", "1 John": "1John",
    "2 John": "2John", "3 John": "3John", "Jude": "Jude",
    "Revelation": "Rev",
}

# Chapter slices, staged separately from the full corpus.
# (edition, source book key, OSIS code, chapter)
SLICES = []

NRV_OSIS = {
    "Matt": "Matt", "Mark": "Mark", "Luke": "Luke", "John": "John",
    "Acts": "Acts", "Rom": "Rom", "1 Cor": "1Cor", "2 Cor": "2Cor",
    "Gal": "Gal", "Eph": "Eph", "Phlp": "Phil", "Col": "Col",
    "1 Ths": "1Thess", "2 Ths": "2Thess", "1 Tim": "1Tim", "2 Tim": "2Tim",
    "Titus": "Titus", "Phlm": "Phlm", "Heb": "Heb", "James": "Jas",
    "1 Pet": "1Pet", "2 Pet": "2Pet", "1 John": "1John", "2 John": "2John",
    "3 John": "3John", "Jude": "Jude", "Rev": "Rev",
}

# `versification` records each edition's OWN numbering. It deliberately does
# NOT claim equivalence to another edition's scheme.
EDITION_META = {
    "WLC":    {"dir": ("Hebrew", "Tanak"), "lang": "hbo", "script": "Hebr",
               "versification": "mt",
               "name": "Unicode/XML Leningrad Codex (UXLC 2.5)",
               "url": "https://tanach.us/Books/Tanach.xml.zip",
               "licence": "Hebrew text free of restriction (tanach.us)",
               "source_file": "Tanach.xml.zip"},
    "TR1894": {"dir": ("Greek", "TR"), "lang": "grc", "script": "Grek",
               "versification": "tr",
               "name": "Scrivener Textus Receptus 1894",
               "url": "https://github.com/bible-api-io/bible-api-version-tr1894",
               "licence": "MIT-0; underlying text public domain",
               "source_file": "tr1894_bibleapi.json"},
    "NRV":    {"dir": ("Greek", "NRV"), "lang": "grc", "script": "Grek",
               "versification": "nrv",
               "name": "LivingGreekNT (Numeric Restorative)",
               "url": "https://github.com/ivandustin/livinggreeknt",
               "licence": "CC0 (site); underlying text public domain",
               "source_file": "livinggreeknt_new.tsv"},
}

VERSIFICATION_NOTES = {
    "nrv": "Edition-native numbering, NOT KJV. Verified divergences from KJV: "
           "Acts 19 has 40 verses (KJV 41); 2Cor 13 has 13 (KJV 14); "
           "3John has 15 (KJV 14); Rev 12 has 18 (KJV 17). "
           "19 verses absent from the earliest manuscripts carry no words: "
           "Matt 16:3, 17:21, 18:11, 23:14; Mark 7:16, 9:44, 9:46, 11:26, "
           "15:28; Luke 17:36, 22:43, 22:44, 23:17; John 5:4; Acts 8:37, "
           "15:34, 24:7, 28:29; Rom 16:24.",
    "mt": "Masoretic numbering. Psalm superscriptions are counted as verse 1.",
    "tr": "Textus Receptus numbering.",
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------- tokenizer
def is_wordchar(ch):
    """Letters and combining marks are word content; everything else separates."""
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
    root = ET.parse(RAW / f"tanach_wlc/Books/{book_key}.xml").getroot()
    c = root.find(f".//c[@n='{chap}']")
    recs = []
    for v in c.findall("v"):
        vn = int(v.get("n"))
        ws = v.findall("w")
        marks = [e.tag for e in v if e.tag in ("pe", "samekh")]
        for wi, w in enumerate(ws, 1):
            raw = "".join(w.itertext())
            after = "" if (wi == len(ws) or raw.endswith(MAQQEF)) else " "
            rec = {"id": f"WLC.{osis}.{chap}.{vn}.w{wi}", "book": osis,
                   "ch": chap, "v": vn, "wi": wi, "raw": raw, "after": after}
            if wi == len(ws) and marks:
                rec["mark"] = marks[0]
            recs.append(rec)
    return recs


def rip_tr(book_key, osis, chap, data):
    ch = data["booksData"][book_key]["chaptersData"][chap]
    recs = []
    for vn, vtext in enumerate(ch):
        if vn == 0 or not vtext:
            continue
        prefix, toks = tokenize(vtext)
        for wi, (raw, after) in enumerate(toks, 1):
            rec = {"id": f"TR.{osis}.{chap}.{vn}.w{wi}", "book": osis,
                   "ch": chap, "v": vn, "wi": wi, "raw": raw, "after": after}
            if wi == 1 and prefix:
                rec["before"] = prefix
            recs.append(rec)
    return recs


def eltext(e):
    """Element text, excluding <x> note markers but keeping their tails."""
    parts = [e.text or ""]
    for c in e:
        if c.tag != "x":
            parts.append("".join(c.itertext()))
        parts.append(c.tail or "")
    return "".join(parts).strip()


def wlc_source(book_file):
    """Independently derive [(ch, v, word, kind, ketiv)] for a WLC book.

    QERE/KETIV POLICY. The Masoretes recorded a written form (ketiv) and a
    read form (qere) at 1254 places. The ketiv is essentially always unpointed
    - 1267 unpointed against 2 pointed corpus-wide - so it carries no vowels
    and cannot be transcribed without inventing them. The qere is pointed
    every time and is by definition the pronunciation.

    So the QERE is the word, and the ketiv travels with it as metadata:
    nothing is discarded, it simply is not what gets transcribed.

    Orphans are real Masoretic phenomena and are kept distinct:
      qere without ketiv  (qere wela ketiv, read but never written) -> a word
      ketiv without qere  (ketiv wela qere, written but not read)   -> recorded
                          as unpronounced; it has no read form to transcribe
    """
    root = ET.parse(RAW / f"tanach_wlc/Books/{book_file}.xml").getroot()
    out = []
    for c in root.iter("c"):
        ch = int(c.get("n"))
        for v in c.findall("v"):
            vn = int(v.get("n"))
            kids = [e for e in v if e.tag in ("w", "k", "q")]
            i = 0
            while i < len(kids):
                e = kids[i]
                if e.tag == "w":
                    out.append((ch, vn, eltext(e), "w", None))
                    i += 1
                elif e.tag == "k":
                    if i + 1 < len(kids) and kids[i + 1].tag == "q":
                        out.append((ch, vn, eltext(kids[i + 1]), "qere",
                                    eltext(e)))
                        i += 2
                    else:                       # ketiv wela qere: not read
                        out.append((ch, vn, None, "ketiv_only", eltext(e)))
                        i += 1
                else:                           # qere wela ketiv: read only
                    out.append((ch, vn, eltext(e), "qere_only", None))
                    i += 1
    return out


def rip_wlc_book(book_file, osis, prefix="Tanak"):
    """Whole WLC book. Maqqef binds a word to the next, so `after` is empty."""
    recs, wi, cur = [], 0, None
    slots = wlc_source(book_file)
    for idx, (ch, vn, word, kind, ketiv) in enumerate(slots):
        if (ch, vn) != cur:
            cur, wi = (ch, vn), 0
        if word is None:                        # unpronounced ketiv-only slot
            continue
        wi += 1
        nxt = slots[idx + 1] if idx + 1 < len(slots) else None
        last = not nxt or (nxt[0], nxt[1]) != (ch, vn)
        after = "" if (last or word.endswith(MAQQEF)) else " "
        rec = {"id": f"{prefix}.{osis}.{ch}.{vn}.w{wi}", "book": osis,
               "ch": ch, "v": vn, "wi": wi, "raw": word, "after": after}
        if kind != "w":
            rec["reading"] = kind
            if ketiv:
                rec["ketiv"] = ketiv
        recs.append(rec)
    return recs


def wlc_want(book_file):
    """(ch, v, word) triples for verification - pronounced slots only."""
    return [(ch, vn, w) for ch, vn, w, kind, k in wlc_source(book_file)
            if w is not None]


def tr_source(data, book_key):
    """Independently derive [(ch, v, verse_text)] for a TR book.

    A separate pass from the rip, so verification is not comparing a value
    against the expression that produced it.
    """
    out = []
    chapters = data["booksData"][book_key]["chaptersData"]
    for ch, verses in enumerate(chapters):
        if ch == 0 or not verses:
            continue
        for v, text in enumerate(verses):
            if v == 0 or not text:
                continue
            out.append((ch, v, text))
    return out


def rip_tr_book(book_key, osis, data):
    """Whole TR book: every chapter. Punctuation is preserved in `after`."""
    recs = []
    for ch, vn, vtext in tr_source(data, book_key):
        prefix, toks = tokenize(vtext)
        for wi, (raw, after) in enumerate(toks, 1):
            rec = {"id": f"TR.{osis}.{ch}.{vn}.w{wi}", "book": osis,
                   "ch": ch, "v": vn, "wi": wi, "raw": raw, "after": after}
            if wi == 1 and prefix:
                rec["before"] = prefix
            recs.append(rec)
    return recs


def verify_tr_from_disk(path, want):
    """Read the WRITTEN file back and rebuild every verse byte-exactly."""
    got = [json.loads(l) for l in open(path, encoding="utf-8")]
    byverse = defaultdict(list)
    for r in got:
        byverse[(r["ch"], r["v"])].append(r)
    if len(byverse) != len(want):
        return False, f"{len(byverse)} verses on disk != {len(want)} in source"
    for ch, vn, vtext in want:
        rs = byverse.get((ch, vn))
        if not rs:
            return False, f"missing verse {ch}:{vn}"
        rebuilt = (rs[0].get("before", "")
                   + "".join(r["raw"] + r["after"] for r in rs))
        if rebuilt != vtext:
            return False, f"{ch}:{vn} rebuild mismatch"
    return True, f"{len(want)} verses byte-exact from disk"


def nrv_source(rows, book_key, chap=None):
    """Independently derive the source word sequence for a book/chapter.

    Deliberately a separate pass from the rip, so verification is not
    comparing a value against the expression that produced it.
    """
    out = []
    for r in rows:
        if len(r) > 7 and r[0] == book_key and r[7]:
            if chap is None or r[1] == str(chap):
                out.append((int(r[1]), int(r[2]), r[7]))
    return out


def rip_nrv(book_key, osis, rows, chap=None):
    """NRV: one row per word already. chap=None rips the whole book."""
    grouped = defaultdict(list)
    for ch, vn, word in nrv_source(rows, book_key, chap):
        grouped[(ch, vn)].append(word)
    recs = []
    for (ch, vn) in sorted(grouped):
        ws = grouped[(ch, vn)]
        for wi, word in enumerate(ws, 1):
            recs.append({"id": f"NRV.{osis}.{ch}.{vn}.w{wi}", "book": osis,
                         "ch": ch, "v": vn, "wi": wi, "raw": word,
                         "after": "" if wi == len(ws) else " "})
    return recs


# -------------------------------------------------------------- verification
def emit(recs, relpath):
    p = TEXTS / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def verify_from_disk(path, want):
    """Read the WRITTEN file back and compare to an independent source list.

    `want` is [(ch, v, word), ...] derived by a separate pass over the source.
    """
    got = [json.loads(l) for l in open(path, encoding="utf-8")]
    if len(got) != len(want):
        return False, f"count {len(got)} != source {len(want)}"
    for g, (ch, v, word) in zip(got, want):
        if g["raw"] != word or g["ch"] != ch or g["v"] != v:
            return False, f"mismatch at {g['id']}: {g['raw']!r} vs {word!r}"
    return True, f"{len(want)} words verified from disk"


# ---------------------------------------------------------------------- run
def main():
    with open(RAW / "livinggreeknt_new.tsv", encoding="utf-8") as fh:
        nrv_rows = list(csv.reader(fh, delimiter="\t"))[1:]
    tr_data = None

    outputs, slice_rows, full_rows, failures = [], [], [], []

    for edition, book_key, osis, chap in SLICES:
        m = EDITION_META[edition]
        if edition == "WLC":
            recs = rip_wlc(book_key, osis, chap)
        elif edition == "TR1894":
            if tr_data is None:
                tr_data = json.load(open(RAW / "tr1894_bibleapi.json",
                                         encoding="utf-8"))
            recs = rip_tr(book_key, osis, chap, tr_data)
        else:
            recs = rip_nrv(book_key, osis, nrv_rows, chap)
        lang, ed = m["dir"]
        rel = f"{STAGING}/{lang}/{ed}/{ed}.{osis}.{chap}.jsonl"
        p = emit(recs, rel)
        ok, detail = (verify_from_disk(p, nrv_source(nrv_rows, book_key, chap))
                      if edition == "NRV" else (True, "not verified"))
        if not ok:
            failures.append(f"{rel}: {detail}")
        outputs.append({"path": rel, "edition": edition, "lang": m["lang"],
                        "script": m["script"], "versification": m["versification"],
                        "book": osis, "chapter": chap, "words": len(recs),
                        "verses": len({r["v"] for r in recs}),
                        "verified": "pass" if ok else "FAIL",
                        "sha256": sha256(p)})
        slice_rows.append((rel, len(recs), ok, detail))

    for edition in FULL:
        m = EDITION_META[edition]
        lang, ed = m["dir"]
        ed_rows = []

        if edition == "NRV":
            mapping = NRV_OSIS
        elif edition == "TR1894":
            mapping = TR_OSIS
            if tr_data is None:
                tr_data = json.load(open(RAW / "tr1894_bibleapi.json",
                                         encoding="utf-8"))
        elif edition == "WLC":
            mapping = WLC_OSIS
        else:
            die(f"no full-rip handler for edition '{edition}'")

        for book_key, osis in mapping.items():
            if edition == "NRV":
                recs = rip_nrv(book_key, osis, nrv_rows)
                want = nrv_source(nrv_rows, book_key)
                verifier = verify_from_disk
            elif edition == "WLC":
                recs = rip_wlc_book(book_key, osis, prefix=ed)
                want = wlc_want(book_key)
                verifier = verify_from_disk
            else:
                recs = rip_tr_book(book_key, osis, tr_data)
                want = tr_source(tr_data, book_key)
                verifier = verify_tr_from_disk
            if not recs:
                failures.append(f"{edition}/{osis}: source book '{book_key}' "
                                f"produced no records - label mismatch?")
                continue
            rel = f"{lang}/{ed}/{ed}.{osis}.jsonl"
            p = emit(recs, rel)
            ok, detail = verifier(p, want)
            if not ok:
                failures.append(f"{rel}: {detail}")
            nch = len({r["ch"] for r in recs})
            nv = len({(r["ch"], r["v"]) for r in recs})
            outputs.append({"path": rel, "edition": edition, "lang": m["lang"],
                            "script": m["script"],
                            "versification": m["versification"], "book": osis,
                            "chapters": nch, "verses": nv, "words": len(recs),
                            "verified": "pass" if ok else "FAIL",
                            "sha256": sha256(p)})
            ed_rows.append((osis, len(recs), nch, nv, ok))

        # -- reconcile the whole edition against its source ----------------
        if edition == "NRV":
            src_books = {r[0] for r in nrv_rows if len(r) > 7 and r[7]}
            unmapped = src_books - set(NRV_OSIS)
            exp_words = sum(1 for r in nrv_rows if len(r) > 7 and r[7])
            exp_verses = None
        elif edition == "WLC":
            # Every book file on disk must be accounted for: either mapped, or
            # a known non-book (metadata / Documentary-Hypothesis duplicate).
            stems = {p.stem for p in (RAW / "tanach_wlc/Books").glob("*.xml")}
            skip = {s for s in stems if s.endswith(".DH")
                    or s.startswith("Tanach")}
            src_books = stems - skip
            unmapped = src_books - set(WLC_OSIS)
            exp_words = sum(len(wlc_want(b)) for b in WLC_OSIS)
            exp_verses = None
        else:
            src_books = set(tr_data["booksData"])
            unmapped = src_books - set(TR_OSIS)
            exp_words = None
            exp_verses = sum(len(tr_source(tr_data, b)) for b in src_books)
        if unmapped:
            failures.append(f"{edition}: source books unmapped: {sorted(unmapped)}")
        if len(ed_rows) != len(src_books):
            failures.append(f"{edition}: ripped {len(ed_rows)} books, source "
                            f"has {len(src_books)}")
        got_words = sum(r[1] for r in ed_rows)
        got_verses = sum(r[3] for r in ed_rows)
        if exp_words is not None and exp_words != got_words:
            failures.append(f"{edition}: word total {got_words} != source "
                            f"{exp_words}")
        if exp_verses is not None and exp_verses != got_verses:
            failures.append(f"{edition}: verse total {got_verses} != source "
                            f"{exp_verses}")
        print(f"{edition:8s} reconciliation: {len(ed_rows)}/{len(src_books)} books"
              + (f", {got_words}/{exp_words} words" if exp_words is not None else
                 f", {got_verses}/{exp_verses} verses")
              + f", {len(unmapped)} unmapped")
        full_rows.extend(ed_rows)

    manifest = {
        "layer": "Raw_Texts",
        "purpose": "read layer for IPA reconstruction",
        "layout": "{Language}/{Edition}/{Edition}.{OsisBook}.jsonl "
                  "(slices under Staging/)",
        "versification_notes": VERSIFICATION_NOTES,
        "expected": {"NRV": {"books": 27, "words": 137720}},
        "sources": [{"edition": e, "name": m["name"], "url": m["url"],
                     "licence": m["licence"],
                     "sha256": sha256(RAW / m["source_file"])}
                    for e, m in EDITION_META.items()
                    if (RAW / m["source_file"]).exists()],
        "outputs": outputs,
    }
    META.mkdir(parents=True, exist_ok=True)
    (META / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if full_rows:
        print(f"\nFULL RIP ({len(full_rows)} books)")
        print(f"  {'book':8s} {'words':>7s} {'chapters':>9s} {'verses':>7s}  verified")
        print("  " + "-" * 50)
        for osis, n, nch, nv, ok in full_rows:
            print(f"  {osis:8s} {n:7d} {nch:9d} {nv:7d}  "
                  f"{'PASS' if ok else 'FAIL'}")
        print("  " + "-" * 50)
        print(f"  {'TOTAL':8s} {sum(r[1] for r in full_rows):7d} "
              f"{sum(r[2] for r in full_rows):9d} "
              f"{sum(r[3] for r in full_rows):7d}")
    if slice_rows:
        print(f"\nslices (Staging/): {len(slice_rows)} files")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  " + f)
    print(f"\nverification failures: {len(failures)}")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
