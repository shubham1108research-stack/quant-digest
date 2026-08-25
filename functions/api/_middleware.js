/**
 * Verify the Cloudflare Access JWT before ANY /api/* function runs.
 *
 * This exists because "the route sits behind Cloudflare Access" was written in
 * a docstring and treated as a security property. It is not one. It is a
 * deployment assumption that no code enforced, and it is false on most of the
 * hostnames this project is served from:
 *
 *   - Access applications are configured PER HOSTNAME. Protecting the custom
 *     domain does nothing for quant-digest-e62.pages.dev.
 *   - Pages publishes a fresh <hash>.quant-digest-e62.pages.dev for EVERY
 *     deployment, permanently. Those URLs are printed in plain text in the
 *     workflow logs.
 *
 * MEASURED, not theorised. On 2026-08-26, against the live site:
 *
 *   POST https://quant-digest-e62.pages.dev/api/ask            -> 302 to Access
 *   POST https://0e784bf5.quant-digest-e62.pages.dev/api/ask   -> 400 from ask.js
 *
 * The second is the function running, for an anonymous caller, with no token.
 * A well-formed body instead of that probe would have spent OpenAI credit; the
 * mode:"ingest" branch dispatches a GitHub workflow with GH_TOKEN. The repo is
 * public, config.py names the hostname, and public Actions logs print the
 * deployment hash -- so every input to that URL is already published.
 *
 * TWO GATES, because they fail independently:
 *
 *   1. HOSTNAME (below). Needs no configuration, so it cannot be switched off
 *      by a missing variable. This is the gate that closes the hole above.
 *   2. TOKEN (verifyAccessJwt). Real authentication, once ACCESS_TEAM_DOMAIN
 *      is set. Gate 1 without gate 2 still trusts Access to be in front of one
 *      hostname; gate 2 makes that trust unnecessary.
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
 *   ACCESS_HOSTS         extra hostnames, comma-separated     (optional)
 *
 * ACCESS_TEAM_DOMAIN alone is the real boundary: only Cloudflare can sign a
 * token for your team. ACCESS_AUD narrows it further to this one application,
 * so a token minted for a different app in the same account is refused too.
 * None of the three is a secret -- the team domain and AUD tag are both in the
 * login redirect any visitor receives -- but they are deployment config, so
 * they live in the environment and not in this file.
 */

// Hostnames an Access application actually covers. A deployment alias is NOT
// on this list and never can be: there is a new one every deploy, forever.
// Serving the API from exactly the hostname the portal is served from costs
// nothing -- the browser calls /api/ask as a same-origin relative URL, so a
// legitimate call is always on the hostname the user loaded the page from.
const CANONICAL_HOSTS = ["quant-digest-e62.pages.dev"];

function hostAllowed(host, env) {
  if (CANONICAL_HOSTS.indexOf(host) !== -1) return true;
  const extra = String(env.ACCESS_HOSTS || "")
    .split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
  return extra.indexOf(host) !== -1;
}

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

  // GATE 1 -- hostname. Deliberately before the token check and deliberately
  // free of configuration: this is the gate that was measured open, and a gate
  // that an unset variable can disable is the failure being fixed, not a fix.
  const host = new URL(request.url).hostname.toLowerCase();
  if (!hostAllowed(host, env)) {
    return deny(
      `This API is served only from ${CANONICAL_HOSTS[0]}. "${host}" is a `
      + "deployment alias, which no Cloudflare Access application covers.", 403);
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

  // GATE 2 -- the token itself. Unconfigured, we cannot verify a signature:
  // the team domain is what says WHICH Cloudflare team may sign, and taking it
  // from the token's own iss claim would let any team sign for us.
  //
  // Unconfigured is therefore degraded, not open, and the degradation is the
  // status quo rather than a new hole: gate 1 has already established that the
  // request arrived on a hostname Access covers, so a token is present at all
  // only because Access put it there. Returning 503 here instead would take
  // down the one hostname that is actually protected, in the name of security,
  // while changing nothing an attacker could do. Set ACCESS_TEAM_DOMAIN and
  // this branch stops running.
  const team = env.ACCESS_TEAM_DOMAIN;
  if (!team) {
    data.email = request.headers.get("Cf-Access-Authenticated-User-Email") || "";
    data.accessSub = "";
    data.accessUnverified = true;
    const r = await next();
    const out = new Response(r.body, r);
    out.headers.set("x-access-verification", "unconfigured");
    return out;
  }

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
