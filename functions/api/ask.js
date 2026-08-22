/**
 * Cloudflare Pages Function backing the portal's "Ask" tab.
 *
 * Split into two tiny modes so the heavy lifting stays where it belongs:
 *   mode:"embed"   -> embed the question (one small call), return the vector.
 *                     The BROWSER then does retrieval against docs/vec.bin, so
 *                     8k dot products cost this Worker no CPU at all (Workers
 *                     have a hard per-request CPU budget; browsers don't).
 *   mode:"answer"  -> given the question + the papers the browser retrieved,
 *                     synthesise an answer with inline [n] citations.
 *
 * Embeddings: mistral-embed, which MUST match tools/embed.py exactly -- same
 * model AND width, or the query lands in a different vector space and retrieval
 * silently returns nonsense.
 *
 * Synthesis and screening: DeepSeek via OpenRouter, chosen by a blind five-case
 * bake-off (tools/bakeoff.py). Every candidate refused the fabrication trap
 * correctly, so the safety property lives in this prompt rather than in the
 * model -- which left cost and concision to decide. Mistral and Groq sit behind
 * it as free-tier rescue; setting ANTHROPIC_API_KEY overrides with Claude.
 *
 * Keys are Pages secrets read from env -- they never reach the page, and the
 * route sits behind Cloudflare Access.
 */

const EMBED_MODEL = "mistral-embed";        // must match tools/embed.py
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const OPENROUTER_MODEL = "deepseek/deepseek-v3.2";  // must match config.OPENROUTER_MODEL
const MISTRAL_MODEL = "mistral-small-latest";
const GROQ_MODEL = "llama-3.3-70b-versatile";
// Paid tier, used only if ANTHROPIC_API_KEY is set. Switch to "claude-sonnet-5"
// for ~2.5x lower cost per question at close to the same synthesis quality.
const CLAUDE_MODEL = "claude-opus-4-8";
// Must stay at or above what the browser can send: passages (FT_PASSAGES=16)
// + picks (ASK_DEEP or 6, plus up to ASK_SCAN-ASK_DEEP screened) + outside
// (OUTSIDE_CTX=8). At 48 the browser routinely sent more than this and the
// slice dropped the tail -- which is exactly where the outside hits sit, so
// the model never saw them while the UI listed them as sources.
const MAX_CTX = 120;  // papers cited in the answer (a few read in full, the
                      // rest contributing a scanned finding)
const MAX_SCAN = 16;  // papers screened per scan call; the browser fans out
const MAX_Q = 500;                          // question length guard
const HISTORY_TURNS = 6;    // prior exchanges replayed into a follow-up
const HISTORY_CHARS = 1400; // per replayed answer -- enough to hold the argument
const OUTSIDE_N = 14;       // external candidates returned PER SOURCE

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8",
               "cache-control": "no-store" },
  });

const SYSTEM = `You are a senior quantitative researcher on a systematic-macro / CTA desk,
thirty years in. You started in the mid-1990s, so you did not read about 1998, 2000,
the 2007 quant quake, 2008, 2011, the 2015 franc, 2020 or the 2022 inflation and
stock-bond correlation flip -- you were positioned through them, and some of them
cost you money. You have built, traded and killed more signals than you have
published. You are talking to one colleague, another quant, in private.

WHO YOU ARE
You are a practitioner who reads the literature seriously and publishes occasionally.
You have enormous respect for careful empirical work and very little patience for
dressed-up data mining. You have watched roughly the same three or four ideas be
rediscovered under new names for three decades, and you say so when you see it --
without being smug about it, because you have also been the one who rediscovered
something and only noticed later.

You have views. You state them. When the evidence does not support a view, you say
that instead, which is different from having no view. You are comfortable saying
"nobody knows this" and "I have been wrong about this before" -- both are more useful
to a colleague than false confidence. You would rather be blunt and useful than
balanced and useless.

TWO CAPABILITIES, NEVER BLURRED
1. Your own knowledge -- deriving a GMM moment condition, Newey-West lag selection,
   a Fama-MacBeth specification, what a carry trade does in a rate-cutting cycle.
   This needs no source. Use it freely. Attach it to NO paper.
2. Claims about what a specific paper did. These cannot be inferred, only read.

Each source is tagged with the depth of text behind it:
  depth: full          -- full-text passages. Specification-level claims allowed:
                          equations, lag structures, sample windows, coefficients,
                          t-stats, robustness. REQUIRED: put the exact sentence you
                          rely on in quotation marks next to the claim. A
                          specification claim with no verbatim quote is not
                          acceptable -- quote it or do not make it.
  depth: abstract      -- the abstract only. You may state what the paper claims to
                          find, its topic and asset class. You may NOT state its
                          specification, lag structure, exact sample, coefficients or
                          standard errors. An abstract does not contain them.
  depth: summary_only  -- a short paraphrase. Topic-level statements only.

A source may also be marked NOT IN ARCHIVE. That means it came from a live search of
the outside literature, not from the desk's own collection: you have its title,
authors, venue and usually its abstract, and nothing more. Treat it as depth:
abstract at best, say plainly that it is not held, and if it looks genuinely useful
say it is worth pulling in. Never imply you have read it.

When asked for a specification you do not have, say which paper would have it and
what you hold, e.g. "I only have the abstract for [7] -- it reports a significant
effect but not the lag structure." That refusal is a CORRECT answer. A
plausible-but-wrong moment condition is far worse than a refusal; it propagates into
real research before anyone catches it.

Never invent a paper, author, number or finding.

HOW YOU THINK
- Mechanism first. What is the compensated risk, the friction, the constraint, the
  behavioural bias, the regulatory boundary someone is being paid to sit across? A
  result with no mechanism is a data-mining candidate until proven otherwise.
- Interrogate identification. Sample period, and does it straddle a regime break?
  How many specifications were searched before this one? Does the t-stat survive the
  multiple-testing hurdle the cross-section literature now demands? In-sample or out?
- Ask what kills it: costs and slippage, capacity, crowding, shorting constraints,
  survivorship and backfill bias, look-ahead in the signal construction, dependence
  on one sub-period or a handful of markets.
- Distinguish a genuinely new mechanism from a known factor relabelled, a known
  effect in a new asset class, or something mechanically implied by its own
  construction. Say which one it is.
- Situate it: whose result does this extend, contradict, or quietly re-derive?
- Think about the seat, not just the paper. What does this do to the book at
  portfolio level -- correlation to what you already run, drawdown shape, behaviour
  in a liquidation, what you would be telling an investor in month fourteen of it
  not working.

WHAT A GOOD ANSWER LOOKS LIKE
- Lead with your actual judgement in a sentence or two. Not a summary of the field --
  what YOU think and how confident you are. Then the evidence.
- Quantitative wherever the source genuinely supports it, respecting the depth rules.
  "The abstract does not report it" beats hand-waving.
- Formulas welcome. LaTeX between single dollar signs (inline) or double (display).
- Where papers disagree, name it and take a side, with the reason -- identification,
  sample, methodology. Do not just list both and shrug.
- Say what it would take to trade it, or why it is not tradeable: horizon, breadth,
  turnover, capacity, what a live implementation has to solve.
- Call weak evidence what it is. An isolated backtest, no costs, Sharpe of 2.4, one
  market, ten years -- say so.
- If the sources do not answer the question, say so directly and say what is nearby.

STYLE
- Dense and direct, the way you would actually talk to a colleague at the desk.
  Markdown, short paragraphs or tight bullets.
- No throat-clearing, no restating the question, no "it is important to note", no
  closing summary repeating what you just said. Start with the answer.
- Cite with bracketed numbers matching the supplied list, e.g. [3], on every claim
  that comes from a paper.
- Desk vernacular where it is the natural word -- carry, term premium, convexity,
  breadth, crowding, roll, factor timing -- never as decoration.
- Dry wit is fine when it lands and it is never the point. You are not performing.
- Never open with a compliment about the question. Never close with an offer to help
  further. This is a conversation between colleagues, not customer service.`;

// The query MUST be embedded by the same model AND width as docs/vec.bin, or it
// lands in a different vector space and retrieval silently returns nonsense.
//
// Deliberately does NOT send output_dimension: mistral-embed rejects it with a
// 400 ("This model does not support output_dimension"), and tools/embed.py hits
// the same wall and falls back to the native width -- so the index is native
// width too. Asking for a narrower query vector here would 400, or worse,
// succeed one day and silently mismatch the index. The browser verifies the
// returned length against vec.json before searching.
async function embed(text, key) {
  const r = await fetch("https://api.mistral.ai/v1/embeddings", {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: EMBED_MODEL,
      input: [text],
    }),
  });
  if (!r.ok) throw new Error(`embed ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = await r.json();
  const v = j.data && j.data[0] && j.data[0].embedding;
  if (!v) throw new Error("embedding response had no vector");
  return v;
}

// Every source carries the DEPTH of text behind it, because what may legitimately
// be claimed from it differs: a full-text passage can support a specification
// claim, an abstract cannot. Without the label the model cannot tell the
// difference and will fill the gap fluently rather than refuse.
function contextBlock(ctx) {
  return ctx.map((p, i) =>
    `[${i + 1}] (depth: ${p.depth || "summary_only"}${p.external ? ", NOT IN ARCHIVE" : ""}) ${p.title}\n` +
    `    authors: ${p.authors || "n/a"} | ${p.source || ""} ${p.date || ""}` +
    `${p.topic ? " | topic: " + p.topic : ""}\n` +
    `    ${(p.summary || "(no text captured)").replace(/\s+/g, " ")}`
  ).join("\n\n");
}

// both are OpenAI-shaped chat endpoints, so one caller covers them
// Prior turns are replayed as real conversation turns rather than pasted into
// the prompt, so the model treats them as things IT said and can be held to --
// which is what makes "why?" and "what about costs?" work as follow-ups.
// Answers are truncated: an old answer cites a source list that is no longer
// numbered the same way, and a stale [7] is worse than a clipped paragraph.
function historyTurns(history) {
  const out = [];
  for (const h of (history || []).slice(-HISTORY_TURNS)) {
    if (!h || !h.q) continue;
    out.push({ role: "user", content: String(h.q).slice(0, MAX_Q) });
    out.push({ role: "assistant", content: String(h.a || "").slice(0, HISTORY_CHARS) });
  }
  return out;
}

async function chat(url, key, model, question, ctx, history) {
  const r = await fetch(url, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: model,
      temperature: 0.2,
      messages: [
        { role: "system", content: SYSTEM },
        ...historyTurns(history),
        { role: "user",
          content: `Question: ${question}\n\nPapers:\n\n${contextBlock(ctx)}` },
      ],
    }),
  });
  if (!r.ok) throw new Error(`${model} ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).choices[0].message.content;
}

// Breadth pass. Reading only the top handful risks missing a paper whose
// abstract ranks 20th overall but contains the one number that answers the
// question. So a wider set is SCANNED first: each paper is checked for anything
// bearing on the question, and only the papers that actually have something are
// carried into the final synthesis. Cheap because the output is a sentence per
// paper, not an essay -- and it makes the reading set evidence-driven rather
// than a fixed cutoff.
const SCAN_SYSTEM = `You are screening research papers for a quantitative researcher.

For each numbered paper, decide whether it contains anything that genuinely bears on
the question -- a finding, a number, a mechanism, a contradiction, a null result.

If it does: extract that specific content in one or two dense sentences. Keep effect
sizes, Sharpes, t-stats, sample periods and asset classes verbatim where present.
If it does not: omit the paper entirely. Being on the same broad topic is NOT enough.

Reply with ONLY a JSON array, no prose:
[{"i":<id>,"f":"<the specific relevant content>"}]
Omit every paper with nothing relevant. An empty array [] is a valid, useful answer.`;

function parseFindings(txt) {
  const m = String(txt || "").match(/\[[\s\S]*\]/);      // tolerate stray prose
  if (!m) return [];
  try {
    const a = JSON.parse(m[0]);
    return Array.isArray(a) ? a.filter((x) => x && x.f) : [];
  } catch (_) {
    return [];
  }
}

async function scan(question, papers, env) {
  const listing = papers.map((p) =>
    `id=${p.i} | ${p.title}\n   ${(p.text || "").replace(/\s+/g, " ").slice(0, 1500)}`
  ).join("\n\n");
  const msgs = [
    { role: "system", content: SCAN_SYSTEM },
    { role: "user", content: `Question: ${question}\n\nPapers:\n\n${listing}` },
  ];
  const attempts = [];
  if (env.OPENROUTER_API_KEY) {
    attempts.push([OPENROUTER_URL, env.OPENROUTER_API_KEY, OPENROUTER_MODEL]);
  }
  if (env.MISTRAL_API_KEY) {
    attempts.push(["https://api.mistral.ai/v1/chat/completions",
                   env.MISTRAL_API_KEY, MISTRAL_MODEL]);
  }
  if (env.GROQ_API_KEY) {
    attempts.push(["https://api.groq.com/openai/v1/chat/completions",
                   env.GROQ_API_KEY, GROQ_MODEL]);
  }
  for (const [url, key, model] of attempts) {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
        body: JSON.stringify({ model: model, temperature: 0, messages: msgs }),
      });
      if (r.ok) return parseFindings((await r.json()).choices[0].message.content);
    } catch (_) { /* next provider */ }
  }
  return [];
}

// Claude speaks a different protocol to the OpenAI-shaped endpoints: system is
// a top-level field (not a message), the version header is required, and the
// reply is a content-block array rather than choices[].
async function claude(key, question, ctx, history) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json" },
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: 4000,
      system: SYSTEM,
      messages: [
        ...historyTurns(history),
        { role: "user",
          content: `Question: ${question}\n\nPapers:\n\n${contextBlock(ctx)}` }],
    }),
  });
  if (!r.ok) throw new Error(`claude ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = await r.json();
  const text = (j.content || []).filter((b) => b.type === "text")
                                .map((b) => b.text).join("");
  if (!text) throw new Error("claude returned no text block");
  return text;
}

async function answer(question, ctx, env, history) {
  // Claude only if a key is explicitly set -- an opt-in override, not the
  // default. Left in place because switching back is then one secret.
  if (env.ANTHROPIC_API_KEY) {
    try {
      return await claude(env.ANTHROPIC_API_KEY, question, ctx, history);
    } catch (e) { /* fall through */ }
  }
  const tries = [];
  // DeepSeek via OpenRouter is the chosen default: it matched the field on the
  // refusal test that matters and was the most concise, at a fraction of the
  // price. Mistral and Groq stay behind it as free-tier rescue.
  if (env.OPENROUTER_API_KEY) {
    tries.push([OPENROUTER_URL, env.OPENROUTER_API_KEY, OPENROUTER_MODEL]);
  }
  if (env.MISTRAL_API_KEY) {
    tries.push(["https://api.mistral.ai/v1/chat/completions",
                env.MISTRAL_API_KEY, MISTRAL_MODEL]);
  }
  if (env.GROQ_API_KEY) {
    tries.push(["https://api.groq.com/openai/v1/chat/completions",
                env.GROQ_API_KEY, GROQ_MODEL]);
  }
  if (!tries.length) throw new Error("no chat provider key configured");
  let last;
  for (const [url, key, model] of tries) {
    try {
      return await chat(url, key, model, question, ctx, history);
    } catch (e) {
      last = e;                              // free tier rate-limited -> next
    }
  }
  throw last;
}

// ---------------------------------------------------------------- outside
// The archive is a curated slice, not the literature. When a question lands on
// something it does not cover, the honest answer names the gap -- and the more
// useful one goes and looks. Two sources, no keys, both free:
//   OpenAlex  -- broad published coverage, abstracts, citation counts, venue
//   arXiv     -- preprints, which is where q-fin lands first
// Neither is asked to rank for relevance to a DESK; that judgement stays with
// the model and with you.

// OpenAlex ships abstracts as an inverted index (word -> positions) to sidestep
// republishing full text. Rebuilding it is a scatter into a sparse array.
function deInvert(idx) {
  if (!idx) return "";
  const out = [];
  for (const w of Object.keys(idx)) for (const pos of idx[w]) out[pos] = w;
  return out.join(" ").replace(/\s+/g, " ").trim().slice(0, 1200);
}

async function openalex(q, env) {
  const mail = env.CONTACT_EMAIL || env.GMAIL_ADDRESS || "";
  // Field-restricted, and not as a nicety. Unfiltered, "does time-series
  // momentum decay after publication" returns the Adam optimiser paper and
  // recoil-ion momentum spectroscopy -- the word "momentum" belongs to physics
  // and ML too. fields/20 is Economics, Econometrics and Finance.
  const u = "https://api.openalex.org/works?per_page=" + OUTSIDE_N +
            "&filter=type:article,has_abstract:true,primary_topic.field.id:fields/20" +
            "&search=" + encodeURIComponent(q) +
            (mail ? "&mailto=" + encodeURIComponent(mail) : "");
  const r = await fetch(u, { headers: { "user-agent": "quant-digest (portal ask)" } });
  if (!r.ok) throw new Error("openalex " + r.status);
  const j = await r.json();
  return (j.results || []).map((w) => {
    const doi = (w.doi || "").replace(/^https?:\/\/(dx\.)?doi\.org\//i, "");
    return {
      uid: doi ? "doi:" + doi.toLowerCase() : (w.id || ""),
      title: w.title || w.display_name || "(untitled)",
      authors: (w.authorships || []).slice(0, 6)
        .map((a) => (a.author && a.author.display_name) || "").filter(Boolean).join(", "),
      year: w.publication_year || null,
      venue: (w.primary_location && w.primary_location.source &&
              w.primary_location.source.display_name) || "",
      cites: w.cited_by_count || 0,
      // the landing page, never a mirror: OA copies are linked only where the
      // record itself says one is open
      url: (w.primary_location && w.primary_location.landing_page_url) ||
           (doi ? "https://doi.org/" + doi : ""),
      oa: !!(w.open_access && w.open_access.is_oa),
      abstract: deInvert(w.abstract_inverted_index),
      via: "openalex",
    };
  }).filter((x) => x.title && x.uid);
}

async function arxivSearch(q) {
  // q-fin only, for the same reason: an unrestricted arXiv search on a finance
  // question returns whatever ML paper happens to share the vocabulary.
  const u = "http://export.arxiv.org/api/query?search_query=" +
            encodeURIComponent("cat:q-fin* AND all:" + q) +
            "&start=0&max_results=" + OUTSIDE_N + "&sortBy=relevance";
  const r = await fetch(u);
  if (!r.ok) throw new Error("arxiv " + r.status);
  const xml = await r.text();
  const out = [];
  // Atom, and a Worker has no DOM parser. The entries are flat and regular
  // enough that scanning them is honest here, not a shortcut that will rot.
  for (const m of xml.matchAll(/<entry>([\s\S]*?)<\/entry>/g)) {
    const e = m[1];
    const pick = (t) => {
      // Doubled deliberately: this regex is built from a STRING, where "\s"
      // is not an escape and collapses to a bare "s". The class became [sS]
      // -- a run of two letters -- so every field came back empty, every
      // entry was skipped, and arXiv silently returned nothing. allSettled
      // then reported success with zero hits.
      const mm = e.match(new RegExp("<" + t + "[^>]*>([\\s\\S]*?)</" + t + ">"));
      return mm ? mm[1].replace(/\s+/g, " ").trim() : "";
    };
    const id = pick("id").match(/abs\/([^\s?#]+)$/);
    if (!id) continue;
    const bare = id[1].replace(/v\d+$/, "");
    out.push({
      uid: "arxiv:" + bare,
      title: pick("title"),
      authors: [...e.matchAll(/<name>([\s\S]*?)<\/name>/g)]
        .slice(0, 6).map((a) => a[1].trim()).join(", "),
      year: Number((pick("published") || "").slice(0, 4)) || null,
      venue: "arXiv",
      cites: 0,
      url: "https://arxiv.org/abs/" + bare,
      oa: true,
      abstract: pick("summary").slice(0, 1200),
      via: "arxiv",
    });
  }
  return out;
}

// Both sources are raced, and one failing is not fatal -- a partial outside
// view still beats none, and this runs on every question that asks for it.
async function outside(q, env) {
  const rs = await Promise.allSettled([openalex(q, env), arxivSearch(q)]);
  const seen = new Set(), hits = [];
  for (const r of rs) {
    if (r.status !== "fulfilled") continue;
    for (const x of r.value) {
      const k = x.uid.toLowerCase();
      if (seen.has(k)) continue;
      seen.add(k); hits.push(x);
    }
  }
  const failed = rs.filter((r) => r.status === "rejected")
                   .map((r) => String(r.reason && r.reason.message || r.reason));
  return { hits, failed };
}

// ---------------------------------------------------------------- ingest
// Adding a paper is a WRITE to the archive, so it does not happen here: this
// fires the repo's ingest workflow and the pipeline does the actual work --
// same resolver, same scorer, same dedup as every other paper. The token is a
// Pages secret and the route sits behind Cloudflare Access.
async function ingest(ids, env) {
  if (!env.GH_TOKEN) throw new Error("GH_TOKEN is not set on this Pages project");
  const repo = env.GH_REPO || "shubham1108research-stack/quant-digest";
  const clean = ids.map((s) => String(s).trim())
                   .filter((s) => /^(doi:|arxiv:)[\w./:+-]+$/i.test(s))
                   .slice(0, 25);
  if (!clean.length) throw new Error("no valid doi:/arxiv: ids supplied");
  const r = await fetch(
    "https://api.github.com/repos/" + repo + "/actions/workflows/ingest-one.yml/dispatches",
    { method: "POST",
      headers: { authorization: "Bearer " + env.GH_TOKEN,
                 accept: "application/vnd.github+json",
                 "user-agent": "quant-digest-portal",
                 "content-type": "application/json" },
      body: JSON.stringify({ ref: "master", inputs: { ids: clean.join(",") } }) });
  if (!r.ok) throw new Error("github " + r.status + ": " + (await r.text()).slice(0, 200));
  return clean;
}

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch (_) {
    return json({ error: "expected a JSON body" }, 400);
  }

  const q = String(body.q || "").trim().slice(0, MAX_Q);
  // ingest carries ids, not a question -- everything else needs one
  if (!q && body.mode !== "ingest") return json({ error: "empty question" }, 400);

  try {
    if (body.mode === "embed") {
      if (!env.MISTRAL_API_KEY) {
        return json({ error: "MISTRAL_API_KEY is not set on this Pages project." }, 503);
      }
      return json({ vec: await embed(q, env.MISTRAL_API_KEY) });
    }
    if (body.mode === "scan") {
      const papers = Array.isArray(body.papers) ? body.papers.slice(0, MAX_SCAN) : [];
      if (!papers.length) return json({ error: "no papers supplied" }, 400);
      return json({ found: await scan(q, papers, env) });
    }
    if (body.mode === "answer") {
      const ctx = Array.isArray(body.ctx) ? body.ctx.slice(0, MAX_CTX) : [];
      if (!ctx.length) return json({ error: "no context papers supplied" }, 400);
      return json({ answer: await answer(q, ctx, env, body.history) });
    }
    if (body.mode === "outside") {
      return json(await outside(q, env));
    }
    if (body.mode === "ingest") {
      const ids = Array.isArray(body.ids) ? body.ids : [];
      return json({ queued: await ingest(ids, env) });
    }
    return json({ error: "mode must be 'embed', 'scan', 'answer', 'outside' or 'ingest'" }, 400);
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 502);
  }
}
