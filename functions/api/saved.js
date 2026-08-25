// Cloudflare Pages Function backing the cross-device Saved-papers sync.
//
// The identity comes from _middleware.js, which VERIFIES the Access JWT
// signature against the team JWKS. It used to be read straight off the
// Cf-Access-Authenticated-User-Email header, which is an ordinary HTTP header:
// anything reaching the origin by a path Access does not cover -- and Access
// applications are per-hostname, while Pages publishes a permanent
// <hash>.pages.dev for every deployment -- could set it to any address and
// read or overwrite that person's saved papers.
//
// The old comment claimed "Access already sits in front of the whole zone".
// It sat in front of one hostname.

function userKey(context) {
  const email = context.data && context.data.email;
  return email ? `saved:${String(email).toLowerCase()}` : null;
}

export async function onRequestGet(context) {
  const key = userKey(context);
  if (!key) return new Response("unauthorized", { status: 401 });
  const value = await context.env.SAVED_PAPERS.get(key);
  return new Response(value || "{}", {
    headers: { "content-type": "application/json" },
  });
}

export async function onRequestPost(context) {
  const key = userKey(context);
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
