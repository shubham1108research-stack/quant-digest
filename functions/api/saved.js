// Cloudflare Pages Function backing the cross-device Saved-papers sync.
// Cloudflare Access already sits in front of the whole zone, so every
// request here has already been authenticated; the Cf-Access-Authenticated-
// User-Email header it injects is what keys each person's own KV entry --
// no separate auth/session logic needed.

function userKey(request) {
  const email = request.headers.get("Cf-Access-Authenticated-User-Email");
  return email ? `saved:${email.toLowerCase()}` : null;
}

export async function onRequestGet(context) {
  const key = userKey(context.request);
  if (!key) return new Response("unauthorized", { status: 401 });
  const value = await context.env.SAVED_PAPERS.get(key);
  return new Response(value || "{}", {
    headers: { "content-type": "application/json" },
  });
}

export async function onRequestPost(context) {
  const key = userKey(context.request);
  if (!key) return new Response("unauthorized", { status: 401 });
  const body = await context.request.text();
  try {
    JSON.parse(body);          // reject anything that isn't a JSON object
  } catch (e) {
    return new Response("invalid json", { status: 400 });
  }
  await context.env.SAVED_PAPERS.put(key, body);
  return new Response("ok");
}
