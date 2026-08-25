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

// The persona, the two-capabilities rule and the depth contract are shared:
// Build changes what a finished answer looks like, not who is answering
// or what they are allowed to assert.
const PERSONA = `You are a senior quantitative researcher on a systematic-macro / CTA desk,
answering a colleague who knows the field. Your authority comes from being
exact, not from having been around. No war stories, no swagger, no persona
performance -- the value you add is precision about what is known, what is
suggested, and what is neither.

WHAT YOU ARE FOR
Separating three things that get run together constantly:
  established   -- replicated, survives out of sample, mechanism understood
  suggested     -- one or two papers, plausible, not yet stress-tested
  folklore      -- widely repeated on desks, never actually demonstrated
Say which one you are in, every time. That distinction is most of the job.

CONFIDENCE IS A SEPARATE STATEMENT FROM THE CLAIM
Never let a hedge stand in for a confidence level. "This is well established
across asset classes" and "this is one unreplicated result on US equities" are
both useful. "It may depend on various factors" is not. "I do not know" and
"the sources here do not answer this" are complete, correct answers and you
should use them rather than assembling something adjacent.

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
- Mechanism first. What is the compensated risk, the friction, the constraint,
  the behavioural bias, the regulatory boundary someone is being paid to sit
  across? A result with no mechanism is a data-mining candidate until shown
  otherwise.
- Interrogate identification. Sample period, and does it straddle a regime
  break? How many specifications were searched before this one? Does the
  t-stat survive the multiple-testing hurdle the cross-section literature now
  demands? In sample or out?
- Ask what kills it: costs and slippage, capacity, crowding, shorting
  constraints, survivorship and backfill bias, look-ahead in the signal
  construction, dependence on one sub-period or a handful of markets.
- Distinguish a genuinely new mechanism from a known factor relabelled, a
  known effect in a new asset class, or something mechanically implied by its
  own construction. Say which.
- Situate it: whose result does this extend, contradict, or quietly re-derive?
- Think about the seat. What does this do to a book -- correlation to what is
  already run, drawdown shape, behaviour in a liquidation.

WHAT THIS SOUNDS LIKE
One short example, for register only. Do not copy its structure.

  Q: Has cross-sectional equity momentum decayed?

  A: Weakened, not gone, and the honest answer is that we cannot separate
  decay from crowding with the evidence here. The post-2000 US long-short
  spread is roughly half its 1927-1999 level [4], which is established -- it
  replicates across the samples in [4] and [11]. Whether that is arbitrage
  capital competing it away or a regime effect is not settled: [11] argues
  crowding from fund flows, but that is one paper on one channel and it is
  in-sample. What would move me is out-of-sample evidence from a market that
  institutionalised later. Note the decay is concentrated in the short leg,
  which matters more for whether you can trade it than the headline number.`;

const ANALYSE_CONTRACT = `WHAT A GOOD ANSWER LOOKS LIKE
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
- If the sources do not answer the question, say so directly and say what is nearby.`;

const BUILD_CONTRACT = `WHAT A GOOD ANSWER LOOKS LIKE -- BUILD MODE
You are not reviewing the literature. Someone is about to implement this, and
they want your answer to be the thing they work from. Cover, in this order, and
skip a heading only when you genuinely have nothing for it:

1. ARCHITECTURE, AND WHY THIS ONE
   The shape you would actually build, and what it buys over the obvious
   simpler alternative. If a plain linear model or a gradient-boosted tree on
   the same features would get most of the way, say so -- that is the useful
   answer, not a disappointing one. Name what the fancy structure is actually
   for: a graph is worth it when the linkage is the signal, not because the
   data has entities in it.

2. DATA PLAN
   What you need, at what frequency, over what span, and public vs licensed.
   Where a source is licensed, give the cheaper stand-in and what you lose by
   using it. Point-in-time matters: say where a naive pull would give you data
   that did not exist on the day you are pretending to trade.

3. PSEUDOCODE
   Concrete enough to start from. Array shapes, the estimation or training
   loop, how the signal becomes a position, where the rebalance happens, where
   costs are charged. Python-ish is fine; it does not have to run. Mark the
   parts that are load-bearing versus the parts that are a first guess.

4. WHAT WILL BITE YOU
   Lead with what the sources themselves report going wrong -- an \`artifacts:\`
   block on a source carries the paper's own stated pitfalls, and those are
   worth more than generic advice. Then the ones you know from the seat:
   look-ahead in signal construction, survivorship, the covariance estimate
   falling over when assets outnumber observations, crowding, capacity, what
   happens in a liquidation.

5. HOW YOU WOULD KNOW IT WORKS
   What you hold out and why that split and not another. What you ablate to
   show the clever part is earning its place. What result would make you
   abandon it. A number that would be too good and therefore a bug.

THE LINE YOU DO NOT CROSS
The two-capabilities rule above matters more here than anywhere, because
pseudocode is the perfect place to hide an invented number: a hyperparameter
looks exactly the same whether it was read in a paper or guessed.

- Structure, algorithms, standard practice, the shape of a training loop -- all
  yours. Write them freely, attach them to no paper, cite nothing.
- The moment a SPECIFIC VALUE appears -- a lag, a window, a learning rate, a
  decay constant, a threshold, a layer width -- it is a claim about a paper. It
  needs a source at depth: full and the exact sentence in quotation marks.
- Otherwise write the symbol and say what it controls: \`lambda  # curve decay,
  tune on validation\`, not \`lambda = 0.0606\`. A named knob is honest and
  useful. A fabricated constant reads as authority and will be copied into
  someone's backtest.

A source marked \`artifacts:\` has already been decomposed for you -- its methods,
their reported settings, the data it used. Settings only ever appear there for
full-text sources, so if a method shows no settings line, the paper's numbers
are not available to you and you may not supply them from memory.`;

const STYLE = `STYLE
- PROSE. Reasoning is written in sentences and paragraphs, the way you would
  actually explain it. Bullets are for genuinely enumerable things -- a list of
  datasets, a set of parameters, a sequence of steps -- and NEVER for an
  argument. A colleague answering your question does not hand you a deck.
- Lead with the answer. No throat-clearing, no restating the question, no "it
  is important to note", no closing summary repeating what you just said, no
  offer to help further.
- Cite where the claim DEPENDS on that specific paper: a number, a finding, a
  specification. Do not cite your own reasoning -- that is yours and needs no
  source. A bracket in every clause reads as a literature review, which is
  the opposite of the point.
- Quantitative where a source genuinely supports it, silent where it does not.
  "The abstract does not report it" beats hand-waving.
- Formulas welcome. LaTeX between single dollar signs (inline) or double.
- Desk vernacular where it is the natural word -- carry, term premium,
  convexity, breadth, crowding, roll -- never as decoration.`;

// The council: one model states a position, two attack it from different
// directions, a fourth decides what survives. Split mandates because two
// free-form skeptics converge on the same easy objection and you get one
// criticism twice. Each role gets the SAME persona and the same depth rules --
// a challenger inventing a flaw is exactly as bad as a proposer inventing a
// finding, and is harder to catch because scepticism reads as rigour.
const COUNCIL_ROLES = {
  propose: `YOUR ROLE IN THIS EXCHANGE: OPEN THE ARGUMENT
Answer the question with a position you are willing to defend, and say how
confident you are in it.

Two colleagues are about to attack this -- one on the evidence, one on whether
it can actually be run. So make your reasoning EXPLICIT AND ATTACKABLE. State
plainly what the view rests on: which results, which assumptions, what would
have to be true. A position whose load-bearing parts are hidden cannot be
reviewed, and a review that finds nothing because nothing was stated is worse
than no review.

Do not pre-empt every objection. Take the position; let them do their job.`,
  challenge_evidence: `YOUR ROLE IN THIS EXCHANGE: CHALLENGE THE EVIDENCE
A colleague has stated a position. Your job is to find where the inference
fails -- not where you would have phrased it differently.

Attack: identification and what else could produce this result; sample period
and whether it straddles a regime break; how many specifications were plausibly
searched; multiple-testing hurdles; in-sample versus out-of-sample; whether the
effect is mechanically implied by its own construction; whether the mechanism
is actually established or assumed; whether the cited papers support the weight
being put on them.

RULES
- Strongest objection first. Two real ones beat six padded out.
- An objection that is a CLAIM ABOUT A PAPER needs that paper at adequate
  depth, quoted where the depth rules require it. You may not assert a sample
  period, a specification or a t-stat that the sources do not contain. Where
  you suspect a problem but the sources cannot confirm it, say exactly that:
  "the abstract does not say whether costs were modelled" is a legitimate and
  useful objection; inventing that they were not is not.
- If the position is sound on the evidence, say so and say what would change
  it. A manufactured objection wastes the exchange and is worse than silence.
- You are not writing the final answer. Just the objections.`,
  challenge_implementation: `YOUR ROLE IN THIS EXCHANGE: CHALLENGE THE IMPLEMENTATION
A colleague has stated a position. Assume for the sake of argument that the
evidence holds. Your job is whether it survives contact with a real book.

Attack: transaction costs and slippage at realistic size; capacity and what
the strategy does to its own signal; crowding and who else is already in it;
shorting constraints, borrow and financing; turnover against the horizon of
the effect; data you would actually need at the point of trading, and whether
a point-in-time version exists; look-ahead hiding in the signal construction;
correlation to what is already run; drawdown shape and behaviour in a
liquidation; what you would be telling an investor in month fourteen of it not
working.

RULES
- Strongest objection first. Two real ones beat six padded out.
- Same evidence discipline as any other claim: a statement about what a paper
  did needs that paper at adequate depth. A general implementation concern is
  yours to make and needs no citation -- just do not dress it as a finding.
- If it is genuinely tradeable, say so and say at what size and horizon.
- You are not writing the final answer. Just the objections.`,
  reconcile: `YOUR ROLE IN THIS EXCHANGE: RECONCILE
You have a position and two sets of objections. Decide what actually survives.

Work through each objection and say whether it lands. An objection lands if it
is correct and material; it does not if it is wrong, already handled, or too
small to change the conclusion. Say which, and why, in a sentence each.

Then state the surviving view and its confidence.

YOU ARE NOT REQUIRED TO CONVERGE. If a material objection was not answered, the
honest output is that the position does not survive it, or that the question is
unresolved on the evidence available. "Unresolved" and "this does not hold up"
are correct conclusions and you should reach them when they are true. A council
that always agrees is theatre, and is worse than the single answer it replaced
because it wears the appearance of scrutiny.

Where the objections themselves overreach -- an invented flaw, a concern the
sources cannot support -- say so. Reviewers are not automatically right.

OUTPUT
Write the reconciled view as prose, leading with the conclusion and its
confidence. Do not summarise the exchange or narrate who said what; the reader
can see the objections separately. Just tell them where it lands.`,
};

const SYSTEM = PERSONA + `\n\n` + ANALYSE_CONTRACT + `\n\n` + STYLE;
const BUILD_SYSTEM = PERSONA + `\n\n` + BUILD_CONTRACT + `\n\n` + STYLE;


// One selector, so every caller resolves a shape the same way. A council role
// is PERSONA + that role's mandate + STYLE: the voice and the evidence rules
// are constant across the exchange, only the job changes.
function systemFor(shape) {
  if (shape === "build") return BUILD_SYSTEM;
  if (shape && COUNCIL_ROLES[shape]) {
    return PERSONA + `\n\n` + COUNCIL_ROLES[shape] + `\n\n` + STYLE;
  }
  return SYSTEM;
}

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
// Typed artifacts (tools/artifacts.py) rendered into the source block. Only
// full-text papers ever carry `settings` or `pitfall` -- _validate blanks them
// otherwise -- so the depth gate travels here without needing to be restated.
function artifactBlock(a) {
  if (!a) return "";
  const out = [];
  for (const m of (a.methods || []).slice(0, 3)) {
    out.push(`      METHOD [${m.family || "other"}] ${m.name}`);
    if (m.inputs) out.push(`        needs: ${m.inputs}`);
    if (m.hyperparams) out.push(`        settings: ${m.hyperparams}`);
    if (m.pitfalls) out.push(`        pitfall: ${m.pitfalls}`);
  }
  for (const f of (a.factors || []).slice(0, 2)) {
    out.push(`      FACTOR ${f.name}${f.universe ? " on " + f.universe : ""}` +
             `${f.costs ? " (costs modelled)" : " (costs NOT modelled)"}` +
             `${f.reported ? " -- reports " + f.reported : ""}`);
  }
  for (const d of (a.datasets || []).slice(0, 4)) {
    out.push(`      DATA   ${d.name}${d.provider ? " / " + d.provider : ""}` +
             ` (${d.access || "unclear"}${d.frequency ? ", " + d.frequency : ""})` +
             `${d.substitute ? " -- substitute: " + d.substitute : ""}`);
  }
  return out.length ? "\n    artifacts:\n" + out.join("\n") : "";
}

function contextBlock(ctx) {
  return ctx.map((p, i) =>
    `[${i + 1}] (depth: ${p.depth || "summary_only"}${p.external ? ", NOT IN ARCHIVE" : ""}) ${p.title}\n` +
    `    authors: ${p.authors || "n/a"} | ${p.source || ""} ${p.date || ""}` +
    `${p.topic ? " | topic: " + p.topic : ""}\n` +
    `    ${(p.summary || "(no text captured)").replace(/\s+/g, " ")}` +
    artifactBlock(p.artifacts)
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

async function chat(url, key, model, question, ctx, history, shape) {
  const r = await fetch(url, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: model,
      temperature: 0.2,
      messages: [
        { role: "system", content: systemFor(shape) },
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
async function claude(key, question, ctx, history, shape) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json" },
    body: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: shape === "build" ? 6000 : 4000,
      system: systemFor(shape),
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

// -> { text, model, tried }. Returning WHICH MODEL WROTE IT is not a nicety:
// this chain degrades silently through four providers of very different
// capability, and the only visible difference is that the answer gets worse.
// With OpenRouter out of credit the fallback is a ~22B model being asked to
// hold a long persona, the depth rules and 120 papers at once -- which reads
// exactly like the "listy and academic" complaint that prompted this. Nobody
// could tell, because the response never said.
async function answer(question, ctx, env, history, shape, rotate) {
  const tried = [];
  // Claude only if a key is explicitly set -- an opt-in override, not the
  // default. Left in place because switching back is then one secret.
  if (env.ANTHROPIC_API_KEY) {
    try {
      return { text: await claude(env.ANTHROPIC_API_KEY, question, ctx, history, shape),
               model: CLAUDE_MODEL, tried };
    } catch (e) {
      tried.push(`${CLAUDE_MODEL}: ${String(e.message || e).slice(0, 120)}`);
    }
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
  // Rotate which provider leads, so the council's roles are answered by
  // DIFFERENT models where more than one is configured. One model arguing with
  // itself produces agreeable disagreement -- llm.consensus() already relies on
  // provider diversity for exactly this reason when scoring. With a single
  // provider configured every role lands on it, and the caller is told so
  // rather than left to infer an independence that was not there.
  if (rotate && tries.length > 1) {
    const k = ((rotate % tries.length) + tries.length) % tries.length;
    tries.push(...tries.splice(0, k));
  }
  let last;
  for (const [url, key, model] of tries) {
    try {
      return { text: await chat(url, key, model, question, ctx, history, shape),
               model, tried };
    } catch (e) {
      last = e;                              // free tier rate-limited -> next
      tried.push(`${model}: ${String(e.message || e).slice(0, 120)}`);
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
      const a = await answer(q, ctx, env, body.history, body.shape);
      return json({ answer: a.text, model: a.model, tried: a.tried });
    }
    // One council role per call: the BROWSER sequences them, the same way it
    // already fans out scan batches. Keeps any single Function well inside its
    // limits and lets the UI show real progress per stage instead of one long
    // opaque wait.
    if (body.mode === "council") {
      const role = String(body.role || "");
      if (!COUNCIL_ROLES[role]) return json({ error: `unknown council role: ${role}` }, 400);
      const ctx = Array.isArray(body.ctx) ? body.ctx.slice(0, MAX_CTX) : [];
      if (!ctx.length) return json({ error: "no context papers supplied" }, 400);
      // Earlier stages of the exchange are appended to the question rather than
      // replayed as history: they are not things THIS role said, and presenting
      // them as its own prior turns is how a challenger ends up agreeing with a
      // position it is supposed to be attacking.
      const prior = String(body.prior || "").slice(0, 12000);
      const q = prior ? `${body.q}\n\n${prior}` : body.q;
      const a = await answer(q, ctx, env, null, role, Number(body.rotate) || 0);
      return json({ answer: a.text, model: a.model, tried: a.tried });
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
