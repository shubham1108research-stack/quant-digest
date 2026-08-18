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
 * Providers are the free tiers already used by the pipeline: Gemini for
 * embeddings (must match tools/embed.py exactly, or the query lands in a
 * different vector space and retrieval silently returns nonsense), Mistral for
 * synthesis with Groq as a fallback. Keys are Pages secrets read from env --
 * they never reach the page, and the route sits behind Cloudflare Access.
 */

const EMBED_MODEL = "mistral-embed";        // must match tools/embed.py
const EMBED_DIM = 256;                      // requested width; see embed() note
const MISTRAL_MODEL = "mistral-small-latest";
const GROQ_MODEL = "llama-3.3-70b-versatile";
const MAX_CTX = 16;                         // papers passed to the model
const MAX_Q = 500;                          // question length guard

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8",
               "cache-control": "no-store" },
  });

const SYSTEM = `You are a research assistant for a systematic-macro / CTA quant desk.
You answer ONLY from the numbered papers supplied by the user; they were retrieved
from the user's own curated archive of quantitative-finance research.

Rules:
- Cite with bracketed numbers matching the supplied list, e.g. [3]. Cite every claim.
- Lead with the answer, then the evidence. Be specific about mechanisms, asset
  classes, sample periods and effect sizes when the source says so.
- Where papers disagree, say so explicitly and attribute each side.
- If the supplied papers do not actually answer the question, say that plainly and
  describe what IS there instead. Never invent a paper, author, number or finding.
- Write for a professional quant: dense, concrete, no hedging boilerplate, no
  restating the question. Markdown, short paragraphs or tight bullets.`;

// The query MUST be embedded by the same model/width as docs/vec.bin, or it
// lands in a different vector space and retrieval silently returns nonsense.
// We ask for the same width tools/embed.py asks for; the browser then checks
// the returned length against vec.json (the file that knows the real width)
// and refuses to search on a mismatch.
async function embed(text, key) {
  const r = await fetch("https://api.mistral.ai/v1/embeddings", {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: EMBED_MODEL,
      input: [text],
      output_dimension: EMBED_DIM,
    }),
  });
  if (!r.ok) throw new Error(`embed ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = await r.json();
  const v = j.data && j.data[0] && j.data[0].embedding;
  if (!v) throw new Error("embedding response had no vector");
  return v;
}

function contextBlock(ctx) {
  return ctx.map((p, i) =>
    `[${i + 1}] ${p.title}\n` +
    `    authors: ${p.authors || "n/a"} | ${p.source || ""} ${p.date || ""}` +
    `${p.topic ? " | topic: " + p.topic : ""}\n` +
    `    ${(p.summary || "(no abstract captured)").replace(/\s+/g, " ")}`
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

async function answer(question, ctx, env) {
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
    if (body.mode === "answer") {
      const ctx = Array.isArray(body.ctx) ? body.ctx.slice(0, MAX_CTX) : [];
      if (!ctx.length) return json({ error: "no context papers supplied" }, 400);
      return json({ answer: await answer(q, ctx, env) });
    }
    return json({ error: "mode must be 'embed' or 'answer'" }, 400);
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 502);
  }
}
