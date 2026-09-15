import hashlib
import os
import shutil
import subprocess
import tempfile

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache", "tex")

BG = "1E1F26"
FG = "F5F5F7"

TEMPLATE = r"""\documentclass[border=2pt,varwidth]{standalone}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{xcolor}
\begin{document}
\pagecolor[HTML]{%(bg)s}
\color[HTML]{%(fg)s}
$\displaystyle %(body)s$
\end{document}
"""


def _candidates():
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(local, "Programs", "MiKTeX", "miktex", "bin", "x64", "pdflatex.exe"),
        r"C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe",
        r"C:\Program Files\MiKTeX\miktex\bin\pdflatex.exe",
        os.path.join(home, "AppData", "Local", "Programs", "MiKTeX", "miktex", "bin", "x64", "pdflatex.exe"),
    ]


_PDFLATEX = None


def find_pdflatex():
    global _PDFLATEX
    if _PDFLATEX:
        return _PDFLATEX
    for c in _candidates():
        if os.path.isfile(c):
            _PDFLATEX = c
            return c
    found = shutil.which("pdflatex")
    if found:
        _PDFLATEX = found
    return _PDFLATEX


def available():
    return find_pdflatex() is not None


def _cache_key(body, scale, bg, fg):
    h = hashlib.sha1()
    h.update(("%s|%s|%s|%s" % (body, scale, bg, fg)).encode("utf-8"))
    return h.hexdigest()


def render(body, scale=4.0, bg=BG, fg=FG, timeout=60):
    """Render a LaTeX math snippet to a PNG file path. Returns None on failure."""
    exe = find_pdflatex()
    if not exe:
        return None
    body = (body or "").strip()
    if not body:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(body, scale, bg, fg)
    png_path = os.path.join(CACHE_DIR, key + ".png")
    if os.path.isfile(png_path):
        return png_path

    tex = TEMPLATE % {"body": body, "bg": bg, "fg": fg}
    tmp = tempfile.mkdtemp(prefix="tex_")
    try:
        tex_path = os.path.join(tmp, "f.tex")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex)
        proc = subprocess.run(
            [exe, "-interaction=nonstopmode", "-halt-on-error", "-output-directory", tmp, tex_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=tmp,
        )
        pdf_path = os.path.join(tmp, "f.pdf")
        if not os.path.isfile(pdf_path):
            return None
        doc = pymupdf.open(pdf_path)
        if doc.page_count < 1:
            return None
        page = doc[0]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        pix.save(png_path)
        doc.close()
        return png_path
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
