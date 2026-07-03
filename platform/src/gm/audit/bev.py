"""Bot's-Eye-View — deterministic bot-visibility diagnostic.

Pure-Python port of aeo-seo-auditor-fable ``service/scripts/bots_eye_view.sh``
+ ``_bev_analyze.py`` (no bash, no curl — all I/O goes through an injected
``gm.audit.fetch.Fetcher``). It answers:

  1. What do GPTBot / PerplexityBot / ClaudeBot / Googlebot actually receive?
  2. Does the server serve the same empty shell for every URL? (SPA-no-SSR)
  3. Is the site cloaking (different content per user-agent)?
  4. Is the content JS-rendered or actually in the raw HTML?
  5. Are bot UAs blocked (401/403/429) while browsers get content?

Classification values are the source repo's full set. The five content
classes from the Phase B contract (fully_accessible | partial_ssr |
js_dependent | minimal_content | spa_no_ssr) apply when a real body was
received; the source's transport/gate classes (fetch_failed |
unresolved_redirect | bot_blocked | http_error) and its
ssr_shell_js_hidden_content refinement are preserved because downstream
grading refuses to grade transport-inconclusive probes (audits.gate_state).
"""

from __future__ import annotations

import html as html_lib
import html.parser
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from gm.audit.fetch import DEFAULT_USER_AGENT, Fetcher, FetchResult
from gm.audit.safety import UnsafeURL, validate_url

# ----------------------------------------------------------------------
# Probe set (UA strings verbatim from bots_eye_view.sh)
# ----------------------------------------------------------------------

USER_AGENTS: dict[str, str] = {
    "default": DEFAULT_USER_AGENT,
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "gptbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "GPTBot/1.0; +https://openai.com/gptbot)"
    ),
    "perplexitybot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"
    ),
    "claudebot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "ClaudeBot/1.0; +claudebot@anthropic.com)"
    ),
}
_BOT_PROBES = ("googlebot", "gptbot", "perplexitybot", "claudebot")

# The shell used a timestamp+pid path; a fixed token keeps output identical
# across runs (a stated design goal of the source diagnostic).
NOT_FOUND_PATH = "/gm-bev-nonexistent-probe-40404"


@dataclass
class BevResult:
    classification: str  # fully_accessible|partial_ssr|js_dependent|minimal_content|spa_no_ssr
    #                      (+ source transport classes, see module docstring)
    per_ua: dict[str, dict]  # ua -> {status, bytes, title, h1, blocked, ...}
    cloaking_suspected: bool
    spa_shell: bool
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# HTML -> visible text (stdlib html.parser, not regex)
# ----------------------------------------------------------------------

class _VisibleTextExtractor(html.parser.HTMLParser):
    """Collects text from an HTML doc, skipping script/style/noscript/template."""

    SKIP_TAGS = {"script", "style", "noscript", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def visible_text(html_str: str, max_chars: int = 50_000) -> str:
    """Extract visible text via stdlib html.parser."""
    if not html_str:
        return ""
    ex = _VisibleTextExtractor()
    try:
        ex.feed(html_str)
    except Exception:
        # If parser chokes, fall back to brute-force tag strip so we never
        # crash the whole audit.
        stripped = re.sub(
            r"<script[^>]*>.*?</script>", " ", html_str, flags=re.DOTALL | re.IGNORECASE
        )
        stripped = re.sub(
            r"<style[^>]*>.*?</style>", " ", stripped, flags=re.DOTALL | re.IGNORECASE
        )
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = html_lib.unescape(stripped)
        return re.sub(r"\s+", " ", stripped).strip()[:max_chars]
    text = re.sub(r"\s+", " ", ex.get_text()).strip()
    return text[:max_chars]


def visible_word_count(html_str: str) -> int:
    """Count visible (human-readable) words in an HTML document."""
    return len([w for w in visible_text(html_str).split() if w.strip()])


# ----------------------------------------------------------------------
# FAQ detection with question-intent gate
# ----------------------------------------------------------------------

_QUESTION_WORDS = (
    "how ", "what ", "when ", "where ", "why ", "which ", "who ",
    "can ", "could ", "do ", "does ", "did ",
    "is ", "are ", "was ", "were ",
    "will ", "would ", "should ", "shall ", "may ", "might ",
    "have ", "has ", "had ",
    # German — most German FAQs end in '?', but accordion summaries often
    # truncate it, and DE-market sites are a primary audit target.
    "wie ", "was ", "wann ", "wo ", "warum ", "wieso ", "weshalb ",
    "welche", "wer ", "wem ", "wen ", "gibt es ",
    "kann ", "können ", "muss ", "müssen ", "darf ", "soll ",
    "ist ", "sind ", "habe ", "brauche ", "bietet ",
)


def looks_like_question(text: str) -> bool:
    """Heuristic: does this text look like a user-facing question?"""
    if not text:
        return False
    t = text.strip().lower()
    if "?" in t:
        return True
    if t.startswith(("faq", "q:", "question")):
        return True
    return any(t.startswith(kw) for kw in _QUESTION_WORDS)


def faq_visible_count(html_str: str) -> tuple[int, str]:
    """Count visible FAQ pairs using multiple detection patterns with
    question-intent gating. Returns (count, detection_method)."""
    if not html_str:
        return 0, "empty_html"

    # Pattern 1: <details><summary> WITH question-like summary text
    summaries = re.findall(r"<summary[^>]*>(.*?)</summary>", html_str, re.IGNORECASE | re.DOTALL)
    q_summaries = []
    for s in summaries:
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", s).strip())
        if looks_like_question(text):
            q_summaries.append(s)
    if q_summaries:
        return len(q_summaries), "details_summary_question"

    # Pattern 2: <dl><dt><dd> — require at least 3 pairs to look FAQ-like,
    # AND at least half of the <dt> texts must be questions.
    dts = re.findall(r"<dt[^>]*>(.*?)</dt>", html_str, re.IGNORECASE | re.DOTALL)
    dds = re.findall(r"<dd[^>]*>", html_str, re.IGNORECASE)
    if len(dts) >= 3 and len(dds) >= 3:
        q_dts = [
            dt for dt in dts
            if looks_like_question(re.sub(r"<[^>]+>", " ", html_lib.unescape(dt)))
        ]
        if len(q_dts) >= len(dts) / 2:
            return min(len(q_dts), len(dds)), "dl_dt_dd_question"

    # Pattern 3: data-slot="accordion-item" (shadcn/ui) — explicit accordion semantic
    accordion = re.findall(r'data-slot=["\']accordion-item["\']', html_str, re.IGNORECASE)
    if accordion:
        return len(accordion), "data_slot_accordion"

    # Pattern 4: class="*accordion-item*" or "*faq-item*" / "*faq-entry*"
    class_items = re.findall(
        r'class=["\'][^"\']*(?:accordion-item|faq-item|faq-entry|faq-question)',
        html_str,
        re.IGNORECASE,
    )
    if class_items:
        return len(class_items), "class_accordion_item"

    # Pattern 5: H3 tags ending in ? — strong signal of FAQ headings
    h3_qs = re.findall(r"<h3[^>]*>\s*[^<]*\?\s*</h3>", html_str, re.IGNORECASE)
    if h3_qs and len(h3_qs) >= 3:
        return len(h3_qs), "h3_question_headings"

    return 0, "none_detected"


# ----------------------------------------------------------------------
# FAQ schema (JSON-LD) counts
# ----------------------------------------------------------------------

def _is_faqpage(node: dict) -> bool:
    """True if a JSON-LD node declares FAQPage, including @type arrays."""
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.strip().lower() == "faqpage" for x in types)


def _mainentity_count(node: dict) -> int:
    """Q&A pair count for a FAQPage node; single-dict mainEntity counts as 1."""
    me = node.get("mainEntity", [])
    if isinstance(me, dict):
        return 1
    if isinstance(me, list):
        return len(me)
    return 0


def _iter_jsonld_nodes(html_str: str):
    """Yield every dict node from all JSON-LD blocks, descending into @graph."""
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_str,
        re.IGNORECASE | re.DOTALL,
    )
    for b in blocks:
        data = None
        # Some CMSs HTML-escape entities inside JSON-LD; retry unescaped.
        for attempt in (b.strip(), html_lib.unescape(b).strip()):
            try:
                data = json.loads(attempt)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            continue
        stack = list(data) if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            yield item
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(g for g in graph if isinstance(g, dict))


def faq_schema_count(html_str: str) -> int:
    """Count FAQ pairs across ALL FAQPage JSON-LD blocks, if present."""
    return sum(
        _mainentity_count(node) for node in _iter_jsonld_nodes(html_str) if _is_faqpage(node)
    )


def faq_schema_questions(html_str: str) -> list[str]:
    """Question texts ('name' of mainEntity items) from FAQPage JSON-LD."""
    questions: list[str] = []
    for node in _iter_jsonld_nodes(html_str):
        if not _is_faqpage(node):
            continue
        me = node.get("mainEntity", [])
        items = [me] if isinstance(me, dict) else me if isinstance(me, list) else []
        for q in items:
            if isinstance(q, dict) and isinstance(q.get("name"), str):
                questions.append(q["name"])
    return questions


def _norm_for_match(s: str) -> str:
    """Normalize text for substring matching: entities, curly quotes, case, ws."""
    s = html_lib.unescape(s or "")
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().casefold()


# ----------------------------------------------------------------------
# SPA signals + classification
# ----------------------------------------------------------------------

def detect_spa_signals(html_str: str) -> list[str]:
    """Return list of SPA framework hints detected in raw HTML."""
    signals = []
    if re.search(r"<app-root", html_str, re.IGNORECASE):
        signals.append("angular_app_root")
    if (
        re.search(r'<div[^>]*id=["\']__next["\']', html_str, re.IGNORECASE)
        or "__NEXT_DATA__" in html_str
        or "self.__next_f" in html_str
    ):
        signals.append("nextjs")
    if re.search(r'<div[^>]*id=["\']root["\']', html_str, re.IGNORECASE) and (
        "react" in html_str.lower()
    ):
        signals.append("react_root")
    if ('id="app"' in html_str or "id='app'" in html_str) and "vue" in html_str.lower():
        signals.append("vue_app")
    if re.search(r'<div[^>]*id=["\']__nuxt["\']', html_str, re.IGNORECASE):
        signals.append("nuxt")
    return signals


_UI_ACTION_H1_KEYWORDS = (
    "select", "choose", "pick", "continue", "enter your",
    "get started", "sign in", "log in", "welcome",
)


def classify_ssr(
    visible_words: int,
    same_as_404: bool,
    spa_signals: list[str],
    h1_first: str | None = None,
    html_snippet: str | None = None,
    http_code: int | None = None,
) -> str:
    """Deterministic classification based on signals (thresholds verbatim from source).

    Returns one of:
      - 'fetch_failed'                — transport error / timeout; no response at all.
      - 'unresolved_redirect'         — final hop is still 3xx; body is NOT the page.
      - 'bot_blocked'                 — 401/403/429; access denied, not thin content.
      - 'http_error'                  — other 4xx/5xx; body is an error page.
      - 'spa_no_ssr'                  — identical shell for every URL. Dark to AI.
      - 'ssr_shell_js_hidden_content' — thin SSR modal/gate; real content in JS bundle.
      - 'js_dependent'                — content exists but <200 words + SPA framework hints.
      - 'minimal_content'             — <200 words, genuinely thin page.
      - 'partial_ssr'                 — 200-500 words.
      - 'fully_accessible'            — >500 words of real content in raw HTML.
    """
    # Transport gate first: a non-2xx body is not the page's content, so
    # word-count classes don't apply.
    if http_code is not None:
        if http_code <= 0:
            return "fetch_failed"
        if 300 <= http_code < 400:
            return "unresolved_redirect"
        if http_code in (401, 403, 429):
            return "bot_blocked"
        if http_code >= 400:
            return "http_error"

    if same_as_404:
        return "spa_no_ssr"

    # SSR-shell-with-JS-hidden-content: thin visible text + UI-action H1 +
    # rich Next.js streaming bundle.
    ui_action_h1 = bool(
        h1_first and any(kw in h1_first.lower() for kw in _UI_ACTION_H1_KEYWORDS)
    )
    has_next_streaming = bool(html_snippet and "self.__next_f.push" in html_snippet)
    rich_bundle = has_next_streaming and html_snippet is not None and len(html_snippet) > 40_000
    if visible_words < 200 and ui_action_h1 and rich_bundle:
        return "ssr_shell_js_hidden_content"

    if visible_words < 200:
        return "js_dependent" if spa_signals else "minimal_content"
    if visible_words < 500:
        return "partial_ssr"
    return "fully_accessible"


# ----------------------------------------------------------------------
# Per-probe analysis helpers
# ----------------------------------------------------------------------

def extract_first_h1(html_str: str) -> str | None:
    """Return the text inside the first <h1>, or None."""
    if not html_str:
        return None
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_str, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def extract_title(html_str: str) -> str | None:
    """Return the text inside the first <title>, or None."""
    if not html_str:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html_str, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _normalize_for_equality(html_str: str) -> str:
    """Strip volatile tokens (nonces, timestamps) for content-equality comparison."""
    if not html_str:
        return ""
    s = re.sub(r'nonce=["\'][^"\']+["\']', "", html_str, flags=re.IGNORECASE)
    s = re.sub(r'csrf[_-]?token["\']?\s*[:=]\s*["\'][^"\']+["\']', "", s, flags=re.IGNORECASE)
    s = re.sub(r"\d{10,}", "", s)  # long numeric IDs / unix timestamps
    return re.sub(r"\s+", " ", s).strip()


def _analyze_probe(result: FetchResult | None, error: str | None = None) -> dict:
    """Derive all per-probe signals from a FetchResult (None = fetch failed)."""
    if result is None:
        return {
            "status": 0,
            "bytes": 0,
            "title": None,
            "h1": None,
            "blocked": False,
            "visible_words": 0,
            "spa_signals": [],
            "final_url": "",
            "redirects_followed": 0,
            "error": error or "fetch failed",
        }
    html_str = result.text
    # Higher cap than the default 50k: FAQ sections usually sit at the end of
    # the page and must not be truncated away before the schema-question match.
    visible = visible_text(html_str, max_chars=300_000)
    wc = len([w for w in visible.split() if w.strip()])
    faq_vc, faq_method = faq_visible_count(html_str)
    schema_qs = faq_schema_questions(html_str)
    vis_norm = _norm_for_match(visible)
    schema_qs_visible = sum(
        1 for q in schema_qs if _norm_for_match(q) and _norm_for_match(q) in vis_norm
    )
    return {
        "status": result.status_code,
        "bytes": len(html_str.encode("utf-8", errors="replace")),
        "title": extract_title(html_str),
        "h1": extract_first_h1(html_str),
        "blocked": result.status_code in (401, 403, 429),
        "visible_words": wc,
        "spa_signals": detect_spa_signals(html_str),
        "final_url": result.final_url,
        "redirects_followed": max(len(result.redirect_chain) - 1, 0),
        "faq_visible": {"count": faq_vc, "method": faq_method},
        "faq_schema": faq_schema_count(html_str),
        "faq_schema_questions_visible": schema_qs_visible,
    }


def _normalize_input_url(url: str) -> str:
    """Default to https:// when no scheme is given (case-insensitive match)."""
    if re.match(r"^https?://", url, re.IGNORECASE):
        return url
    return f"https://{url}"


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------

def bots_eye_view(url: str, fetcher_factory: Callable[[str], Fetcher]) -> BevResult:
    """Run the 5-UA probe set + 404 probe against `url` and classify.

    `fetcher_factory` maps a User-Agent string to a Fetcher (production:
    ``lambda ua: make_fetcher(user_agent=ua)``; tests: fake fetchers).
    Raises UnsafeURL if the target itself is SSRF-blocked.
    """
    target = validate_url(_normalize_input_url(url))
    parsed = urlparse(target)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    probe_url = f"{origin}{NOT_FOUND_PATH}"

    raw: dict[str, FetchResult | None] = {}
    errors: dict[str, str] = {}
    for name, ua in USER_AGENTS.items():
        fetcher = fetcher_factory(ua)
        try:
            raw[name] = fetcher(target)
        except (httpx.HTTPError, UnsafeURL) as e:
            raw[name] = None
            errors[name] = f"{type(e).__name__}: {e}"
    nf_fetcher = fetcher_factory(USER_AGENTS["default"])
    try:
        raw["not_found"] = nf_fetcher(probe_url)
    except (httpx.HTTPError, UnsafeURL) as e:
        raw["not_found"] = None
        errors["not_found"] = f"{type(e).__name__}: {e}"

    per_ua = {name: _analyze_probe(res, errors.get(name)) for name, res in raw.items()}

    default_html = raw["default"].text if raw["default"] is not None else ""
    nf_html = raw["not_found"].text if raw["not_found"] is not None else ""
    d = per_ua["default"]
    nf = per_ua["not_found"]

    # If the guaranteed-404 probe was REDIRECTED to the same final URL as the
    # default probe (unknown paths -> homepage), both bodies are the homepage.
    # That's a soft-404 setup, NOT an SPA shell.
    soft_404_redirect = bool(
        nf.get("redirects_followed", 0) > 0
        and nf.get("final_url")
        and nf.get("final_url") == d.get("final_url")
    )

    # same-as-404: default page body indistinguishable from a guaranteed 404.
    same_as_404 = False
    if not soft_404_redirect and default_html and nf_html:
        same_as_404 = _normalize_for_equality(default_html) == _normalize_for_equality(
            nf_html
        ) or (
            visible_word_count(default_html) == visible_word_count(nf_html) > 0
            and visible_text(default_html) == visible_text(nf_html)
        )

    default_wc = d.get("visible_words", 0)
    default_code = d.get("status", 0)
    default_ok = 200 <= default_code < 300
    default_final = d.get("final_url", "")

    # Bot blocking vs cloaking are different findings. A 403/429 to GPTBot
    # while the browser UA gets 200 is access denial — comparing its error
    # page's word count against the real page would misfire as "cloaking".
    cloaking_detected = False
    bot_blocking: list[dict] = []
    divergent_final_urls: list[dict] = []
    for name in _BOT_PROBES:
        p = per_ua[name]
        code = p.get("status", 0)
        ok = 200 <= code < 300
        if default_ok and not ok:
            bot_blocking.append({"probe": name, "status": code})
            continue
        final = p.get("final_url", "")
        if default_final and final and final != default_final:
            divergent_final_urls.append({"probe": name, "final_url": final})
        if not (default_ok and ok):
            continue
        probe_wc = p.get("visible_words", 0)
        if default_wc == 0 and probe_wc == 0:
            continue
        delta = probe_wc - default_wc
        rel = (abs(delta) / default_wc) if default_wc else 1.0
        p["delta_vs_default"] = delta
        if abs(delta) > 50 and rel > 0.20:
            p["cloaking_flagged"] = True
            cloaking_detected = True

    classification = classify_ssr(
        visible_words=default_wc,
        same_as_404=same_as_404,
        spa_signals=d.get("spa_signals", []),
        h1_first=d.get("h1"),
        html_snippet=default_html,
        http_code=default_code,
    )

    visible_faq = d.get("faq_visible", {}).get("count", 0)
    schema_faq = d.get("faq_schema", 0)
    schema_q_visible = d.get("faq_schema_questions_visible", 0)
    if visible_faq == 0 and schema_faq == 0:
        faq_integrity = "na"
    elif schema_faq == 0 and visible_faq > 0:
        # Visible FAQ widget but no FAQPage JSON-LD — a markup opportunity,
        # not an integrity failure.
        faq_integrity = "schema_missing"
    elif visible_faq == schema_faq:
        faq_integrity = "ok"
    elif schema_faq > 0 and schema_q_visible >= schema_faq:
        # Every schema question's text IS in the visible HTML — Google's
        # actual requirement — even though no FAQ widget pattern matched.
        faq_integrity = "ok_text_match"
    elif schema_faq > 0 and schema_q_visible >= (schema_faq + 1) // 2:
        faq_integrity = "partial_text_match"
    else:
        faq_integrity = "mismatch"
    d["faq_integrity"] = faq_integrity

    # Notes: the source orchestrator's critical_issues, verbatim where possible.
    # Transport-level classifications mean "probe inconclusive — fix the
    # fetch, re-run" and must never read as content conclusions.
    notes: list[str] = []
    if classification == "fetch_failed":
        notes.append(
            "Probe inconclusive: fetch failed (timeout/connection error) — "
            "no content conclusions possible"
        )
    elif classification == "unresolved_redirect":
        notes.append(
            f"Probe inconclusive: final response is still a redirect (HTTP {default_code}) "
            f"after following up to 5 hops — re-run against the final URL"
        )
    elif classification == "bot_blocked":
        notes.append(
            f"Default UA is blocked (HTTP {default_code}) — site denies non-browser clients"
        )
    elif classification == "http_error":
        notes.append(f"Page returns HTTP {default_code} — analyzed body is an error page")
    elif classification == "spa_no_ssr":
        notes.append(
            "Page serves the identical shell for real and 404 URLs — "
            "content is JS-only, dark to AI crawlers"
        )
    if bot_blocking:
        blocked = ", ".join(f"{b['probe']}={b['status']}" for b in bot_blocking)
        notes.append(f"AI-bot user agents blocked while browser UA succeeds: {blocked}")
    bots_ok = [n for n in _BOT_PROBES if 200 <= per_ua[n].get("status", 0) < 300]
    if not default_ok and bots_ok:
        notes.append(
            "Note: bot UAs (" + ", ".join(bots_ok) + ") fetched the page "
            "successfully (2xx) while the browser-profile UA did not — "
            "classification reflects the browser probe; see per_ua for the bot view"
        )
    if cloaking_detected:
        notes.append("Cloaking suspected: bot UAs receive significantly different content")
    if divergent_final_urls:
        div = ", ".join(f"{x['probe']} -> {x['final_url']}" for x in divergent_final_urls)
        notes.append(f"Per-UA redirect divergence: {div}")
    if faq_integrity in ("mismatch", "partial_text_match"):
        notes.append(
            f"FAQ schema/HTML mismatch: {schema_faq} pairs in JSON-LD, "
            f"{schema_q_visible} question texts found in visible HTML"
        )
    if soft_404_redirect:
        notes.append(
            "404 probe redirected to the same final URL as the page (soft-404 setup) — "
            "shell comparison skipped"
        )

    return BevResult(
        classification=classification,
        per_ua=per_ua,
        cloaking_suspected=cloaking_detected,
        spa_shell=same_as_404,
        notes=notes,
    )
