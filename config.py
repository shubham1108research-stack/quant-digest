"""Central configuration for the quant research digest."""

LOOKBACK_DAYS = 8

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
# All ISSNs verified against Crossref (run-1, 2026-07-18) -- looked up BY ISSN
# and title-confirmed, because Crossref's fuzzy title search mis-resolves.
# Each item is tagged with its tier (T1/T2) in the email.
#
# Tier 1 -- top academic. Ratification signal; slow, high bar.
JOURNALS_T1 = {
    "Journal of Finance": "0022-1082",
    "Journal of Financial Economics": "0304-405X",
    "Review of Financial Studies": "0893-9454",
    "Journal of Financial and Quantitative Analysis": "0022-1090",
    "Review of Finance": "1572-3097",
    "Review of Asset Pricing Studies": "2045-9920",
    "Journal of Econometrics": "0304-4076",
    "Econometrica": "0012-9682",                 # methods; finance-relevant
    "Journal of Political Economy": "0022-3808",  # publishes asset pricing
    "Management Science": "0025-1909",           # finance dept
}
# Tier 2 -- strong field + practitioner. Where implementable work lands.
JOURNALS_T2 = {
    "Journal of Financial Markets": "1386-4181",
    "Journal of Empirical Finance": "0927-5398",
    "Journal of Banking and Finance": "0378-4266",
    "Journal of Financial Econometrics": "1479-8409",
    "Mathematical Finance": "0960-1627",
    "Finance and Stochastics": "0949-2984",
    "Quantitative Finance": "1469-7688",
    "Journal of Portfolio Management": "0095-4918",
    "Financial Analysts Journal": "0015-198X",
    "Journal of Financial Data Science": "2640-3943",
    "Journal of Asset Management": "1470-8272",
    "Journal of Derivatives": "1074-1240",
    "Review of Corporate Finance Studies": "2046-9128",
    "Journal of Risk": "1465-1211",
    # "Journal of Investment Management": no usable Crossref coverage
    #     (no journal-level ISSN indexed; DOIs absent) -- dropped, not guessed.
    # accounting trio -- uncomment if IVA/fundamentals work returns:
    # "Journal of Accounting Research": "0021-8456",
    # "Journal of Accounting and Economics": "0165-4101",
    # "The Accounting Review": "0001-4826",
    # deliberately EXCLUDED: AER, QJE, REStud -- mostly non-finance volume;
    # their finance papers reach you via NBER/NEP anyway.
}

# ---- Practitioner blogosphere ---------------------------------------
QUANTOCRACY_RSS = "https://quantocracy.com/feed/"

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
LLM_MODEL = "gemini-flash-latest"   # alias -> current free Flash; won't go stale
LLM_RANK_BATCH = 40          # items scored per API call
LLM_MAX_RETRIES = 3          # retries (with backoff) on 429/5xx
LLM_BATCH_PAUSE = 6          # seconds between calls -- stay under free-tier RPM
TOP_PICKS = 20               # how many top-ranked items to feature in the email
MIN_SHOW_SCORE = 20          # hide items the LLM scored below this from the email
                             # (0-19 = off-topic/noise band); the portal keeps all
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
