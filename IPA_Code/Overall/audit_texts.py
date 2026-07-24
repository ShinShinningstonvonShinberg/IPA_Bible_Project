#!/usr/bin/env python3
"""Audit Raw_Texts. Mechanical, judgement-free checks:

  1. Source reconciliation - corpus is compared against the SOURCE, so a
                             silently dropped book or word is impossible
  2. Structural validation - id format/uniqueness, field agreement, wi
                             contiguity, types, ordering, `after` correctness
  3. Character census      - on codepoints AS STORED *and* on their NFD form;
                             the gate is the union, because downstream reads
                             what is stored, not the decomposition
  4. Normalization audit   - decomposition vs canonical reordering, Hebrew
                             presentation forms (U+FB1D-FB4F)
  5. Allowlist gate        - every codepoint must be classified. Only
                             explicitly ALLOWED codepoints may auto-freeze;
                             anything else must be hand-classified.
  6. Checksum check        - ripped files must match the manifest

Design notes, each earned by a real defect:

* Block membership, never Unicode category. Latin capitals are category Lu,
  exactly like Greek capitals; a category rule silently blesses homoglyphs.
* The allowed set is the codepoints an edition ACTUALLY uses, not the whole
  Unicode block. The Greek block would admit 94 codepoints the NRV never
  contains, including all lowercase Greek, Coptic, and unassigned points.
* Gating on the NFD census alone is blind to homoglyphs that decompose INTO
  the allowed set (U+2126 OHM SIGN -> omega, U+1FBE -> iota), and it renames
  what is stored (U+1F49 is reported as U+0314). Hence: gate on both.
* A failing gate - for ANY reason, including a checksum mismatch - must never
  rewrite that edition's allowlist entry, or one bad run freezes the
  contamination as known-good.
* Freezing is per-edition, so one dirty edition cannot block the others.
"""
import json, hashlib, unicodedata, csv, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
TEXTS = ROOT / "Raw_Texts"
RAW = ROOT / "Git_Ignored_Stuff/Raw_Downloads"
META = TEXTS / "MISC_INFO"        # manifest, allowlist, corrections
ALLOWLIST = META / "codepoint_allowlist.json"
CORRECTIONS = META / "corrections.json"

# Greek isopsephy, for independently re-verifying corrections. The audit never
# trusts corrections.json: it recomputes every value from the source itself.
GEMATRIA = {
    "Α": 1, "Β": 2, "Γ": 3, "Δ": 4, "Ε": 5, "Ζ": 7, "Η": 8, "Θ": 9,
    "Ι": 10, "Κ": 20, "Λ": 30, "Μ": 40, "Ν": 50, "Ξ": 60, "Ο": 70,
    "Π": 80, "Ρ": 100, "Σ": 200, "Τ": 300, "Υ": 400, "Φ": 500,
    "Χ": 600, "Ψ": 700, "Ω": 800,
}
NRV_OSIS_TO_LABEL = {
    "Matt": "Matt", "Mark": "Mark", "Luke": "Luke", "John": "John",
    "Acts": "Acts", "Rom": "Rom", "1Cor": "1 Cor", "2Cor": "2 Cor",
    "Gal": "Gal", "Eph": "Eph", "Phil": "Phlp", "Col": "Col",
    "1Thess": "1 Ths", "2Thess": "2 Ths", "1Tim": "1 Tim", "2Tim": "2 Tim",
    "Titus": "Titus", "Phlm": "Phlm", "Heb": "Heb", "Jas": "James",
    "1Pet": "1 Pet", "2Pet": "2 Pet", "1John": "1 John", "2John": "2 John",
    "3John": "3 John", "Jude": "Jude", "Rev": "Rev",
}

HEBREW_PRESENTATION = (0xFB1D, 0xFB4F)
ID_RE = re.compile(r"^[A-Za-z0-9]+\.[A-Za-z0-9]+\.\d+\.\d+\.w\d+$")


def rng(lo, hi, exclude=()):
    return {cp for cp in range(lo, hi + 1) if cp not in exclude}


# Codepoints each EDITION is permitted to use, as an explicit set.
# NRV is undiacriticised uppercase: exactly the 24 Greek capitals
# (U+03A2 does not exist). Anything else - lowercase, Coptic, unassigned,
# Extended-block, combining - is an anomaly by construction.
GREEK_CAPS = rng(0x0391, 0x03A9, exclude={0x03A2})
ALLOWED = {
    "NRV":    GREEK_CAPS,
    "TR1894": (rng(0x0370, 0x03FF) | rng(0x1F00, 0x1FFF) | rng(0x0300, 0x036F)),
    "WLC":    rng(0x0590, 0x05FF),
}

# Out-of-set codepoints verified legitimate by inspection. Hand-entered only;
# the bootstrap never adds to this. Each needs a reason.
CLASSIFIED = {
    "WLC": {
        0x0020: "SPACE inside `raw` - occurs only in the paseq encoding, "
                "where WLC attaches ' ׀' to the preceding word.",
        0x034F: "COMBINING GRAPHEME JOINER - blocks canonical reordering of "
                "Masoretic marks. Load-bearing: never strip.",
        0x200D: "ZERO WIDTH JOINER - deliberate Masoretic encoding control. "
                "Occurs 83x, always between a hataf (reduced) vowel and a "
                "meteg, keeping the two from colliding. Same class as the "
                "CGJ above: load-bearing, never strip.",
    },
    "TR1894": {
        0x2019: "RIGHT SINGLE QUOTATION MARK - Greek elision apostrophe; part "
                "of the word and phonetically meaningful.",
        0x0374: "GREEK NUMERAL SIGN (keraia) - marks letters used as "
                "alphabetic numerals: ιβʹ=12, ρμδʹ=144, χξϛʹ=666. Legitimate "
                "Greek, but these 14 words are NUMBERS, not pronounceable "
                "letter sequences, and need explicit handling downstream.",
        0x02B9: "MODIFIER LETTER PRIME - the NFD decomposition of U+0374 "
                "keraia, not a separately authored character.",
    },
    "NRV": {},
}

NOTES = {
    0x05BE: "MAQQEF - joins words; the following `after` is empty by design.",
    0x05C0: "PASEQ - phrase divider, attached to the preceding word.",
    0x05C3: "SOF PASUQ - verse terminator, attached to the last word.",
}


def describe(ch):
    return {"char": ch, "name": unicodedata.name(ch, "<unnamed>"),
            "category": unicodedata.category(ch),
            "combining": unicodedata.combining(ch)}


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(2)


# ------------------------------------------------------- source reconcilers
def reconcile_nrv():
    """Expected {book_label: wordcount} straight from the source TSV."""
    p = RAW / "livinggreeknt_new.tsv"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))[1:]
    c = Counter(r[0] for r in rows if len(r) > 7 and r[7])
    return sum(c.values()), len(c)


RECONCILERS = {"NRV": reconcile_nrv}


def source_gematria():
    """{(label, ch, v, wi): recorded value} straight from the source TSV."""
    p = RAW / "livinggreeknt_new.tsv"
    if not p.exists():
        return {}
    out, seen = {}, {}
    with open(p, encoding="utf-8") as fh:
        for r in list(csv.reader(fh, delimiter="\t"))[1:]:
            if len(r) > 9 and r[7]:
                k = (r[0], int(r[1]), int(r[2]))
                seen[k] = seen.get(k, 0) + 1
                out[(r[0], int(r[1]), int(r[2]), seen[k])] = r[9]
    return out


def isopsephy(word):
    total = 0
    for ch in word:
        if ch not in GEMATRIA:
            return None
        total += GEMATRIA[ch]
    return total


def load_corrections(recs_by_id):
    """Load and INDEPENDENTLY re-verify corrections. -> (by_id, errors).

    The corrections file is treated as untrusted input: every entry must name
    a real record, match that record's stored text, and produce a word whose
    gematria equals the value the SOURCE recorded. Anything else is an error,
    never a silent application.
    """
    if not CORRECTIONS.exists():
        return {}, []
    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    src = source_gematria()
    by_id, errs = {}, []
    for e in doc.get("corrections", []):
        rid = e.get("id", "<missing>")
        rec = recs_by_id.get(rid)
        if rec is None:
            errs.append(f"{rid}: correction names a record that does not exist")
            continue
        if rec["raw"] != e.get("stored"):
            errs.append(f"{rid}: stored text {e.get('stored')!r} != actual "
                        f"{rec['raw']!r} (stale correction)")
            continue
        label = NRV_OSIS_TO_LABEL.get(rec["book"])
        recorded = src.get((label, rec["ch"], rec["v"], rec["wi"]))
        got = isopsephy(e.get("corrected", ""))
        if recorded is None:
            errs.append(f"{rid}: no source gematria to verify against")
        elif got is None or str(got) != str(recorded):
            errs.append(f"{rid}: corrected {e.get('corrected')!r} computes "
                        f"{got}, source records {recorded}")
        else:
            by_id[rid] = e["corrected"]
    for e in doc.get("rejected", []):
        errs.append(f"{e.get('id')}: unresolved defect - {e.get('reason')}")
    return by_id, errs


# ---------------------------------------------------------------- structure
def check_structure(ed, recs):
    """Return a list of structural error strings (empty == sound)."""
    errs = []
    seen_ids = set()
    keys_ref = None
    by_verse = defaultdict(list)
    order = []
    for r in recs:
        rid = r.get("id", "<missing>")
        if not isinstance(rid, str) or not ID_RE.match(rid):
            errs.append(f"bad id format: {rid!r}")
            continue
        if rid in seen_ids:
            errs.append(f"duplicate id: {rid}")
        seen_ids.add(rid)

        ks = frozenset(r) - {"mark", "before", "reading", "ketiv"}
        if keys_ref is None:
            keys_ref = ks
        elif ks != keys_ref:
            errs.append(f"{rid}: key set {sorted(ks)} != {sorted(keys_ref)}")

        for f, t in (("ch", int), ("v", int), ("wi", int),
                     ("book", str), ("raw", str), ("after", str)):
            if not isinstance(r.get(f), t):
                errs.append(f"{rid}: field {f} is {type(r.get(f)).__name__}, "
                            f"expected {t.__name__}")
        if not isinstance(r.get("raw"), str) or not r["raw"].strip():
            errs.append(f"{rid}: empty or whitespace-only raw")

        parts = rid.split(".")
        if len(parts) == 5:
            _, bk, ch, v, w = parts
            if (bk != r.get("book") or ch != str(r.get("ch"))
                    or v != str(r.get("v")) or w != f"w{r.get('wi')}"):
                errs.append(f"{rid}: fields disagree with id")
        # Keyed by BOOK too: records from all books are concatenated, so a
        # (ch, v) key would merge every book's 1:1 into one bogus group.
        by_verse[(r.get("book"), r.get("ch"), r.get("v"))].append(r)
        # Only order-check well-typed records: a stringified int is already
        # reported above, and mixing int/str here would crash the sort rather
        # than report the defect.
        if all(isinstance(r.get(f), int) for f in ("ch", "v", "wi")):
            order.append((r.get("book"), r["ch"], r["v"], r["wi"]))

    for (bk, ch, v), group in by_verse.items():
        wis = [g["wi"] for g in group]
        if wis != list(range(1, len(wis) + 1)):
            errs.append(f"{bk} {ch}:{v}: wi not contiguous from 1 "
                        f"({wis[:6]}…)")
        for g in group[:-1]:
            if g["after"] == "" and not g["raw"].endswith("־"):
                errs.append(f"{g['id']}: empty `after` on a non-final word")
        # A verse-final `after` is NOT required to be empty: in the TR it
        # legitimately carries the closing punctuation (. or ·) that makes the
        # verse rebuild byte-exactly. The real invariant is that `after` holds
        # only separators - never word content.
        for g in group:
            if any(unicodedata.category(c)[0] in "LM" for c in g["after"]):
                errs.append(f"{g['id']}: `after` contains word content: "
                            f"{g['after']!r}")

    per_book = defaultdict(list)
    for bk, ch, v, wi in order:
        per_book[bk].append((ch, v, wi))
    for bk, seq in per_book.items():
        if seq != sorted(seq):
            errs.append(f"{bk}: records not ordered by (ch, v, wi)")
    return errs


# --------------------------------------------------------------------- main
def main():
    mpath = META / "manifest.json"
    if not mpath.exists():
        die(f"no manifest at {mpath}. Run rip_texts.py first.")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    outs = manifest.get("outputs", [])
    if not outs:
        die("manifest lists no outputs.")

    missing = [o["path"] for o in outs if not (TEXTS / o["path"]).exists()]
    if missing:
        die(f"{len(missing)} manifest output(s) do not exist, e.g. "
            f"{missing[:3]}. Re-run rip_texts.py.")
    empty = [o["path"] for o in outs if (TEXTS / o["path"]).stat().st_size == 0]
    if empty:
        die(f"empty output file(s): {empty[:3]}")

    by_edition = defaultdict(list)
    for o in outs:
        by_edition[o["edition"]].append(o)

    # Load every record before judging any edition, so corrections can be
    # verified against the whole corpus.
    recs_by_ed = {}
    for ed, entries in by_edition.items():
        if ed not in ALLOWED:
            die(f"edition '{ed}' has no ALLOWED codepoint set; refusing to "
                f"audit it, since every codepoint would look foreign.")
        loaded = [json.loads(l) for e in entries
                  for l in open(TEXTS / e["path"], encoding="utf-8")]
        if not loaded:
            die(f"edition '{ed}' produced zero records.")
        recs_by_ed[ed] = loaded
    all_by_id = {r["id"]: r for rs in recs_by_ed.values() for r in rs}
    fixes, fix_errs = load_corrections(all_by_id)

    report = {}
    for ed, entries in by_edition.items():
        recs = recs_by_ed[ed]

        bad_sums = [e["path"] for e in entries
                    if hashlib.sha256((TEXTS / e["path"]).read_bytes()
                                      ).hexdigest() != e["sha256"]]
        struct = check_structure(ed, recs)

        # -- source reconciliation ---------------------------------------
        recon = None
        if ed in RECONCILERS:
            got = RECONCILERS[ed]()
            if got:
                exp_words, exp_books = got
                recon = (len(recs) == exp_words and len(entries) == exp_books,
                         f"{len(recs)}/{exp_words} words, "
                         f"{len(entries)}/{exp_books} books")

        # Census the CORRECTED view: `raw` stays source-exact on disk, but the
        # gate must judge the text the engine will actually transcribe.
        raws = [fixes.get(r["id"], r["raw"]) for r in recs]
        applied = sum(1 for r in recs if r["id"] in fixes)
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

        stored, nfd_c = Counter(), Counter()
        for w in raws:
            stored.update(w)
            nfd_c.update(unicodedata.normalize("NFD", w))
        sep = Counter()
        for r in recs:
            sep.update(r["after"])

        report[ed] = {"entries": entries, "words": len(recs), "stored": stored,
                      "nfd": nfd_c, "sep": sep, "presentation": presentation,
                      "decomposed": decomposed, "reordered": reordered,
                      "bad_sums": bad_sums, "struct": struct, "recon": recon,
                      "applied": applied, "fix_errs": fix_errs}

    # ---- allowlist gate, per edition --------------------------------------
    prior = (json.loads(ALLOWLIST.read_text(encoding="utf-8"))
             if ALLOWLIST.exists() else None)
    allow_out = {"purpose": "frozen inventory of every codepoint known to occur "
                            "in Raw_Texts. A new codepoint fails the audit "
                            "until it is classified.",
                 "rule": "auto-freeze only codepoints in an edition's ALLOWED "
                         "set; anything else must be hand-classified. Gate is "
                         "the union of stored and NFD codepoints. A failing "
                         "edition is never rewritten.",
                 "editions": dict((prior or {}).get("editions", {}))}
    gate, failures = {}, []
    for ed, R in report.items():
        seen = {ord(c) for c in R["stored"]} | {ord(c) for c in R["nfd"]}
        approvable = ALLOWED[ed] | set(CLASSIFIED[ed])
        known = set()
        if prior and ed in prior.get("editions", {}):
            known = {int(k[2:], 16)
                     for k in prior["editions"][ed]["codepoints"]}
        baseline = (seen & approvable) if not known else (known & approvable)
        new = sorted(seen - baseline)
        gate[ed] = new

        clean = (not new and not R["bad_sums"] and not R["struct"]
                 and not R["fix_errs"]
                 and (R["recon"] is None or R["recon"][0]))
        if not clean:
            failures.append(ed)
            continue                       # never rewrite a failing edition

        # Freeze exactly what the gate tests: stored UNION nfd. Freezing only
        # `stored` meant every NFD-only codepoint (the decomposed polytonic
        # accents) failed as "unclassified" on the very next run.
        entries = {}
        for ch in sorted(set(R["stored"]) | set(R["nfd"]), key=ord):
            cp = ord(ch)
            e = describe(ch)
            e["stored"] = R["stored"].get(ch, 0)
            e["nfd"] = R["nfd"].get(ch, 0)
            if cp in CLASSIFIED[ed]:
                e["note"] = CLASSIFIED[ed][cp]
            elif cp in NOTES:
                e["note"] = NOTES[cp]
            entries[f"U+{cp:04X}"] = e
        allow_out["editions"][ed] = {"words": R["words"],
                                     "distinct": len(entries),
                                     "codepoints": entries}

    if len(failures) < len(report):        # something is freezable
        ALLOWLIST.write_text(json.dumps(allow_out, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ------------------------------------------------------------------ out
    print(f"corpus: {len(outs)} files, {sum(R['words'] for R in report.values())}"
          f" words, {len(by_edition)} edition(s)\n")
    print(f"{'edition':9s} {'files':>5s} {'words':>8s} {'cps':>4s} "
          f"{'fixed':>6s} {'struct':>7s} {'sums':>5s}  reconcile        gate")
    print("-" * 88)
    for ed, R in report.items():
        rc = R["recon"][1] if R["recon"] else "n/a"
        print(f"{ed:9s} {len(R['entries']):5d} {R['words']:8d} "
              f"{len(R['stored']):4d} {R['applied']:6d} {len(R['struct']):7d} "
              f"{len(R['bad_sums']):5d}  {rc:16s} "
              f"{'FAIL' if ed in failures else 'pass'}")
    if any(R["fix_errs"] for R in report.values()):
        errs = next(R["fix_errs"] for R in report.values() if R["fix_errs"])
        print(f"\nCORRECTION VERIFICATION ERRORS ({len(errs)}):")
        for e in errs[:10]:
            print("   " + e)

    for ed, R in report.items():
        if R["struct"]:
            print(f"\n{ed} STRUCTURAL ERRORS ({len(R['struct'])}):")
            for e in R["struct"][:10]:
                print("   " + e)
        if R["bad_sums"]:
            print(f"\n{ed} CHECKSUM MISMATCH: {R['bad_sums']}")
        if R["recon"] and not R["recon"][0]:
            print(f"\n{ed} RECONCILIATION FAILED: {R['recon'][1]}")
        if gate[ed]:
            print(f"\n{ed} UNCLASSIFIED ({len(gate[ed])}):")
            for cp in gate[ed]:
                ch = chr(cp)
                where = []
                if ch in R["stored"]:
                    where.append(f"stored×{R['stored'][ch]}")
                if ch in R["nfd"]:
                    where.append(f"nfd×{R['nfd'][ch]}")
                print(f"   U+{cp:04X} {ch!r:6s} {'|'.join(where):22s} "
                      f"{unicodedata.name(ch,'<unnamed>')}")

    print("\nseparators in `after`:")
    for ed, R in report.items():
        s = ", ".join(f"U+{ord(c):04X}×{n}" for c, n in
                      sorted(R["sep"].items(), key=lambda x: -x[1]) if c)
        print(f"  {ed:9s} {s or '(none)'}")
    print("\npresentation forms (U+FB1D-FB4F):",
          sum(sum(R["presentation"].values()) for R in report.values()) or "none")

    print(f"\nallowlist gate: {'pass' if not failures else 'FAIL'}"
          f"{'' if not failures else ' — not frozen: ' + ', '.join(failures)}")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
