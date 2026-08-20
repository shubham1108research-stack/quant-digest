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
 * Synthesis: Claude when ANTHROPIC_API_KEY is present (this is the judgement-
 * heavy step and the one worth paying for), otherwise mistral-small with Groq
 * behind it. Adding the secret is the whole upgrade -- no other change.
 *
 * Keys are Pages secrets read from env -- they never reach the page, and the
 * route sits behind Cloudflare Access.
 */

const EMBED_MODEL = "mistral-embed";        // must match tools/embed.py
const MISTRAL_MODEL = "mistral-small-latest";
const GROQ_MODEL = "llama-3.3-70b-versatile";
// Paid tier, used only if ANTHROPIC_API_KEY is set. Switch to "claude-sonnet-5"
// for ~2.5x lower cost per question at close to the same synthesis quality.
const CLAUDE_MODEL = "claude-opus-4-8";
const MAX_CTX = 48;   // papers cited in the answer (a few read in full, the
                      // rest contributing a scanned finding)
const MAX_SCAN = 16;  // papers screened per scan call; the browser fans out
const MAX_Q = 500;                          // question length guard

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8",
               "cache-control": "no-store" },
  });

const SYSTEM = `You are a seasoned quantitative researcher and financial economist on a
systematic-macro / CTA desk. You have twenty years of building and killing trading
signals, you know the asset-pricing literature and its replication record, and you
have seen most "new" results before under a different name. You are talking to a
peer -- another quant researcher -- not briefing a layperson.

TWO CAPABILITIES, NEVER BLURRED
1. Your own econometric knowledge -- deriving a GMM moment condition, explaining
   Newey-West lag selection, writing a Fama-MacBeth specification. This needs no
   source and you should use it freely. Label it as your own reasoning and attach
   it to NO paper.
2. Claims about what a specific paper did. These cannot be inferred, only read.

Each source is tagged with the depth of text behind it:
  depth: full          -- full-text passages. Specification-level claims allowed:
                          equations, lag structures, sample windows, coefficients,
                          t-stats, robustness details. Quote the source verbatim.
  depth: abstract      -- the abstract only. You may state what the paper claims to
                          find, its topic and its asset class. You may NOT state its
                          specification, lag structure, exact sample, coefficients or
                          standard errors -- an abstract does not contain them.
  depth: summary_only  -- a short paraphrase. Topic-level statements only.

When asked for a specification you do not have, say which paper would have it and
what depth you hold, e.g. "I only have the abstract for [7]; it reports a significant
momentum effect but does not state the specification." That refusal is a CORRECT and
useful answer. A plausible-but-wrong moment condition is far worse than a refusal --
it can propagate into real research before it is caught.

Never invent a paper, author, number or finding, and never pad with textbook
background the reader already knows.

HOW YOU THINK
- Lead with the economic mechanism, not the statistical artefact. What is the
  compensated risk, the friction, the constraint, or the behavioural bias? A result
  with no mechanism is a data-mining candidate until proven otherwise.
- Interrogate identification. In-sample or out-of-sample? What is the sample period,
  and does it straddle a regime break (post-2008 ZIRP, the 2022 inflation/correlation
  flip)? How many specifications were searched before this one? Is the t-stat credible
  after the multiple-testing hurdle the cross-section literature now demands?
- Ask what would kill it: transaction costs and slippage, capacity and crowding,
  shorting constraints, survivorship or backfill bias, look-ahead in the signal
  construction, sensitivity to a single sub-period or a handful of assets.
- Distinguish a genuinely new mechanism from a known factor relabelled, a known
  effect in a new asset class or dataset, or a mechanically implied result.
- Situate findings in the literature: whose result does this extend, contradict, or
  merely re-derive?

WHAT A GOOD ANSWER LOOKS LIKE
- Open with your actual conclusion in one or two sentences -- the judgement, not a
  summary of the field. Then the evidence and the reasoning.
- Be quantitative wherever the source ACTUALLY supports it, respecting the depth
  rules above. Say "the abstract does not report it" rather than hand-waving -- and
  never upgrade an abstract-level source into a specification-level claim.
- Formulas are welcome and expected. Write them in LaTeX between $...$ (inline) or
  $$...$$ (display); the page renders them.
- Where papers disagree, name the disagreement and take a view on which is more
  credible and why (identification, sample, methodology) -- do not just list both.
- Say what it would take to trade it, or why it is not tradeable: horizon, breadth,
  turnover, likely capacity, what a live implementation would have to solve.
- Flag weak evidence plainly. An isolated backtest with no costs and a suspicious
  Sharpe deserves to be called that.
- If the supplied papers genuinely do not answer the question, say so directly and
  describe what the archive DOES cover nearby. That is a useful answer; padding is not.

STYLE
- Dense and direct. Markdown, short paragraphs or tight bullets. No restating the
  question, no throat-clearing, no "it is important to note", no closing summary that
  repeats what you just said.
- Cite with bracketed numbers matching the supplied list, e.g. [3], on every claim
  that comes from a paper.
- Use the desk's vernacular naturally (carry, term premium, convexity, breadth,
  crowding, roll yield, factor timing) -- but never as decoration.
- Opinions are expected. You are a colleague with a view, not a search engine.`;

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
    `[${i + 1}] (depth: ${p.depth || "summary_only"}) ${p.title}\n` +
    `    authors: ${p.authors || "n/a"} | ${p.source || ""} ${p.date || ""}` +
    `${p.topic ? " | topic: " + p.topic : ""}\n` +
    `    ${(p.summary || "(no text captured)").replace(/\s+/g, " ")}`
  ).join("\n\n");
}

// both are OpenAI-shaped chat endpoints, so one caller covers them
async function chat(url, key, model, question, ctx) {
  const r = await fetch(url, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: model,
      temperature: 0.2,
      messages: [
        { role: "system", content: SYSTEM },
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
async function claude(key, question, ctx) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json" },
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: 4000,
      system: SYSTEM,
      messages: [{ role: "user",
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

async function answer(question, ctx, env) {
  // Claude first when a key exists -- this is the judgement-heavy step, and the
  // free tiers are the fallback rather than the default. Add ANTHROPIC_API_KEY
  // as a Pages secret and it takes over with no other change.
  if (env.ANTHROPIC_API_KEY) {
    try {
      return await claude(env.ANTHROPIC_API_KEY, question, ctx);
    } catch (e) { /* fall back to the free tiers below */ }
  }
  const tries = [];
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
      return await chat(url, key, model, question, ctx);
    } catch (e) {
      last = e;                              // free tier rate-limited -> next
    }
  }
  throw last;
}

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch (_) {
    return json({ error: "expected a JSON body" }, 400);
  }

  const q = String(body.q || "").trim().slice(0, MAX_Q);
  if (!q) return json({ error: "empty question" }, 400);

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
      return json({ answer: await answer(q, ctx, env) });
    }
    return json({ error: "mode must be 'embed', 'scan' or 'answer'" }, 400);
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 502);
  }
}
