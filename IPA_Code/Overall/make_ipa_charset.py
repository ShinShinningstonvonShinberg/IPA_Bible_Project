#!/usr/bin/env python3
"""Build IPA_Code/Overall/ipa_charset.jsonl - the IPA-side character allowlist.

The twin of Raw_Texts/MISC_INFO/codepoint_allowlist.json: that one bounds what
may appear in a source text, this one bounds what may appear in emitted IPA.

Sources, in order of authority:
  1. The IPA's own i-chart symbol database (CC BY-SA). Supplies the canonical
     symbol set, official IPA Numbers, Unicode codepoints, and - crucially -
     the `NonIPA` field, in which the IPA itself records which non-IPA
     characters impersonate each symbol.
  2. Python `unicodedata` + Unicode Blocks.txt. Authoritative for names,
     categories and block membership. Block membership, never Unicode
     category, decides script identity: Latin and Greek capitals are both
     category Lu, so a category rule silently blesses homoglyphs.
  3. PanPhon (MIT) for attestation - does the symbol actually occur in a
     real segment inventory, or is it chart-only?

PHOIBLE is deliberately NOT used. Its licence is stated inconsistently across
four official channels (CC-BY-SA 3.0 / GPL-3.0 / CC-BY-4.0 / CC-BY 3.0), and
share-alike or copyleft would contaminate this MIT repo. It is also unusable
for Ancient Greek and Biblical Hebrew, which it does not contain.

status is taken from the IPA's OWN categorisation by IPA Number band:
    100s consonants   300s vowels      400s diacritics
    500s suprasegmentals              900s delimiters      -> allow
    200s retired / non-IPA            600s extIPA          -> quarantine
Quarantined symbols are RECORDED, not dropped. An anomaly you can name is a
five-minute fix; an anomaly you cannot name is a silent mispronunciation.
"""
import json, re, csv, html, unicodedata, sys
from collections import Counter
from pathlib import Path

ROOT = Path("/Users/Shared/IPA_Bible_Project")
QUAR = ROOT / "Git_Ignored_Stuff/Raw_Downloads/ipa_charset"
IPAINFO = ROOT / "Git_Ignored_Stuff/IPA_Information"
OUT = ROOT / "IPA_Code/Overall/ipa_charset.jsonl"
META = ROOT / "IPA_Code/Overall/ipa_charset.meta.json"

# Rendering artefacts the chart wraps around symbols so diacritics display
# standalone. Neither is part of any symbol; both must be stripped before the
# string is treated as IPA.
DOTTED_CIRCLE = "◌"          # U+25CC placeholder base
CGJ = "͏"               # COMBINING GRAPHEME JOINER, used as a spacer
ARTEFACTS = str.maketrans("", "", DOTTED_CIRCLE + CGJ)
BAND = {100: "consonant", 200: "retired", 300: "vowel", 400: "diacritic",
        500: "suprasegmental", 600: "extipa", 700: "capital", 900: "delimiter"}
ALLOW_BANDS = {"consonant", "vowel", "diacritic", "suprasegmental", "delimiter"}


def load_blocks():
    out = []
    for line in open(QUAR / "Blocks.txt", encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            rng, name = line.split(";")
            lo, hi = rng.split("..")
            out.append((int(lo, 16), int(hi, 16), name.strip()))
    return out


BLOCKS = load_blocks()


def blk(cp):
    for lo, hi, n in BLOCKS:
        if lo <= cp <= hi:
            return n
    return "Unassigned"


def uname(ch):
    return unicodedata.name(ch, "<unnamed>")


def seq(s):
    return [f"U+{ord(c):04X}" for c in s]


def fld(rec, name):
    m = re.search(name + r'\s*:\s*"((?:[^"\\]|\\.)*)"', rec)
    if m:
        return m.group(1)
    m = re.search(name + r'\s*:\s*(-?\d+)', rec)
    return m.group(1) if m else ""


def parse_confusables(raw):
    """`NonIPA` field -> list of distinct impostor strings.

    Format: alternatives separated by ':::' (font-variant renderings of the
    same thing), entries separated by ',', each followed by '(n)' citation
    indices which we drop - the impostor characters themselves are what
    matter, and we classify them by Unicode block ourselves.
    """
    out = []
    for entry in re.split(r",(?![^()]*\))", raw):
        entry = re.sub(r"\([\d,\s]*\)", "", entry).strip()
        if not entry:
            continue
        for alt in entry.split(":::"):
            alt = html.unescape(alt).translate(ARTEFACTS).strip()
            if alt and alt not in out:
                out.append(alt)
    return out


def main():
    src = (QUAR / "ichart_js/arrays.js").read_text(encoding="utf-8-sig",
                                                   errors="replace")
    chunks = src.split("{SID:")[1:]
    if not chunks:
        print("FATAL: could not parse the i-chart database", file=sys.stderr)
        return False

    # PanPhon attestation: which codepoints occur in a real segment inventory
    pp_cps = set()
    pp_path = IPAINFO / "panphon_ipa_all.csv"
    if pp_path.exists():
        with open(pp_path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pp_cps.update(ord(c) for c in row["ipa"])

    recs, skipped = [], 0
    for c in chunks:
        # The chart stores delimiters as HTML entities (&#91; = '['). Decode
        # first, or the entity's own digits and punctuation get mistaken for
        # phonetic content.
        sym = html.unescape(fld(c, "Symbol")).translate(ARTEFACTS)
        ipa_no = fld(c, "IPA_No")
        if not sym:
            skipped += 1
            continue

        digits = re.sub(r"\D", "", ipa_no)
        band = BAND.get(int(digits) // 100 * 100, "unnumbered") if digits \
            else "unnumbered"
        status = "allow" if band in ALLOW_BANDS else "quarantine"

        sym = unicodedata.normalize("NFD", sym)
        cps = [ord(ch) for ch in sym]
        rec = {
            "seq": seq(sym),
            "char": sym,
            "n_cp": len(cps),
            "ipa_no": int(digits) if digits else None,
            "band": band,
            "status": status,
            "descr": fld(c, "Descr") or None,
            "ipa_name": fld(c, "IPA_Name") or None,
            "u_names": [uname(ch) for ch in sym],
            "blocks": sorted({blk(cp) for cp in cps}),
            "categories": sorted({unicodedata.category(ch) for ch in sym}),
            "attested_panphon": all(cp in pp_cps for cp in cps) if pp_cps else None,
        }
        conf = parse_confusables(fld(c, "NonIPA"))
        if conf:
            rec["confusables"] = [
                {"char": x, "seq": seq(x),
                 "blocks": sorted({blk(ord(ch)) for ch in x})}
                for x in conf]
        recs.append(rec)

    # de-duplicate on the normalized sequence, keeping the first (lowest SID)
    seen, uniq = set(), []
    for r in recs:
        k = tuple(r["seq"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    dupes = len(recs) - len(uniq)

    # ---- pivot from SYMBOL records to CODEPOINT records ------------------
    # The charset must bound which *characters* may appear in emitted IPA.
    # An IPA Number belongs to a symbol, not a codepoint: the chart lists
    # length as the composite `eː`, so keying on symbols would leave the bare
    # `ː` with no row at all. A codepoint is legal if it occurs in ANY symbol
    # the IPA has not explicitly retired or consigned to extIPA.
    # A codepoint is legal only if it occurs in at least one symbol the IPA has
    # actually NUMBERED in an allowed band. Chart furniture - footnote markers
    # (Ⓒ Ⓕ Ⓟ), bare digits, legend punctuation, Private Use Area font hacks -
    # never appears in a numbered symbol, so this separates it cleanly from
    # genuine suprasegmentals like | ‖ . ‿ which are numbered delimiters.
    REJECT = {"retired", "extipa"}
    cp_symbols, cp_ok, cp_bad, cp_seen, cp_self = {}, set(), set(), set(), {}
    for r in uniq:
        numbered_ok = bool(r["ipa_no"]) and r["band"] not in REJECT
        numbered_bad = bool(r["ipa_no"]) and r["band"] in REJECT
        for ch in r["char"]:
            cp_symbols.setdefault(ch, []).append(r["char"])
            cp_seen.add(ch)
            if numbered_ok:
                cp_ok.add(ch)
            elif numbered_bad:
                cp_bad.add(ch)
        if r["n_cp"] == 1 and r["ipa_no"] and r["band"] not in REJECT:
            cp_self.setdefault(r["char"], r)

    # Private Use Area can never be legal IPA: a PUA codepoint means whatever
    # one particular font says it means, so it is unportable by definition.
    # The chart uses U+E003 for IPA#490 (release/burst) for want of a real
    # assignment; we quarantine it rather than emit an unportable character.
    def is_pua(cp):
        return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD

    cp_recs = []
    for ch in sorted(cp_seen, key=ord):
        cp = ord(ch)
        allowed = ch in cp_ok and not is_pua(cp)
        self_rec = cp_self.get(ch)
        syms = cp_symbols[ch]
        rec = {
            "cp": f"U+{cp:04X}",
            "char": ch,
            "status": "allow" if allowed else "quarantine",
            "u_name": uname(ch),
            "block": blk(cp),
            "category": unicodedata.category(ch),
            "combining": unicodedata.combining(ch),
            "role": ("mark" if unicodedata.category(ch)[0] == "M"
                     else "modifier" if unicodedata.category(ch) == "Lm"
                     else "letter" if unicodedata.category(ch)[0] == "L"
                     else "other"),
            "used_in_symbols": len(syms),
            "example_symbols": syms[:4],
            "attested_panphon": (cp in pp_cps) if pp_cps else None,
        }
        if self_rec:
            rec["ipa_no"] = self_rec["ipa_no"]
            rec["band"] = self_rec["band"]
            rec["descr"] = self_rec["descr"]
            rec["ipa_name"] = self_rec["ipa_name"]
        if not allowed:
            rec["reason"] = ("Private Use Area - font-specific, unportable"
                             if is_pua(cp) else
                             "occurs only in retired/extIPA symbols"
                             if ch in cp_bad else
                             "never occurs in a numbered IPA symbol "
                             "(chart furniture: footnote marker, legend "
                             "punctuation, digit, or font artefact)")
        conf = []
        for r in uniq:
            if r["char"] == ch:
                conf = r.get("confusables", [])
                break
        if conf:
            rec["confusables"] = conf
        cp_recs.append(rec)

    with open(OUT, "w", encoding="utf-8") as fh:
        for r in cp_recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    conf_chars = Counter()
    for r in cp_recs:
        for c_ in r.get("confusables", []):
            for ch in c_["char"]:
                if ch.isalpha() or unicodedata.combining(ch):
                    conf_chars[ch] += 1
    uniq_symbols, uniq = uniq, cp_recs

    meta = {
        "file": OUT.name,
        "purpose": "IPA-side character allowlist. Bounds what may appear in "
                   "emitted IPA, as codepoint_allowlist.json bounds what may "
                   "appear in a source text.",
        "normalization": "NFD. Lossless for PanPhon (0 collisions) and the "
                         "safe canonical form for inspecting diacritics.",
        "tie_bar": "Affricates are written with U+0361 COMBINING DOUBLE "
                   "INVERTED BREVE. Measured: PanPhon with tie bars has 0/6367 "
                   "ambiguously-segmentable entries; without them 1246/6367; "
                   "PHOIBLE's tie-bar-free convention 1094/3175. Only U+0361 "
                   "makes IPA strings uniquely decodable, which a deterministic "
                   "tokenizer and a reversible mapping both require.",
        "status_rule": "Bands taken from the IPA's own IPA Number ranges. "
                       "Quarantined entries are recorded, never dropped.",
        "sources": [
            {"name": "IPA i-chart symbol database",
             "url": "https://www.internationalphoneticassociation.org/IPAcharts/IPA_charts_TI/",
             "licence": "CC BY-SA",
             "attribution": "IPA Chart, http://www.internationalphoneticassociation.org/content/ipa-chart, "
                            "available under a Creative Commons Attribution-Sharealike "
                            "License. Copyright © International Phonetic Association.",
             "note": "Only factual symbol/codepoint/number mappings are "
                     "extracted; no chart layout or design is reproduced."},
            {"name": "Unicode Character Database", "via": "python unicodedata + Blocks.txt",
             "url": "https://www.unicode.org/Public/UNIDATA/Blocks.txt"},
            {"name": "PanPhon", "licence": "MIT", "role": "attestation only",
             "url": "https://github.com/dmort27/panphon"},
        ],
        "excluded": {
            "PHOIBLE": "Deliberately unused. Licence stated inconsistently "
                       "across four official channels (CC-BY-SA 3.0 / GPL-3.0 / "
                       "CC-BY-4.0 / CC-BY 3.0); share-alike or copyleft would "
                       "contaminate this MIT repo. Also lacks Ancient Greek "
                       "(grc) and Biblical Hebrew (hbo) entirely."},
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    allow = [r for r in uniq if r["status"] == "allow"]
    quar = [r for r in uniq if r["status"] == "quarantine"]
    print(f"symbols parsed   : {len(uniq_symbols)} (dupes collapsed {dupes})")
    print(f"CODEPOINTS written: {len(uniq)}   allow {len(allow)} / "
          f"quarantine {len(quar)}")

    print(f"\n{'role':12s} {'allow':>6s} {'quar':>6s}")
    print("-" * 28)
    for role in ("letter", "mark", "modifier", "other"):
        a = sum(1 for r in allow if r["role"] == role)
        q = sum(1 for r in quar if r["role"] == role)
        if a or q:
            print(f"{role:12s} {a:6d} {q:6d}")

    print("\nallowed codepoints by block:")
    for b, n in Counter(r["block"] for r in allow).most_common(10):
        print(f"   {b:36s} {n}")

    print(f"\ncodepoints carrying confusables : "
          f"{sum(1 for r in uniq if 'confusables' in r)}")
    print(f"distinct impostor characters    : {len(conf_chars)}")
    print("impostors by block:")
    for b, n in Counter(blk(ord(c)) for c in conf_chars).most_common(6):
        print(f"   {b:36s} {n}")

    need = {"θ": "Greek theta", "x": "chi", "ʃ": "shin", "ɬ": "sin",
            "ħ": "het", "ʕ": "ayin", "ʔ": "aleph", "q": "qof",
            "ð": "dalet-spirant", "β": "bet-spirant", "ɣ": "gimel-spirant",
            "y": "front upsilon", "ː": "length", "ˈ": "primary stress",
            "ˌ": "secondary stress", "͡": "tie bar"}
    have = {r["char"] for r in allow}
    miss = [f"{k} ({v})" for k, v in need.items() if k not in have]
    print(f"\nGreek/Hebrew essentials in ALLOW: {len(need)-len(miss)}/{len(need)}")
    print("   MISSING:", ", ".join(miss) if miss else "none")
    return not miss


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
