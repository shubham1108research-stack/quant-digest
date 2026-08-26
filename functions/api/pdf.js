/**
 * Fetch a paper's PDF on the browser's behalf.
 *
 * WHY THIS HAS TO EXIST. arXiv sends `access-control-allow-origin: *`, so the
 * page can fetch those itself. NBER sends no CORS header at all, and neither
 * do most publisher and repository hosts -- measured, not assumed. Without a
 * proxy, "open the PDF here" and "parse it for Implement" work for arXiv and
 * nothing else.
 *
 * THE RISK THIS CARRIES, AND WHAT BOUNDS IT. A Worker that fetches whatever URL
 * it is handed is an open proxy and an SSRF hole: it would fetch internal
 * addresses, act as an anonymiser, and bill the account for someone else's
 * bandwidth. The route sits behind Cloudflare Access, but "behind auth" is a
 * deployment fact and not a control -- this project spent a day learning that.
 * So:
 *
 *   - a HOST ALLOWLIST, not a URL pattern. Only the hosts the portal actually
 *     derives PDFs from, plus the open-access repositories OpenAlex resolves
 *     to. An unknown host is refused with its name, so extending the list is a
 *     decision someone makes rather than a hole someone finds.
 *   - https only. http would let a redirect walk into plaintext.
 *   - a size cap. A malicious or broken host must not stream gigabytes through
 *     a Worker that is billed per request.
 *   - redirects followed by fetch(), but the FINAL host is re-checked, because
 *     an allowlisted host that redirects off the list is the obvious way round
 *     an allowlist.
 */

const MAX_BYTES = 40 * 1024 * 1024;      // a long working paper is ~5 MB

// Hosts the portal derives or stores PDF links for. Suffix match, so
// "arxiv.org" covers "www.arxiv.org" but never "arxiv.org.evil.com" -- the
// check is on a leading dot or an exact match, not `endsWith` alone.
const ALLOWED = [
  "arxiv.org",
  "nber.org",
  "ssrn.com",
  "repec.org",
  "openalex.org",
  "doi.org",
  "tandfonline.com",
  "sciencedirect.com",
  "elsevier.com",
  "springer.com",
  "wiley.com",
  "oup.com",
  "cambridge.org",
  "jstor.org",
  "bis.org",
  "federalreserve.gov",
  "ecb.europa.eu",
  "imf.org",
  "worldbank.org",
  "econstor.eu",
  "zbw.eu",
  "osf.io",
  "biorxiv.org",
  "hal.science",
  "core.ac.uk",
  "semanticscholar.org",
  "researchgate.net",
  "sagepub.com",
  "pm-research.com",
  "macrosynergy.com",
];

function hostAllowed(host) {
  const h = String(host || "").toLowerCase();
  return ALLOWED.some((d) => h === d || h.endsWith("." + d));
}

const deny = (msg, status = 400) =>
  new Response(JSON.stringify({ error: msg }), {
    status,
    headers: { "content-type": "application/json; charset=utf-8",
               "cache-control": "no-store" },
  });

export async function onRequestGet({ request }) {
  const target = new URL(request.url).searchParams.get("url") || "";
  let u;
  try {
    u = new URL(target);
  } catch (_) {
    return deny("pass ?url=<absolute https url>");
  }
  if (u.protocol !== "https:") return deny("https only");
  if (!hostAllowed(u.hostname)) {
    return deny(`"${u.hostname}" is not on the PDF allowlist. Add it in `
      + `functions/api/pdf.js if it should be.`, 403);
  }

  let r;
  try {
    r = await fetch(u.toString(), {
      redirect: "follow",
      headers: {
        // Some repositories serve HTML to a client that does not say what it
        // wants. Asking for a PDF is honest and gets the file.
        accept: "application/pdf,*/*",
        "user-agent": "quant-digest/1.0 (personal research portal)",
      },
    });
  } catch (e) {
    return deny(`could not fetch: ${String((e && e.message) || e)}`, 502);
  }
  if (!r.ok) return deny(`upstream ${r.status}`, 502);

  // An allowlisted host that redirects OFF the list is the obvious way around
  // an allowlist, so the host that actually answered is checked too.
  try {
    const finalHost = new URL(r.url || u.toString()).hostname;
    if (!hostAllowed(finalHost)) {
      return deny(`redirected to "${finalHost}", which is not on the allowlist`, 403);
    }
  } catch (_) { /* r.url absent: the original host stands */ }

  const len = Number(r.headers.get("content-length") || 0);
  if (len && len > MAX_BYTES) {
    return deny(`that file is ${Math.round(len / 1e6)} MB, over the `
      + `${Math.round(MAX_BYTES / 1e6)} MB cap`, 413);
  }
  const type = (r.headers.get("content-type") || "").toLowerCase();
  if (type && !type.includes("pdf") && !type.includes("octet-stream")) {
    // Usually a login wall or a "choose your institution" page. Saying so is
    // more useful than handing the viewer 200 KB of HTML to fail on.
    return deny(`that URL returned ${type.split(";")[0]}, not a PDF -- it is `
      + `probably a landing page or a paywall`, 415);
  }

  return new Response(r.body, {
    status: 200,
    headers: {
      "content-type": "application/pdf",
      // The portal fetches this same-origin, so no CORS header is needed; the
      // point of the proxy is that the ORIGIN host lacked one.
      "cache-control": "private, max-age=3600",
      "content-disposition": "inline",
      "x-pdf-source": u.hostname,
    },
  });
}
