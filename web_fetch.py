import re
from urllib.parse import urlparse

import requests

try:
    import trafilatura
except ImportError:
    trafilatura = None

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]{2,}")
TAG_RE = re.compile(r"<(script|style|noscript|nav|header|footer|aside)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
NL_RE = re.compile(r"\n{3,}")

DEFAULT_PAGE_CHARS = 40000
DEFAULT_TOTAL_CHARS = 120000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def extract_urls(text, limit=6):
    seen = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}\u3002\uff0c\uff1b\uff1a")
        if u not in seen:
            seen.append(u)
        if len(seen) >= limit:
            break
    return seen


def _strip_tags(html):
    html = TAG_RE.sub(" ", html)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", html, flags=re.IGNORECASE)
    text = ANY_TAG.sub(" ", html)
    for k, v in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&mdash;", "-"), ("&ndash;", "-")):
        text = text.replace(k, v)
    text = re.sub(r"&#\d+;", " ", text)
    text = WS_RE.sub(" ", text)
    text = NL_RE.sub("\n\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())


def extract_main(html, url=""):
    if trafilatura is not None:
        try:
            out = trafilatura.extract(
                html,
                url=url or None,
                include_comments=False,
                include_tables=True,
                include_links=False,
                favor_precision=True,
                deduplicate=True,
            )
            if out and len(out.strip()) > 80:
                return out.strip()
        except Exception:  # noqa: BLE001
            pass
    return _strip_tags(html)


def tokens_of(text):
    return TOKEN_RE.findall((text or "").lower())


def select_relevant(text, query, budget):
    if len(text) <= budget:
        return text
    if not query:
        return text[:budget]
    q = set(tokens_of(query))
    if not q:
        return text[:budget]
    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    scored = []
    for i, p in enumerate(paras):
        toks = tokens_of(p)
        if not toks:
            continue
        hit = sum(1 for t in toks if t in q)
        score = hit / (len(toks) ** 0.5)
        scored.append((score, i, p))
    scored.sort(key=lambda x: -x[0])
    chosen = []
    used = 0
    for score, i, p in scored:
        if score <= 0:
            break
        if used + len(p) > budget:
            continue
        chosen.append((i, p))
        used += len(p)
        if used >= budget * 0.95:
            break
    if not chosen:
        return text[:budget]
    chosen.sort(key=lambda x: x[0])
    return "\n\n".join(p for _, p in chosen)


def fetch(url, query="", max_chars=DEFAULT_PAGE_CHARS, timeout=25):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as exc:
        return {"url": url, "ok": False, "error": str(exc), "text": "", "chars": 0}
    ctype = (r.headers.get("content-type") or "").lower()
    if "html" in ctype or "xml" in ctype or not ctype:
        text = extract_main(r.text, url)
    else:
        text = r.text
    text = select_relevant(text, query, max_chars)
    return {"url": url, "ok": True, "error": None, "text": text, "chars": len(text)}


def fetch_many(urls, query="", max_chars=DEFAULT_PAGE_CHARS, total_chars=DEFAULT_TOTAL_CHARS):
    results = []
    used = 0
    for u in urls:
        if used >= total_chars:
            break
        budget = min(max_chars, total_chars - used)
        res = fetch(u, query=query, max_chars=budget)
        used += res["chars"]
        results.append(res)
    return results


def format_for_prompt(results):
    if not results:
        return ""
    chunks = []
    for r in results:
        if r["ok"] and r["text"]:
            host = urlparse(r["url"]).netloc
            chunks.append("【网页 %s 的内容】\n%s" % (host, r["text"]))
        elif not r["ok"]:
            chunks.append("【网页 %s 抓取失败：%s】" % (r["url"], r["error"]))
    return "\n\n".join(chunks)


SEARCH_URL = "https://cn.bing.com/search"


def search(query, max_results=8, timeout=20):
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, params={"q": query}, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return []
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', r.text, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = m.group(1)
        if not url.startswith("http"):
            continue
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = re.sub(r"<[^>]+>", "", sn.group(1)).strip() if sn else ""
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


def format_search_for_prompt(results, query):
    if not results:
        return "【搜索「%s」没有返回结果】" % query
    lines = ["【搜索「%s」的结果】" % query]
    for i, r in enumerate(results, 1):
        lines.append("%d. %s\n   %s\n   %s" % (i, r["title"], r["url"], r["snippet"][:220]))
    return "\n".join(lines)
