/**
 * Verify the Cloudflare Access JWT before ANY /api/* function runs.
 *
 * This exists because "the route sits behind Cloudflare Access" was written in
 * a docstring and treated as a security property. It is not one. It is a
 * deployment assumption that no code enforced, and it is false on most of the
 * hostnames this project is served from:
 *
 *   - Access applications are configured PER HOSTNAME. Protecting the custom
 *     domain does nothing for quant-digest.pages.dev.
 *   - Pages publishes a fresh <hash>.quant-digest.pages.dev for EVERY
 *     deployment, permanently. Those URLs are printed in plain text in the
 *     workflow logs.
 *
 * So an unauthenticated caller who read a run log could reach /api/ask, which
 * puts up to 120 caller-supplied "papers" straight into a prompt on a paid
 * model, and mode:"ingest", which dispatches a GitHub workflow using GH_TOKEN.
 * A free LLM proxy and a remote build trigger, billed to the repo owner.
 *
 * A middleware rather than a check inside each function: the next endpoint
 * added is protected by default instead of by someone remembering.
 *
 * WHY THE HEADER ALONE IS NOT ENOUGH
 * Cf-Access-Authenticated-User-Email is set by Access on its way through. It
 * is an ordinary HTTP header: anything that reaches the origin by another path
 * can set it to whatever it likes. saved.js used it directly as a KV key, so
 * spoofing it read and wrote someone else's saved papers. The email must come
 * from the VERIFIED token, never from a raw header.
 *
 * CONFIGURE (Pages project -> Settings -> Environment variables, not secrets):
 *   ACCESS_TEAM_DOMAIN   e.g. yourteam.cloudflareaccess.com   (required)
 *   ACCESS_AUD           the Access application's AUD tag     (recommended)
 *
 * ACCESS_TEAM_DOMAIN alone is the real boundary: only Cloudflare can sign a
 * token for your team. ACCESS_AUD narrows it further to this one application,
 * so a token minted for a different app in the same account is refused too.
 */

const CERTS_TTL_MS = 60 * 60 * 1000;      // JWKS rotates rarely; cache an hour
let _jwks = { at: 0, keys: null, team: "" };

const deny = (msg, status = 403) =>
  new Response(JSON.stringify({ error: msg }), {
    status,
    headers: { "content-type": "application/json; charset=utf-8",
               "cache-control": "no-store" },
  });

function b64urlToBytes(s) {
  const pad = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(pad + "=".repeat((4 - (pad.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function jwks(team) {
  const now = Date.now();
  if (_jwks.keys && _jwks.team === team && now - _jwks.at < CERTS_TTL_MS) {
    return _jwks.keys;
  }
  const r = await fetch(`https://${team}/cdn-cgi/access/certs`);
  if (!r.ok) throw new Error(`JWKS fetch failed: ${r.status}`);
  const keys = (await r.json()).keys || [];
  _jwks = { at: now, keys, team };
  return keys;
}

/** Verified claims, or null. Never throws to the caller's benefit. */
async function verifyAccessJwt(token, team, aud) {
  const parts = String(token || "").split(".");
  if (parts.length !== 3) return null;
  const [h64, p64, s64] = parts;

  let header, claims;
  try {
    header = JSON.parse(new TextDecoder().decode(b64urlToBytes(h64)));
    claims = JSON.parse(new TextDecoder().decode(b64urlToBytes(p64)));
  } catch (e) { return null; }
  if (header.alg !== "RS256") return null;    // never accept alg:none or HS*

  const key = (await jwks(team)).find((k) => k.kid === header.kid);
  if (!key) return null;

  const pub = await crypto.subtle.importKey(
    "jwk", key, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5", pub,
    b64urlToBytes(s64),
    new TextEncoder().encode(`${h64}.${p64}`));
  if (!ok) return null;

  const now = Math.floor(Date.now() / 1000);
  if (claims.exp && now >= claims.exp) return null;
  if (claims.nbf && now < claims.nbf) return null;
  // The issuer must be OUR team, or a valid token from any other Cloudflare
  // team would pass signature verification against that team's own JWKS.
  if (claims.iss && claims.iss !== `https://${team}`) return null;
  if (aud) {
    const a = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!a.includes(aud)) return null;
  }
  return claims;
}

export async function onRequest(context) {
  const { request, env, next, data } = context;

  const team = env.ACCESS_TEAM_DOMAIN;
  if (!team) {
    // Fails CLOSED. An auth check that is skipped when unconfigured is not an
    // auth check -- and this codebase already believed it was protected once.
    return deny(
      "This API is not configured for authentication. Set ACCESS_TEAM_DOMAIN "
      + "(e.g. yourteam.cloudflareaccess.com) and ideally ACCESS_AUD as "
      + "environment variables on the Pages project.", 503);
  }

  // Access forwards the token in either place depending on how it was reached.
  // Written without optional chaining on purpose: Workers support it, but the
  // only JS parser available for pre-commit checking here does not, and an
  // unparseable auth file is one nobody can lint.
  let token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) {
    const cookie = (request.headers.get("Cookie") || "")
      .split(/;\s*/).find((c) => c.startsWith("CF_Authorization="));
    if (cookie) token = cookie.slice("CF_Authorization=".length);
  }
  if (!token) return deny("No Cloudflare Access token on this request.", 401);

  let claims;
  try {
    claims = await verifyAccessJwt(token, team, env.ACCESS_AUD);
  } catch (e) {
    // A JWKS outage must not become an open door.
    return deny(`Could not verify access token: ${String(e.message || e)}`, 503);
  }
  if (!claims) return deny("Invalid or expired Cloudflare Access token.", 403);

  // Downstream reads the identity from HERE, never from a request header.
  data.email = claims.email || "";
  data.accessSub = claims.sub || "";
  return next();
}
