import hashlib
import os
import re
import shutil
import subprocess
import tempfile
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


def _decode(resp):
    """按响应头/实际内容正确解码，避免中文乱码。"""
    enc = (resp.encoding or "").lower()
    if enc in ("", "iso-8859-1", "ascii"):
        enc = resp.apparent_encoding or "utf-8"
    try:
        return resp.content.decode(enc, errors="replace")
    except (LookupError, TypeError):
        return resp.text


def fetch(url, query="", max_chars=DEFAULT_PAGE_CHARS, timeout=25, render_fallback=True):
    if is_pubmed_url(url):
        term = pubmed_term_from_url(url)
        if term:
            res = pubmed_search(term)
            if res["ok"] and res["count"] is not None:
                head = (
                    "【PubMed 精确命中数】%s 篇\n检索式：%s\n翻译后：%s\n\n"
                    % (res["count"], term, res.get("translation", ""))
                )
                text = select_relevant(head + _pubmed_page_text(url, query, max_chars, timeout),
                                      query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": False}
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        html = _decode(r)
        ctype = (r.headers.get("content-type") or "").lower()
    except requests.RequestException as exc:
        if render_fallback:
            rendered, err = render_with_browser(url, timeout=60)
            if rendered:
                text = select_relevant(_strip_tags(rendered), query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": True}
        return {"url": url, "ok": False, "error": str(exc), "text": "", "chars": 0}

    if "html" in ctype or "xml" in ctype or not ctype:
        main = extract_main(html, url)
        naive = _strip_tags(html)
        if len(main) < 400 and len(naive) > 1500:
            main = naive
        if render_fallback and _looks_js_required(main, html):
            rendered, _err = render_with_browser(url, timeout=60)
            if rendered:
                rt = _strip_tags(rendered)
                if len(rt) > len(main):
                    main = rt
                    text = select_relevant(main, query, max_chars)
                    return {"url": url, "ok": True, "error": None, "text": text,
                            "chars": len(text), "rendered": True}
        text = main
    else:
        text = r.text

    text = select_relevant(text, query, max_chars)
    return {"url": url, "ok": True, "error": None, "text": text, "chars": len(text),
            "rendered": False}


def _looks_js_required(text, html):
    if len(text) < 300:
        return True
    head = text[:600].lower()
    if any(k in head for k in ("enable javascript", "请启用", "正在加载", "loading", "requires javascript")):
        return True
    if len(html) > 40000 and len(text) < 800:
        return True
    return False


BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

_BROWSER = None


def find_browser():
    global _BROWSER
    if _BROWSER:
        return _BROWSER
    for c in BROWSER_CANDIDATES:
        if os.path.isfile(c):
            _BROWSER = c
            return c
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            _BROWSER = found
            return found
    return None


def render_with_browser(url, timeout=35, budget_ms=9000):
    """用 Edge/Chrome 无头模式渲染页面，返回渲染后的 HTML（解决 SPA 抓不到的问题）。"""
    exe = find_browser()
    if not exe or not url.startswith("http"):
        return None, "未找到 Edge/Chrome"
    profile = os.path.join(tempfile.gettempdir(), "sa_render_profile")
    out_fd, out_path = tempfile.mkstemp(prefix="dom_", suffix=".html")
    os.close(out_fd)
    args = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--user-data-dir=" + profile,
        "--virtual-time-budget=%d" % budget_ms,
        "--dump-dom",
        url,
    ]
    creation = 0x08000000 if os.name == "nt" else 0
    try:
        with open(out_path, "wb") as f:
            subprocess.run(
                args, stdout=f, stderr=subprocess.DEVNULL, timeout=timeout,
                creationflags=creation,
            )
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            dom = f.read()
    except subprocess.TimeoutExpired:
        return None, "渲染超时"
    except OSError as exc:
        return None, "渲染失败：%s" % exc
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    if not dom or "<html" not in dom.lower():
        return None, "渲染结果为空"
    return dom, None


PUBMED_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def pubmed_search(term, retmax=0, timeout=25):
    """用 NCBI E-utilities 精确获取 PubMed 命中数（比抓网页准确）。"""
    params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": retmax}
    try:
        r = requests.get(PUBMED_API, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("esearchresult", {})
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": str(exc), "count": None, "pmids": []}
    return {
        "ok": True,
        "error": data.get("error"),
        "count": data.get("count"),
        "pmids": data.get("idlist") or [],
        "translation": data.get("querytranslation", ""),
    }


def is_pubmed_url(url):
    return "pubmed.ncbi.nlm.nih.gov" in (url or "")


def _pubmed_page_text(url, query, max_chars, timeout):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        html = _decode(r)
        main = extract_main(html, url)
        naive = _strip_tags(html)
        return naive if len(main) < 400 and len(naive) > 1500 else main
    except requests.RequestException:
        return ""


def pubmed_term_from_url(url):
    try:
        qs = urlparse(url).query
    except ValueError:
        return None
    for part in qs.split("&"):
        if part.startswith("term="):
            from urllib.parse import unquote_plus

            return unquote_plus(part[5:])
    return None


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
