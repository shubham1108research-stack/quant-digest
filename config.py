"""Central configuration for the quant research digest."""

LOOKBACK_DAYS = 30       # every-3-days cron with a 1-month window; cross-run
                         # dedup drops the heavy overlap so emails stay net-new.

# Abstract enrichment (sources.enrich_abstracts): after the reliable OpenAlex
# lookup, at most this many journal article pages are scraped for a missing
# abstract, so a slow/blocked publisher can't stall the run.
ENRICH_SCRAPE_CAP = 60

# ---- RePEc NEP -------------------------------------------------------
NEP_CODES = ["fmk", "inv", "rmg", "ecm", "ets", "for",
             "cfn", "ino", "mst", "big", "cmp", "ain"]
NEP_URL = "https://nep.repec.org/rss/nep-{code}.rss.xml"

# ---- NBER ------------------------------------------------------------
# RUN-1 VERIFY: confirm this feed URL resolves; if not, find the current
# "new working papers" RSS on nber.org and update.
NBER_RSS = "https://back.nber.org/rss/new.xml"

# ---- arXiv (Atom API, no key) ---------------------------------------
ARXIV_CATS = ["q-fin.PR", "q-fin.PM", "q-fin.ST", "q-fin.GN", "q-fin.EC",
              "q-fin.RM", "q-fin.CP", "q-fin.TR", "q-fin.MF"]
ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_MAX = 200

# ---- Journals via Crossref (tiered) ---------------------------------
# Finance-only, keyed to NYU Stern Finance Dept's top-tier journals list
# (2020-12-10). All ISSNs verified against Crossref by ISSN. Economics
# (Econometrica, JPE, J of Econometrics, AER/QJE/REStud) and accounting are
# deliberately excluded -- finance only. Each item is tagged T1/T2 in the email.
#
# Tier 1 -- NYU Stern's three top-tier FINANCE journals (the top-tier list also
# names 5 economics journals, excluded here per the finance-only scope).
JOURNALS_T1 = {
    "Journal of Finance": "0022-1082",
    "Review of Financial Studies": "0893-9454",
    "Journal of Financial Economics": "0304-405X",
}
# Tier 2 -- NYU Stern's "other notable" FINANCE journals + the practitioner /
# quant field journals (JPM, FAJ, Quantitative Finance, ...) a quant desk reads.
JOURNALS_T2 = {
    # NYU "other notable" finance
    "Financial Management": "0046-3892",
    "Journal of Banking and Finance": "0378-4266",
    "Journal of Corporate Finance": "0929-1199",
    "Journal of Financial and Quantitative Analysis": "0022-1090",
    "Journal of Financial Econometrics": "1479-8409",
    "Journal of Financial Intermediation": "1042-9573",
    "Journal of Financial Markets": "1386-4181",
    "Journal of Financial Services Research": "0920-8550",
    "Journal of Money, Credit and Banking": "0022-2879",
    "Review of Asset Pricing Studies": "2045-9920",
    "Review of Corporate Finance Studies": "2046-9128",
    "Review of Finance": "1572-3097",
    # practitioner / quant field finance journals (not on NYU's academic list,
    # but finance and central to a quant practitioner)
    "Quantitative Finance": "1469-7688",
    "Mathematical Finance": "0960-1627",
    "Finance and Stochastics": "0949-2984",
    "Financial Analysts Journal": "0015-198X",
    "Journal of Asset Management": "1470-8272",
    "Journal of Risk": "1465-1211",
    "Journal of Empirical Finance": "0927-5398",
    # NOTE: Journal of Portfolio Management, Journal of Derivatives, and Journal
    # of Financial Data Science are PM-Research journals -- collected via pmr()
    # below (with abstracts scraped from pm-research.com), not here.
    # EXCLUDED per finance-only scope: Econometrica, Journal of Political
    # Economy, Journal of Econometrics (economics); Management Science (NYU
    # classes it "other business disciplines"); accounting journals.
}

# ---- PM Research journals (pm-research.com) -------------------------
# Practitioner finance journals. Crossref lists each journal's articles by ISSN
# but PMR does NOT deposit abstracts to Crossref, so pmr() fetches each article's
# pm-research.com page and pulls the abstract from its citation_abstract meta tag
# (static HTML, no Cloudflare). All tagged tier T2. Abstracts are only fetched
# for articles not already in the archive, so re-runs don't re-scrape.
PMR_JOURNALS = {
    "Journal of Portfolio Management": "0095-4918",
    "Journal of Investing": "1068-0896",
    "Journal of Derivatives": "1074-1240",
    "Journal of Fixed Income": "1059-8596",
    "Journal of Financial Data Science": "2640-3943",
    "Journal of Alternative Investments": "1520-3255",
    "Journal of Wealth Management": "1520-4154",
    "Journal of Beta Investment Strategies": "2771-6511",
    "Practical Applications": "2329-0196",
}
PMR_MAX_PER_JOURNAL = 30    # newest articles listed per journal per run

# ---- SSRN via Crossref ----------------------------------------------
# SSRN registers DOIs under the 10.2139 prefix, indexed by Crossref same-day.
# This gets fresh SSRN papers WITHOUT scraping SSRN (which Cloudflare-blocks).
# The prefix alone is an all-discipline firehose, so each finance query narrows
# it before the LLM layer filters; queries overlap and dedup handles it.
SSRN_CROSSREF_PREFIX = "10.2139"
SSRN_ROWS = 60               # results per finance query
SSRN_QUERIES = [
    "asset pricing cross-section returns factor",
    "return predictability anomalies momentum value",
    "portfolio optimization allocation risk parity",
    "machine learning deep learning trading finance",
    "market microstructure liquidity execution high-frequency",
    "volatility variance risk premium options derivatives",
    "hedge fund mutual fund performance flows",
]

# ---- Practitioner blogosphere ---------------------------------------
QUANTOCRACY_RSS = "https://quantocracy.com/feed/"
# Direct practitioner RSS (verified 2026-07-18). Asset-manager house pages
# (AQR, Man, Research Affiliates) and SSRN eJournals have no usable RSS -- they
# are JS-rendered / scrape-blocked and would need a headless browser.
PRACTITIONER_FEEDS = {
    "Alpha Architect": "https://alphaarchitect.com/feed/",
    "Quantpedia": "https://quantpedia.com/feed/",
    "Newfound / Flirting with Models": "https://blog.thinknewfound.com/feed/",
    "Macrosynergy": "https://macrosynergy.com/research/feed/",
}

# ---- Asset-manager house research (headless Playwright) -------------
# JS-rendered sites with no RSS. Each entry: (insights URL, href regex that
# matches an article page). SSRN is intentionally excluded -- it Cloudflare-
# blocks headless browsers (verified). Best-effort: a site change/block or a
# missing browser just skips that firm, never breaks the run.
FIRM_SITES = {
    "AQR": ("https://www.aqr.com/Insights/Research",
            r"^/Insights/Research/[^/]+/[^/]+$"),
    "Man Group": ("https://www.man.com/insights",
                  r"/insights/[a-z0-9][a-z0-9-]{10,}$"),
    "Research Affiliates": ("https://www.researchaffiliates.com/insights/publications",
                            r"/insights/publications/articles/\d+-[a-z0-9-]+$"),
}
FIRM_MAX_ITEMS = 15   # newest per firm; cross-week dedup surfaces only net-new

# ---- Preprint probes via OpenAlex -----------------------------------
# label -> OpenAlex source id (Sxxxxxxxxx), or None to resolve by name at
# runtime. Resolution is logged so you can verify and hardcode it after
# run 1 (same workflow as SSRN). Each source is fetched independently; a
# 429/failure on one is logged and skipped, never killing the others.
# Keep these to genuine PREPRINT/working-paper repositories -- journals are
# already covered via Crossref, so a journal source here just adds dedup work.
OPENALEX_PREPRINT_SOURCES = {
    "SSRN": "S4210172589",                       # verified run-1 (2026-07-18)
    "Munich Personal RePEc Archive": "S4306400553",  # MPRA working papers
    "EconStor": "S4306401696",                   # ZBW open working-paper server
    # OSF Preprints (S4306401127) dropped -- all-disciplines repository that
    # brought in sociology/psychology/etc.; the three above are econ/finance.
}
# How many retries (with linear backoff) on an OpenAlex 429 before giving up.
OPENALEX_MAX_RETRIES = 4

# Restrict the topic sweep to finance/economics via primary_topic. Branch A
# (curated finance topics) uses the whole field 20 ("Economics, Econometrics
# and Finance"); branch B (broad keyword search) uses the tighter Finance
# subfield 2003 -- keyword search over all of economics otherwise returns
# economic history, philosophy-of-economics, regional-development, and spam.
OPENALEX_FINANCE_FIELD = "20"          # branch A (already topic-constrained)
OPENALEX_FULLTEXT_SUBFIELD = "2003"    # branch B: Finance only

# ---- OpenAlex topic sweep -------------------------------------------
# SEARCH SEEDS, not OpenAlex taxonomy names. Resolved once (run-1) against the
# ~4,500-topic taxonomy behind a finance-field + relevance-score gate; seeds
# that don't clear it run as precise fulltext /works searches instead.
TOPIC_SEARCH_TERMS = [
    # asset pricing core
    "asset pricing", "cross-section of stock returns", "return predictability",
    "cross-predictability", "lead-lag returns", "factor models",
    "factor investing", "anomalies", "momentum", "value premium",
    "risk premia", "expected returns",
    # portfolio
    "portfolio management", "portfolio optimization", "asset allocation",
    "risk parity", "portfolio rebalancing", "transaction costs",
    "market impact", "optimal execution",
    # methods
    "covariance estimation", "shrinkage estimation", "regime switching",
    "structural breaks", "forecast combination", "Bayesian portfolio",
    "estimation risk", "multiple testing finance", "backtest overfitting",
    # ML
    "machine learning asset pricing", "deep learning returns",
    "reinforcement learning trading", "textual analysis finance",
    "alternative data",
    # vol / derivatives / micro
    "volatility forecasting", "variance risk premium", "implied volatility",
    "option returns", "market microstructure", "liquidity",
    "high-frequency trading", "statistical arbitrage",
    # institutional
    "mutual fund performance", "fund flows", "hedge fund", "crowding",
    "short selling", "tail risk",
]

# Taxonomy survivors resolved run-1 (2026-07-18) behind the finance-field +
# score>=OPENALEX_TOPIC_MIN_SCORE gate. Only these cleared it (many seeds map
# to off-domain topics -- momentum->Physics, anomalies->CompSci -- or nothing).
# Empty this dict to force a live re-bootstrap via sources.resolve_topics().
OPENALEX_TOPIC_IDS = {
    "Financial Markets and Investment Strategies": "T10047",
    "Stochastic processes and financial applications": "T10067",
    "Financial Risk and Volatility Modeling": "T10282",
}
# Seeds already covered by the mapped topics above -- excluded from fulltext to
# avoid redundant queries.
_TOPIC_COVERED = {
    "asset pricing", "factor investing", "market microstructure", "liquidity",
    "risk premia", "volatility forecasting",
}
# Every other seed runs as a precise fulltext search.
OPENALEX_FULLTEXT_TERMS = [t for t in TOPIC_SEARCH_TERMS
                          if t not in _TOPIC_COVERED]

OPENALEX_TOPIC_MIN_SCORE = 3000   # relevance gate used during live re-bootstrap
OPENALEX_FULLTEXT_LIMIT = 25      # max works per fulltext seed

# S2 is best-effort on the free unauthenticated tier: 1 = try once, skip on a
# 429 with no wait (fast runs). Raise for retries-with-backoff if you rely on it.
S2_MAX_RETRIES = 1
SEMANTIC_SCHOLAR_QUERIES = [
    "asset pricing",
    "factor investing",
    "portfolio optimization",
    "machine learning finance",
    "market microstructure",
    "volatility risk premium",
]

# ---- LLM triage (optional; Google Gemini, free tier) ----------------
# Dormant unless GEMINI_API_KEY (or GOOGLE_API_KEY) is set; any failure
# degrades cleanly to the plain no-LLM feed. Ranks deduped items 0-100 for a
# "Top picks" section. Get a free key at https://aistudio.google.com/apikey.
LLM_MODEL = "gemini-flash-latest"   # Gemini (primary); alias -> current Flash
GROQ_MODEL = "llama-3.3-70b-versatile"   # Groq (fallback if GROQ_API_KEY set)
LLM_RANK_BATCH = 40          # items scored per API call
LLM_MAX_RETRIES = 3          # retries (with backoff) on 429/5xx
LLM_BATCH_PAUSE = 6          # seconds between calls -- stay under free-tier RPM
TOP_PICKS = 20               # how many top-ranked items to feature in the email
MIN_SHOW_SCORE = 20          # hide items the LLM scored below this from the email
                             # (0-19 = off-topic/noise band); the portal keeps all

# ---- Prominence tiering ---------------------------------------------
# The email is divided into Tier 1 / Tier 2 / rest. An item is Tier 1 if it's a
# T1 journal, a prominent author (OpenAlex h-index >= PROM_H1), or a must-read
# by the LLM (score >= RANK_T1); Tier 2 is the analogous middle band.
PROM_H1 = 40                 # author h-index for "prominent" (tier 1)
PROM_H2 = 20                 # author h-index for "established" (tier 2)
RANK_T1 = 85                 # LLM score that alone earns tier 1
RANK_T2 = 60                 # LLM score that alone earns tier 2
RANK_INTERESTS = (
    "- Empirical asset pricing: cross-section of returns, factor models, "
    "anomalies, return predictability, risk premia, expected returns.\n"
    "- Portfolio construction: optimization, allocation, risk parity, "
    "rebalancing, transaction costs, optimal execution.\n"
    "- Quant methods: covariance/shrinkage estimation, regime switching, "
    "forecast combination, estimation risk, multiple testing, backtest "
    "overfitting.\n"
    "- Machine learning in finance: ML asset pricing, deep learning for "
    "returns, reinforcement-learning trading, textual/alternative data.\n"
    "- Volatility & derivatives: vol forecasting, variance risk premium, "
    "implied vol, option returns, market microstructure, liquidity, HFT, "
    "statistical arbitrage.\n"
    "- Institutional: fund performance, flows, hedge funds, crowding, short "
    "selling, tail risk.\n"
    "Prefer novel, rigorous, and practitioner-implementable work; deprioritize "
    "incremental results and papers with no finance angle."
)

# ---- Email -----------------------------------------------------------
SUBJECT_PREFIX = "[Research Digest]"
# Link shown in the email to browse the full archive portal. Set to your hosted
# URL (GitHub Pages https://<user>.github.io/quant-digest/, Netlify, Cloudflare
# Pages) once docs/ is published; the local fallback works with
# `py -m http.server 8000 --directory docs`. Empty hides the button.
PORTAL_URL = "https://quant-digest-e62.pages.dev"  # Cloudflare Pages (login-walled)
