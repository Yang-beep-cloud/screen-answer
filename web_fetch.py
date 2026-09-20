import hashlib
import os
import threading
import time
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
    """抽取正文。

    trafilatura 并不总是可靠：实测同一份 HTML 连续调用会给出
    13526 / 45581 / 0 三种结果，而且经常只抽到参考文献、把页面开头的
    标题与作者行整段丢掉。所以这里加两道保险：
    (1) 输出为空或过短 → 回退到 naive；
    (2) 输出明显比 naive 短（不足其一半）→ 认为它漏了大块内容，改用 naive。
    """
    naive = _strip_tags(html)
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
            out = out.strip() if out else ""
            if len(out) > 80:
                if len(naive) > 3000 and len(out) < len(naive) * 0.5:
                    return naive
                return out
        except Exception:  # noqa: BLE001
            pass
    return naive


def _page_opening(html, main, naive):
    """若正文提取把页面开头的标题/作者区当样板丢掉了，把这部分补回来。

    实测 NCBI Bookshelf 的章节页：trafilatura 只抽出参考文献（13526 字），
    把章节标题和「Angela Y Chang, Susan Horton, and Dean T Jamison」这行
    作者整段丢掉，而题目问的恰好就是作者。naive 文本里这两样都在。
    """
    if not main or not naive:
        return ""
    # 优先用 <title>：站点常把 <h1> 当站名（NCBI 的 <h1> 就是「Bookshelf」），
    # 而 <title> 才是文档标题。
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", ANY_TAG.sub("", m.group(1))).strip()
        title = re.split(r"\s+[-|–]\s+", title)[0].strip()  # 去掉「- 站点名」后缀
    if len(title) < 8:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", ANY_TAG.sub("", m.group(1))).strip()
    if len(title) < 8:
        return ""
    head = title[:36]
    pos = naive.find(head)
    if pos < 0:
        return ""
    # 窗口要够大：NCBI 章节页 title 在第 0 字，作者行在第 1753 字
    # （中间还夹着站点导航），窗口太小就会把作者行漏掉。
    seg = naive[max(0, pos - 60): pos + 2600]
    # 用分块包含率判断「这段开头是否已经在正文里」。
    # 不能只看标题：实测正文里可能有标题却没有作者行。
    chunks = [seg[i:i + 60] for i in range(0, len(seg), 200)]
    chunks = [c for c in chunks if len(c) == 60]
    if chunks:
        present = sum(1 for c in chunks if c in main)
        if present >= len(chunks) * 0.75:
            return ""
    return seg


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


DOCX_MAGIC = b"PK\x03\x04"
W_T_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
W_BR_RE = re.compile(r"<w:br\s*/?>|</w:p>", re.I)


def extract_docx(content):
    """从 .docx 提取正文（很多平台的「教学设计/课件」就是 docx 附件）。

    不依赖 python-docx：docx 本质是 zip，正文在 word/document.xml。
    """
    import io as _io
    import zipfile

    try:
        z = zipfile.ZipFile(_io.BytesIO(content))
    except Exception:  # noqa: BLE001
        return None
    try:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    xml = W_BR_RE.sub("\n", xml)
    parts = []
    for m in W_T_RE.finditer(xml):
        t = m.group(1)
        if "<" in t:
            t = ANY_TAG.sub("", t)
        parts.append(t)
    text = "".join(parts)
    for k, v in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(k, v)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())
    return text or None


# 注意：JSON 里的地址常把斜杠转义成 \/（如 "upfile":"https:\/\/oss...\/x.docx"），
# 所以模式里要允许反斜杠，匹配后再把 \/ 还原成 /。
OFFICE_EXT_RE = re.compile(
    r"""["'\s(](https?:\\?/\\?/[^"'\s<>()]+?\.(?:docx?|xlsx?|pptx?|pdf))(?=["'\s<>)]|$)""",
    re.I)


def extract_file_links(html, base_url="", limit=12):
    """抽出页面（含 <script> 内嵌 JSON）里指向文档附件的 URL。

    很多平台把「教学设计/课件」做成 docx 附件，地址写在 script 的 JSON 里
    （如课程思政平台的 sss 数组 upfile 字段），_strip_tags 会把 script 整个
    删掉，模型就无从得知。这里从原始 HTML 里把它们捞出来。
    """
    from urllib.parse import urljoin

    out, seen = [], set()
    for m in OFFICE_EXT_RE.finditer(html or ""):
        u = m.group(1).replace("\\/", "/")
        try:
            u = urljoin(base_url or u, u)
        except (ValueError, TypeError):
            pass
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    if not out:
        return ""
    return "\n【本页内嵌的文档附件地址（可直接 fetch_web 打开）】\n" + \
        "\n".join("- " + u for u in out)


def _is_docx(ctype, url, content):
    if "wordprocessingml" in ctype:
        return True
    if url.lower().split("?")[0].endswith(".docx"):
        return True
    return content[:4] == DOCX_MAGIC and b"word/" in content[:4000]


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
                text = select_relevant(
                    head + _pubmed_page_text(url, query, max_chars, timeout, total_timeout),
                    query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": False}
    try:
        r = _get(url, timeout=timeout, total=total_timeout)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if _is_docx(ctype, url, r.content):
            doc_text = extract_docx(r.content)
            if doc_text:
                text = select_relevant(doc_text, query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": False, "docx": True}
            return {"url": url, "ok": False, "error": "DOCX 解析失败", "text": "", "chars": 0}
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
                raw = _strip_tags(rendered)
                if _looks_like_challenge(raw):
                    return {"url": url, "ok": False, "text": "", "chars": 0,
                            "error": CHALLENGE_ERROR}
                text = select_relevant(raw, query, max_chars)
                return {"url": url, "ok": True, "error": None, "text": text,
                        "chars": len(text), "rendered": True}
        if status is not None:
            return {"url": url, "ok": False, "text": "", "chars": 0,
                    "error": "HTTP %d —— 该页无法抓取（可能需要登录或已被限制）。"
                             "不要把它当作已抓到的正文，请换来源或输出检索指引。" % status}
        return {"url": url, "ok": False, "error": str(exc), "text": "", "chars": 0}

    notes = _soft_redirect_note(url, r.url)
    links = extract_nav_links(html, r.url or url)
    files = extract_file_links(html, r.url or url)
    suffix = notes + ("\n" + links if links else "") + ("\n" + files if files else "")

    opening = ""
    if "html" in ctype or "xml" in ctype or not ctype:
        main = extract_main(html, url)
        naive = _strip_tags(html)
        if len(main) < 400 and len(naive) > 1500:
            main = naive
        _op = _page_opening(html, main, naive)
        opening = ("【页面开头（标题/作者区；正文提取常把这段当样板丢掉）】\n" + _op + "\n\n") if _op else ""
        if render_fallback and _looks_js_required(main, html):
            rendered, _err = render_with_browser(url, timeout=render_timeout)
            if rendered:
                rt = _strip_tags(rendered)
                if len(rt) > len(main):
                    main = rt
                    rlinks = extract_nav_links(rendered, r.url or url)
                    rfiles = extract_file_links(rendered, r.url or url)
                    rsuffix = notes + ("\n" + rlinks if rlinks else "") + ("\n" + rfiles if rfiles else "")
                    if _looks_like_challenge(main):
                        return {"url": url, "ok": False, "text": "", "chars": 0,
                                "error": CHALLENGE_ERROR}
                    ropening = _page_opening(rendered, main, rt)
                    text = ropening + select_relevant(main, query, max_chars) + rsuffix
                    return {"url": url, "ok": True, "error": None, "text": text,
                            "chars": len(text), "rendered": True}
        text = main
    else:
        text = r.text
        opening = ""

    if _looks_like_challenge(text):
        return {"url": url, "ok": False, "text": "", "chars": 0,
                "error": CHALLENGE_ERROR}
    text = opening + select_relevant(text, query, max_chars) + suffix
    return {"url": url, "ok": True, "error": None, "text": text, "chars": len(text),
            "rendered": False}


# 强标记：出现任意一个就基本可断定是验证页
STRONG_CHALLENGE = (
    "just a moment",
    "cf-chl", "cf_chl_opt", "cf-challenge",
    "checking your browser",
    "enable javascript and cookies to continue",
    "正在进行安全验证",
    "安全服务防护恶意自动程序",
    "验证您不是自动程序",
    "incapsula incident",
    "request unsuccessful. incapsula",
    "由 cloudflare 提供",
    "attention required! | cloudflare",
    # 「浏览器版本过低/请升级」这类拦截页：站点只回了导航外壳与升级提示，
    # 没有实际正文（实测万方详情页会这样），必须识别出来，
    # 否则模型会拿导航文字当正文，甚至从 URL 里的 periodical 猜出 [J]。
    "浏览器版本过低",
    "建议您下载",
    "请升级浏览器",
    "升级浏览器至",
    "upgrade your browser",
    "unsupported browser",
    "browser is not supported",
    "your browser is out of date",
)

# 弱标记：单独出现不足以判定（正常页面也会提到 captcha、请稍候、
# access denied 等词，例如 CNKI 帮助页就会写「滑块验证码 captchaType=...」，
# 误判会让本来能抓的页面被拒掉）。需要命中 2 个以上才算验证页。
WEAK_CHALLENGE = (
    "cloudflare", "captcha", "请稍候", "access denied",
    "403 forbidden", "401 unauthorized", "429 too many requests",
    "incident id", "verify you are human", "ddos protection",
    "imperva", "请开启 javascript", "请求被拦截", "ray id",
)


CHALLENGE_ERROR = (
    "抓到的不是正文，而是站点的拦截页（反爬验证 / 浏览器版本过低提示 / 访问受限）。"
    "程序无法自动通过，页面上也没有题目要的内容。"
    "**不要根据网址或导航文字猜测答案**，请改用其它来源，"
    "或按系统提示词输出检索指引。"
)


def _looks_like_challenge(text):
    """判断抓到的是不是反爬验证页（Cloudflare「Just a moment」等）。

    这类页面 HTTP 200、正文很短，若不识别会被当成正文交给模型，
    让模型以为「已经抓到目标页面」，从而基于验证页内容瞎答。

    但也要避免误报：正常页面也可能出现 captcha、请稍候 等词。
    故强标记命中 1 个即可，弱标记需命中 2 个以上。
    """
    t = (text or "").strip()
    if not t or len(t) > 1000:
        return False
    low = t.lower()
    if any(m in low for m in STRONG_CHALLENGE):
        return True
    return sum(1 for m in WEAK_CHALLENGE if m in low) >= 2


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


def _pubmed_page_text(url, query, max_chars, timeout, total_timeout=25):
    try:
        r = _get(url, timeout=timeout, total=total_timeout)
        r.raise_for_status()
        html = _decode(r)
        main = extract_main(html, url)
        naive = _strip_tags(html)
        return naive if len(main) < 400 and len(naive) > 1500 else main
    except Exception:  # noqa: BLE001 - 辅助抓取失败不应影响主流程
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


def openalex_doi(title, timeout=20):
    """用 OpenAlex 按标题找 DOI。

    OpenAlex 的标题匹配比 Crossref 可靠得多（实测 Crossref 连
    「eDoctor: machine learning and the future of medicine」都搜不到，
    OpenAlex 第一条就命中 10.1111/joim.12822）。
    """
    try:
        r = requests.get(
            "https://api.openalex.org/works",
            params={"search": title, "per-page": 5,
                    "select": "doi,title,publication_year"},
            headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        items = r.json().get("results") or []
    except Exception:  # noqa: BLE001
        return None, ""
    if not items:
        return None, ""
    # 优先选标题与查询高度重合的那条，避免拿错文献
    want = _query_tokens(title)
    best, best_score = None, 0.0
    for it in items:
        t = it.get("title") or ""
        got = _query_tokens(t)
        if not got:
            continue
        score = len(want & got) / float(len(want) or 1)
        if score > best_score:
            best, best_score = it, score
    if best is None or best_score < 0.5:
        return None, ""
    doi = (best.get("doi") or "").replace("https://doi.org/", "")
    return (doi or None), (best.get("title") or "")


def _unpaywall_pdfs(doi, timeout=20):
    """Unpaywall：免费、无需 key，返回该 DOI 的所有 OA 位置。"""
    out = []
    try:
        r = requests.get("https://api.unpaywall.org/v2/%s" % doi,
                         params={"email": "oa-lookup@example.com"},
                         headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return out
        d = r.json()
        for loc in [d.get("best_oa_location")] + (d.get("oa_locations") or []):
            if not loc:
                continue
            u = loc.get("url_for_pdf") or loc.get("url")
            if u and u not in out:
                out.append(u)
    except Exception:  # noqa: BLE001
        pass
    return out


def _openalex_pdfs(doi, timeout=20):
    out = []
    try:
        r = requests.get("https://api.openalex.org/works/doi:%s" % doi,
                         headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return out
        for loc in (r.json().get("locations") or []):
            u = loc.get("pdf_url")
            if u and u not in out:
                out.append(u)
    except Exception:  # noqa: BLE001
        pass
    return out


def _core_pdfs(doi=None, title=None, timeout=25, tries=4):
    """CORE：副本最全，但免费接口限流很紧（约 10 次/窗口，会 429）。"""
    q = 'doi:"%s"' % doi if doi else 'title:"%s"' % title
    out = []
    for attempt in range(tries):
        try:
            r = requests.get("https://api.core.ac.uk/v3/search/works",
                             params={"q": q, "limit": 3},
                             headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5 + attempt * 8)
                continue
            if r.status_code != 200:
                return out
            for w in (r.json().get("results") or []):
                dl = w.get("downloadUrl")
                if dl and dl not in out:
                    out.append(dl)
            return out
        except Exception:  # noqa: BLE001
            time.sleep(4)
    return out


def oa_pdf_candidates(doi=None, title=None):
    """汇总多个 OA 来源的 PDF 候选地址（Unpaywall → OpenAlex → CORE）。

    单靠 CORE 不可靠：免费接口限流很紧，实测连发 6 次就有 1 次 429、
    还有单次 22.9 秒的慢响应，会导致本来能答的题退化成给指引。
    """
    urls = []
    if doi:
        for fn in (_unpaywall_pdfs, _openalex_pdfs):
            for u in fn(doi):
                if u not in urls:
                    urls.append(u)
    for u in _core_pdfs(doi=doi, title=title):
        if u not in urls:
            urls.append(u)
    return urls


def oa_fulltext(doi=None, title=None, query="", max_chars=60000, timeout=30):
    """按 DOI/标题找到论文的开放获取全文并解析成文本。

    query 用于从长文档里挑出与题目相关的段落——整篇论文常超过 max_chars，
    直接取前 N 字会把「图 5 的说明」这类位于后半篇的内容截掉。
    返回 (text, note)；text 为空时 note 说明失败原因。
    """
    if not doi and title:
        doi, found = openalex_doi(title)
        if not doi:
            return "", "OpenAlex 没找到这篇文献的 DOI（标题可能有出入，可先用 search_web 核实标题）。"
    if not doi:
        return "", "需要提供 DOI 或准确的文献标题。"
    urls = oa_pdf_candidates(doi=doi)
    if not urls:
        return "", ("没有找到这篇文献的开放获取副本（DOI: %s）。"
                    "该文献可能需要通过机构订阅获取，请输出检索指引。" % doi)

    def pick(text):
        return select_relevant(text, query, max_chars) if query else text[:max_chars]

    tried = []
    for url in urls[:6]:
        try:
            r = _get(url, timeout=timeout, total=timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            tried.append("%s → %s" % (url[:60], type(exc).__name__))
            continue
        # 有些服务端会在 %PDF 前加空白/BOM，放宽魔数判断
        if b"%PDF" in r.content[:1024]:
            text = extract_pdf(r.content)
            if text:
                return pick(text), "已获取 OA 全文 PDF（DOI: %s）" % doi
            tried.append("%s → PDF 解析失败" % url[:60])
            continue
        res = fetch(url, max_chars=max_chars, timeout=timeout, total_timeout=timeout)
        if res["ok"] and res["text"]:
            return res["text"], "已获取 OA 全文页面（DOI: %s）" % doi
        tried.append("%s → %s" % (url[:60], (res.get("error") or "空")[:40]))
    return "", "找到 %d 个 OA 副本但都抓不到：%s" % (len(urls), "；".join(tried[:3]))


SEARCH_URL = "https://cn.bing.com/search"


_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "http", "https", "www", "com", "org", "net", "html", "htm", "php",
}


def _query_tokens(query):
    return {t.lower() for t in re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", query or "")
            if t.lower() not in _STOP}


def search(query, max_results=8, timeout=20):
    """Bing 搜索。

    注意：Bing 在「零结果」时**不输出 sb_count**，但页面里仍会有 10 个
    b_algo 块，内容是「测网速」之类的无关填充。若直接解析这些块，
    模型会拿到与题目毫无关系的「搜索结果」并据此推理，后果严重。
    因此这里加两道闸：(1) 没有 sb_count 视为无结果；
    (2) 结果与查询词零重叠也视为无结果。
    """
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, params={"q": query}, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return []
    html = r.text
    if "sb_count" not in html:
        return []
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
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
    toks = _query_tokens(query)
    if toks and out:
        blob = " ".join((x["title"] + " " + x["snippet"] + " " + x["url"]) for x in out).lower()
        if not any(t in blob for t in toks):
            return []
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
