import hashlib
import os
import threading
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


def _host(u):
    h = (urlparse(u).netloc or "").lower()
    if ":" in h:
        h = h.split(":")[0]
    if h.startswith("www."):
        h = h[4:]
    return h


def _same_site(u1, u2):
    h1, h2 = _host(u1), _host(u2)
    return bool(h1) and h1 == h2


def _soft_redirect_note(requested, final):
    """检测「请求的路径不存在 → 服务器重定向到首页」这类软 404。

    这类站点对任意不存在的路径都回首页（HTTP 200），若不提醒，
    模型会误以为抓到了目标栏目页，从而基于错误内容作答。
    """
    if not final or final == requested:
        return ""
    rp, fp = urlparse(requested), urlparse(final)
    req_path = (rp.path or "/").strip()
    fin_path = (fp.path or "/").strip()
    if not _same_site(requested, final):
        return ("\n⚠️ 注意：该网址被重定向到了**其它站点** %s，"
                "你抓到的不是请求的那个站点的内容。" % final)
    roots = ("", "/", "/index.htm", "/index.html", "/index.php", "/default.html")
    if req_path not in roots and fin_path in roots:
        return ("\n⚠️ 注意：请求的路径「%s」**不存在**，服务器把页面重定向到了网站首页"
                "（实际打开的是 %s）。你抓到的**不是**目标栏目页，"
                "不要把它当成目标页面的内容，请改用本页链接列表里的真实地址。"
                % (rp.path, final))
    if req_path != fin_path:
        return "\n⚠️ 注意：该网址被重定向到 %s（实际打开的是这个地址）。" % final
    return ""


A_RE = re.compile(r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                  re.DOTALL | re.IGNORECASE)


def extract_nav_links(html, base_url, limit=45):
    """抽出页面里的同站链接（文字 → 绝对地址）。

    很多站点的栏目真实地址与猜测不符（如 piyao.org.cn 的辟谣访谈是 /ft.htm
    而不是 /pyft/）。给出本页链接，模型才能导航到正确的栏目页/文章页。
    """
    from urllib.parse import urljoin

    out, seen = [], set()
    for m in A_RE.finditer(html or ""):
        href, inner = m.group(1).strip(), m.group(2)
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        text = re.sub(r"\s+", " ", ANY_TAG.sub("", inner)).strip()
        if not text or len(text) > 60:
            continue
        try:
            full = urljoin(base_url, href)
        except (ValueError, TypeError):
            continue
        if not full.startswith("http") or not _same_site(base_url, full):
            continue
        key = (text, full)
        if key in seen:
            continue
        seen.add(key)
        out.append((text, full))
        if len(out) >= limit:
            break
    if not out:
        return ""
    lines = ["\n【本页可点的同站链接（想找栏目页/文章页时用这里的真实地址）】"]
    for text, full in out:
        lines.append("- %s → %s" % (text, full))
    return "\n".join(lines)


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


def extract_pdf(content):
    """用 PyMuPDF 提取 PDF 文本，带页码标记（题目常问「第N页」）。"""
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:  # noqa: BLE001
        return None
    if doc.page_count == 0:
        return None
    parts = ["【PDF 共 %d 页】" % doc.page_count]
    for i in range(doc.page_count):
        try:
            t = doc[i].get_text()
        except Exception:  # noqa: BLE001
            t = ""
        t = t.strip()
        if t:
            parts.append("\n【第 %d 页】\n%s" % (i + 1, t))
    doc.close()
    return "\n".join(parts)


GITHUB_API = "https://api.github.com"
GH_TREE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/(?:tree|blob)/([^/]+)/?(.*)$", re.I
)
GH_REPO_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/?$", re.I)


def github_dir(owner, repo, path="", ref=None, timeout=25):
    """用 GitHub API 精确列目录（比抓网页准，题目常问「某文件夹有几个文件」）。"""
    api = "%s/repos/%s/%s/contents/%s" % (GITHUB_API, owner, repo, path.strip("/"))
    if ref:
        api += "?ref=" + ref
    try:
        req = requests.get(
            api,
            headers={"User-Agent": "screen-answer", "Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
        if req.status_code == 404:
            return {"ok": False, "error": "路径不存在（仓库/分支/目录名可能有误）"}
        req.raise_for_status()
        data = req.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(data, list):
        return {"ok": False, "error": "该路径不是目录"}
    lines = ["【GitHub 目录】%s/%s/%s  共 %d 项" % (owner, repo, path.strip("/") or "(根)", len(data))]
    for it in data:
        lines.append("  - %s (%s)" % (it.get("name"), it.get("type")))
    return {"ok": True, "count": len(data), "text": "\n".join(lines), "items": data}


def github_from_url(url):
    m = GH_TREE_RE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        return github_dir(owner, repo, path, ref)
    m = GH_REPO_RE.match(url)
    if m:
        return github_dir(m.group(1), m.group(2), "")
    return None


def _decode(resp):
    """按响应头/实际内容正确解码，避免中文乱码。"""
    enc = (resp.encoding or "").lower()
    if enc in ("", "iso-8859-1", "ascii"):
        enc = resp.apparent_encoding or "utf-8"
    try:
        return resp.content.decode(enc, errors="replace")
    except (LookupError, TypeError):
        return resp.text


def _get(url, timeout=25, total=25):
    """带「硬性总超时」的 GET。

    requests 的 timeout 只限制单次连接/读取；服务端慢慢滴数据时总耗时可无限延长。
    这里用线程 + join(total) 保证总耗时不超过 total 秒。
    """
    box = {}

    def work():
        try:
            box["r"] = requests.get(url, headers=HEADERS, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(total)
    if th.is_alive():
        raise requests.Timeout("总耗时超过 %ds" % total)
    if "e" in box:
        raise box["e"]
    return box["r"]


def fetch(url, query="", max_chars=DEFAULT_PAGE_CHARS, timeout=25,
          render_fallback=True, render_timeout=60, total_timeout=25):
    gh = github_from_url(url)
    if gh is not None:
        if gh.get("ok"):
            return {"url": url, "ok": True, "error": None, "text": gh["text"],
                    "chars": len(gh["text"]), "rendered": False}
        return {"url": url, "ok": False, "error": gh.get("error", "GitHub 查询失败"),
                "text": "", "chars": 0}
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
        r = _get(url, timeout=timeout, total=total_timeout)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
        if is_pdf:
            pdf_text = extract_pdf(r.content)
            if pdf_text:
                text = select_relevant(pdf_text, query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": False, "pdf": True}
            return {"url": url, "ok": False, "error": "PDF 解析失败", "text": "", "chars": 0}
        html = _decode(r)
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # 404/410 表示路径本身不存在：渲染也没用，而且很多站点会返回一个
        # 长得像首页的 404 页面，渲染后会被误当成目标页内容（软 404 陷阱）。
        if status in (404, 410):
            return {"url": url, "ok": False, "text": "", "chars": 0,
                    "error": "HTTP %d 页面不存在——该路径无效，"
                             "不要把它当作目标栏目页；请改用首页链接列表里的真实地址，"
                             "或换一个网址再试。" % status}
        if render_fallback:
            rendered, err = render_with_browser(url, timeout=render_timeout)
            if rendered:
                text = select_relevant(_strip_tags(rendered), query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": True}
        return {"url": url, "ok": False, "error": str(exc), "text": "", "chars": 0}

    notes = _soft_redirect_note(url, r.url)
    links = extract_nav_links(html, r.url or url)
    suffix = notes + ("\n" + links if links else "")

    if "html" in ctype or "xml" in ctype or not ctype:
        main = extract_main(html, url)
        naive = _strip_tags(html)
        if len(main) < 400 and len(naive) > 1500:
            main = naive
        if render_fallback and _looks_js_required(main, html):
            rendered, _err = render_with_browser(url, timeout=render_timeout)
            if rendered:
                rt = _strip_tags(rendered)
                if len(rt) > len(main):
                    main = rt
                    rlinks = extract_nav_links(rendered, r.url or url)
                    rsuffix = notes + ("\n" + rlinks if rlinks else "")
                    text = select_relevant(main, query, max_chars) + rsuffix
                    return {"url": url, "ok": True, "error": None, "text": text,
                            "chars": len(text), "rendered": True}
        text = main
    else:
        text = r.text

    text = select_relevant(text, query, max_chars) + suffix
    return {"url": url, "ok": True, "error": None, "text": text, "chars": len(text),
            "rendered": False}


def _looks_js_required(text, html):
    """判断是否真的需要渲染。

    注意：页面正文短 ≠ 需要渲染。只有「HTML 很大但正文极少」或出现
    「请启用 JavaScript」这类提示时，才值得花 20-40 秒去渲染。
    """
    head = (text or "")[:600].lower()
    if any(k in head for k in ("enable javascript", "请启用", "正在加载",
                               "requires javascript", "loading")):
        return True
    if len(html) > 40000 and len(text) < 800:
        return True
    # 正文几乎为空、但 HTML 有一定体量 → 很可能是 SPA 空壳
    if len(text) < 120 and len(html) > 800:
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
        r = _get(url, timeout=timeout, total=total_timeout)
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


def search_and_read(query, count=3, max_chars=8000, timeout=25):
    """搜索并自动抓取前 N 条结果的正文（并行抓取，避免串行过慢）。

    用于「搜到了结果但只抓了一个页面、恰好是空页」的情况。
    """
    import concurrent.futures as _cf

    hits = search(query, max_results=max(1, min(count, 5)))
    if not hits:
        return "【搜索「%s」没有返回结果】" % query

    results = [None] * len(hits)

    def work(idx_hit):
        idx, h = idx_hit
        return idx, fetch(
            h["url"], query=query, max_chars=max_chars, timeout=timeout,
            render_timeout=35,
        )

    with _cf.ThreadPoolExecutor(max_workers=len(hits)) as ex:
        for idx, r in ex.map(work, list(enumerate(hits))):
            results[idx] = r

    blocks = ["【搜索「%s」并抓取前 %d 条结果】" % (query, len(hits))]
    for i, (h, r) in enumerate(zip(hits, results), 1):
        blocks.append(
            "\n=== 结果 %d ===\n标题：%s\n网址：%s" % (i, h["title"], h["url"])
        )
        if r and r["ok"] and r["text"]:
            blocks.append("正文（%d 字%s）：\n%s" % (
                r["chars"], "，已渲染" if r.get("rendered") else "", r["text"]))
        else:
            blocks.append("抓取失败：%s" % ((r or {}).get("error") or "内容为空"))
    return "\n".join(blocks)
