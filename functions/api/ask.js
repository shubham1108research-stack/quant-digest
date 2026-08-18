/**
 * Cloudflare Pages Function backing the portal's "Ask" tab.
 *
 * Split into two tiny modes so the heavy lifting stays where it belongs:
 *   mode:"embed"   -> embed the question (one small API call), return the vector.
 *                     The BROWSER then does retrieval against docs/vec.bin, so
 *                     5.8k dot products cost this Worker no CPU at all (Workers
 *                     have a hard per-request CPU budget; browsers don't).
 *   mode:"answer"  -> given the question + the papers the browser retrieved,
 *                     synthesise an answer with inline [n] citations.
 *
 * The API key is a Pages secret read from env -- it is never sent to the page.
 * The whole route sits behind the project's existing Cloudflare Access policy.
 */

const EMBED_MODEL = "text-embedding-3-small";
const EMBED_DIM = 256;              // must match tools/embed.py
const CHAT_MODEL = "gpt-5.4-mini";  // matches config.OPENAI_MODEL
const MAX_CTX = 16;                 // papers passed to the model
const MAX_Q = 500;                  // question length guard

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

async function embed(text, key) {
  const r = await fetch("https://api.openai.com/v1/embeddings", {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({ model: EMBED_MODEL, input: text, dimensions: EMBED_DIM }),
  });
  if (!r.ok) throw new Error(`embeddings ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).data[0].embedding;
}

async function answer(question, ctx, key) {
  const block = ctx.map((p, i) =>
    `[${i + 1}] ${p.title}\n` +
    `    authors: ${p.authors || "n/a"} | ${p.source || ""} ${p.date || ""}` +
    `${p.topic ? " | topic: " + p.topic : ""}\n` +
    `    ${(p.summary || "(no abstract captured)").replace(/\s+/g, " ")}`
  ).join("\n\n");

  const r = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: CHAT_MODEL,
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: `Question: ${question}\n\nPapers:\n\n${block}` },
      ],
    }),
  });
  if (!r.ok) throw new Error(`chat ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).choices[0].message.content;
}

export async function onRequestPost({ request, env }) {
  const key = env.OPENAI_API_KEY;
  if (!key) {
    return json({ error: "OPENAI_API_KEY is not set on this Pages project. " +
                         "Add it under Settings -> Environment variables (encrypted)." }, 503);
  }
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
      return json({ vec: await embed(q, key) });
    }
    if (body.mode === "answer") {
      const ctx = Array.isArray(body.ctx) ? body.ctx.slice(0, MAX_CTX) : [];
      if (!ctx.length) return json({ error: "no context papers supplied" }, 400);
      return json({ answer: await answer(q, ctx, key) });
    }
    return json({ error: "mode must be 'embed' or 'answer'" }, 400);
  } catch (e) {
    return json({ error: String(e.message || e) }, 502);
  }
}
