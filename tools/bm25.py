#!/usr/bin/env python3
"""A lexical recall channel over the parsed full text.

WHY THIS EXISTS, AND WHAT IT IS NOT FOR
The retrieval eval splits its misses cleanly in two. Seven gold papers were
retrieved by cosine and then demoted by the re-rank -- that is a ranking
problem, and rank fusion fixes it. The other seven were never retrieved at
all, at cosine ranks 633, 878, 2889, 3293, 5124, 8166 and 10589. No amount of
re-ranking reaches those, and widening ASK_RECALL to 10,589 is not retrieval,
it is scanning the archive.

Look at what those questions actually ask:

    "Which cross-asset futures study uses Barchart end-of-day data"
    "Which paper uses a G-10 panel of nine developed bond markets"

Both name something concrete, and both name it in the METHOD -- the data
vendor, the sample construction. The semantic index embeds title and abstract
only, and an abstract does not name its data vendor. The words are in the
paper; they are simply not in the part of it we vectorised. That is a recall
failure an embedding cannot fix, because the text never reached the embedder.

So this indexes the body. It is deliberately a SECOND channel, fused with
cosine rather than replacing it: BM25 is exact-term matching and will do
nothing whatsoever for the vocab tier, whose whole difficulty is that the
question and the paper share no vocabulary at all. Fusion is the point --
each channel covers the other's blind spot.

COVERAGE IS PARTIAL AND THAT IS FINE. 2,381 of 20,999 papers have parsed full
text. BM25 can only ever return those, which is why it fuses with a channel
that spans everything rather than gating it.

    python tools/bm25.py build          # docs/bm25.bin + docs/bm25.json
    python tools/bm25.py query "barchart end of day futures"

FORMAT. Mirrors tools/graph.py's edges.bin: a magic, fixed-width records, and
the index width carried in the header so it widens rather than truncating.
The browser holds the whole file the way it already holds vec.bin.

    magic   "QBM1"    4 bytes
    ndocs   uint32
    nterms  uint32
    avgdl   float32
    width   uint8     docid bytes; 2 while ndocs < 65536
    pad     3 bytes
    termblob_len  uint32
    postings_len  uint32
    -- then --
    termblob       UTF-8, terms concatenated, sorted bytewise
    term_off       uint32[nterms+1]   into termblob
    post_off       uint32[nterms+1]   into postings, in RECORDS
    postings       record: docid[width] tf:uint8
    doclen         uint16[ndocs]      token count, clamped

Sorted terms so a reader binary-searches without building a hash map over
115,000 entries on every page load.
"""

import argparse
import collections
import glob
import io
import json
import math
import pathlib
import re
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

DOCS = pathlib.Path("docs")
FT = DOCS / "ft"

# How common a term may be and still be indexed, as a fraction of the corpus.
#
# THIS WAS 0.126 AND IT WAS WRONG, in a way worth recording. The reasoning was
# that BM25's IDF at df/N = 0.126 is already small and goes negative above N/2,
# so 2,978 terms carrying 68% of the postings could go for a rounding error of
# ranking quality. That is true of RANKING and false of MATCHING. Measured on
# the fulltext tier, it removed every usable term from two questions --
#
#   "...sorts portfolios by their forward discount..."  -> 1 term survived
#   "...a G-10 panel of nine developed bond markets..." -> 0 terms survived
#
# -- so BM25 could not score them at all, and both stayed missed. A question
# made entirely of ordinary words is exactly the case where the index must
# still be able to intersect them. IDF already discounts common terms; doing it
# again by deletion is not a second opinion, it is a hole.
DF_MAX_FRAC = 1.0
# df == 1 is KEPT deliberately. A term appearing in exactly one paper is the
# highest-IDF, most discriminative thing in the index, and "Barchart" is
# exactly such a term. Pruning singletons is the obvious size win and it would
# remove precisely the vocabulary this channel exists to match.
DF_MIN = 1

# Mirrors portal.js ASK_STOP so the query side tokenises identically. A term
# dropped here that the query keeps is simply never found.
STOP = set((
    'the a an of and or to in on for with is are be as by at from that this what which how does do did why '
    'when we our their its it also than then these those between across over under more most less least there here into out '
    'about after before during any some all can could would should may might will shall must have has had been being'
).split())

TOKEN = re.compile(r'[a-z0-9]+')


def log(m):
    print(m, flush=True)


def tokenise(text):
    """The one tokeniser. Both sides of a search must agree exactly."""
    return [t for t in TOKEN.findall(str(text or '').lower())
            if len(t) > 2 and t not in STOP]


# ------------------------------------------------------------------- build
def build(args):
    files = sorted(glob.glob(str(FT / "*.json")))
    if not files:
        raise SystemExit(
            "[bm25] docs/ft is empty. It lives in R2 -- run "
            "`python tools/dbsync.py ftpull` first, the way prepare_deploy does.")
    log(f"[bm25] {len(files):,} full-text files")

    uids, doclen = [], []
    tf_by_doc = []
    df = collections.Counter()
    for i, f in enumerate(files):
        try:
            d = json.load(io.open(f, encoding='utf-8'))
        except Exception as e:                                   # noqa: BLE001
            log(f"[bm25] skipping {f}: {e}")
            continue
        uid = d.get("uid")
        toks = tokenise(' '.join(p.get('t', '') for p in d.get('p', [])))
        if not uid or not toks:
            continue
        counts = collections.Counter(toks)
        uids.append(uid)
        doclen.append(len(toks))
        tf_by_doc.append(counts)
        df.update(counts.keys())
        if i and i % 500 == 0:
            log(f"[bm25]   {i:,}/{len(files):,} -- {len(df):,} terms so far")

    ndocs = len(uids)
    if not ndocs:
        raise SystemExit("[bm25] no document produced any tokens")
    df_max = max(2, int(ndocs * getattr(args, "df_max_frac", DF_MAX_FRAC)))
    keep = sorted(t for t, n in df.items() if DF_MIN <= n <= df_max)
    kept = set(keep)
    log(f"[bm25] {ndocs:,} docs, {len(df):,} terms, keeping {len(keep):,} "
        f"with df in [{DF_MIN},{df_max}]")

    # postings, grouped by term, docids ascending
    by_term = collections.defaultdict(list)
    for doc_i, counts in enumerate(tf_by_doc):
        for t, n in counts.items():
            if t in kept:
                by_term[t].append((doc_i, min(255, n)))

    width = 2 if ndocs < 65536 else 4
    dfmt = "<H" if width == 2 else "<I"
    termblob = bytearray()
    term_off = [0]
    post_off = [0]
    postings = bytearray()
    nrec = 0
    for t in keep:
        termblob += t.encode("utf-8")
        term_off.append(len(termblob))
        for doc_i, n in by_term[t]:
            postings += struct.pack(dfmt, doc_i) + struct.pack("<B", n)
            nrec += 1
        post_off.append(nrec)

    avgdl = sum(doclen) / float(ndocs)
    head = (b"QBM1" + struct.pack("<IIfB3x", ndocs, len(keep), avgdl, width)
            + struct.pack("<II", len(termblob), nrec))
    body = (bytes(termblob)
            + struct.pack("<%dI" % len(term_off), *term_off)
            + struct.pack("<%dI" % len(post_off), *post_off)
            + bytes(postings)
            + struct.pack("<%dH" % ndocs, *[min(65535, x) for x in doclen]))
    out = pathlib.Path(getattr(args, "out", None) or DOCS)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bm25.bin").write_bytes(head + body)
    (out / "bm25.json").write_text(json.dumps({
        "n": ndocs, "nterms": len(keep), "avgdl": round(avgdl, 2),
        "postings": nrec, "uids": uids}), encoding="utf-8")
    total = len(head) + len(body)
    log(f"[bm25] wrote {out}/bm25.bin -- {nrec:,} postings, {total/1e6:.2f} MB "
        f"({total/max(nrec,1):.1f} b/posting), avgdl {avgdl:,.0f}")
    return 0


# -------------------------------------------------------------------- read
class Bm25:
    """Reader shared by eval/run.py and, later, the browser's mirror of it."""

    K1 = 1.2
    B = 0.75

    def __init__(self, docs=DOCS):
        docs = pathlib.Path(docs)
        raw = (docs / "bm25.bin").read_bytes()
        meta = json.loads((docs / "bm25.json").read_text(encoding="utf-8"))
        if raw[:4] != b"QBM1":
            raise SystemExit("[bm25] docs/bm25.bin has the wrong magic")
        ndocs, nterms, avgdl, width = struct.unpack_from("<IIfB3x", raw, 4)
        tb_len, nrec = struct.unpack_from("<II", raw, 20)
        o = 28
        self.termblob = raw[o:o + tb_len]; o += tb_len
        self.term_off = struct.unpack_from("<%dI" % (nterms + 1), raw, o)
        o += 4 * (nterms + 1)
        self.post_off = struct.unpack_from("<%dI" % (nterms + 1), raw, o)
        o += 4 * (nterms + 1)
        self.rec = width + 1
        self.postings = raw[o:o + nrec * self.rec]; o += nrec * self.rec
        self.doclen = struct.unpack_from("<%dH" % ndocs, raw, o)
        self.n, self.nterms, self.avgdl, self.width = ndocs, nterms, avgdl, width
        self.uids = meta["uids"]
        self.dfmt = "<H" if width == 2 else "<I"

    def _find(self, term):
        """Bytewise binary search over the sorted term blob."""
        key = term.encode("utf-8")
        lo, hi = 0, self.nterms - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            t = self.termblob[self.term_off[mid]:self.term_off[mid + 1]]
            if t == key:
                return mid
            if t < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1

    def postings_for(self, term):
        i = self._find(term)
        if i < 0:
            return []
        out = []
        for r in range(self.post_off[i], self.post_off[i + 1]):
            off = r * self.rec
            doc = struct.unpack_from(self.dfmt, self.postings, off)[0]
            tf = self.postings[off + self.width]
            out.append((doc, tf))
        return out

    def search(self, terms, limit=200):
        """Okapi BM25. Returns [(uid, score)] descending."""
        score = collections.defaultdict(float)
        for t in set(terms):
            post = self.postings_for(t)
            if not post:
                continue
            idf = math.log(1.0 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for doc, tf in post:
                dl = self.doclen[doc] or 1
                denom = tf + self.K1 * (1 - self.B + self.B * dl / self.avgdl)
                score[doc] += idf * (tf * (self.K1 + 1)) / denom
        top = sorted(score.items(), key=lambda kv: -kv[1])[:limit]
        return [(self.uids[d], s) for d, s in top]


def query(args):
    idx = Bm25()
    terms = tokenise(args.text)
    log(f"[bm25] {idx.n:,} docs, {idx.nterms:,} terms; query terms {terms}")
    for uid, s in idx.search(terms, limit=args.limit):
        log(f"  {s:7.2f}  {uid}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    b.add_argument("--df-max-frac", type=float, default=DF_MAX_FRAC,
                   help="drop terms appearing in more than this fraction of docs")
    b.add_argument("--out", default=str(DOCS), help="directory to write into")
    q = sub.add_parser("query")
    q.add_argument("text")
    q.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()
    return build(args) if args.action == "build" else query(args)


if __name__ == "__main__":
    sys.exit(main())
