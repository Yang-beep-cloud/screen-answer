import re
import tkinter as tk

import latex_render

BG = "#1e1f26"
FG = "#f5f5f7"
ACCENT = "#3b6fe0"
MUTED = "#9aa0b5"
BORDER = "#3a3d4d"
ERR = "#f7768e"

MATH_RE = re.compile(r"(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$[^$\n]+?\$)", re.DOTALL)


def split_math(text):
    segments = []
    pos = 0
    for m in MATH_RE.finditer(text):
        if m.start() > pos:
            segments.append(("text", text[pos:m.start()]))
        raw = m.group(0)
        if raw.startswith("$$") and raw.endswith("$$"):
            segments.append(("display", raw[2:-2]))
        elif raw.startswith("\\["):
            segments.append(("display", raw[2:-2]))
        elif raw.startswith("\\("):
            segments.append(("inline", raw[2:-2]))
        else:
            segments.append(("inline", raw[1:-1]))
        pos = m.end()
    if pos < len(text):
        segments.append(("text", text[pos:]))
    return segments


class Bubble:
    def __init__(self, master):
        self.root = tk.Toplevel(master)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

        self.frame = tk.Frame(self.root, bg=BORDER)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.inner = tk.Frame(self.frame, bg=BG)
        self.inner.pack(fill="both", expand=True)

        bar = tk.Frame(self.inner, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 0))
        self.title = tk.Label(
            bar, text="屏幕答题", bg=BG, fg=ACCENT, font=("Microsoft YaHei UI", 10, "bold")
        )
        self.title.pack(side="left")
        close = tk.Label(
            bar, text="✕", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 11), cursor="hand2"
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())

        self.status = tk.Label(
            self.inner, text="准备就绪", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8), anchor="w"
        )
        self.status.pack(fill="x", padx=12, pady=(0, 2))

        wrap = tk.Frame(self.inner, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.scroll = tk.Scrollbar(
            wrap,
            orient="vertical",
            width=10,
            bg=BORDER,
            troughcolor=BG,
            activebackground=ACCENT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.scroll.pack(side="right", fill="y")

        self.body = tk.Text(
            wrap,
            bg=BG,
            fg=FG,
            font=("Microsoft YaHei UI", 11),
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            height=6,
            width=42,
            cursor="arrow",
            yscrollcommand=self.scroll.set,
            padx=2,
            pady=2,
        )
        self.body.pack(side="left", fill="both", expand=True)
        self.scroll.configure(command=self.body.yview)
        self.body.configure(state="disabled")

        for seq, fn in (("<Button-1>", self._start_move), ("<B1-Motion>", self._on_move)):
            for w in (self.inner, self.title, self.status):
                w.bind(seq, fn)

        self._drag = (0, 0)
        self._visible = False
        self._user_moved = False
        self._full_text = ""
        self._lines = 6
        self._images = []

    @property
    def visible(self):
        return self._visible

    def _start_move(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_move(self, event):
        self._user_moved = True
        self.root.geometry("+%d+%d" % (event.x_root - self._drag[0], event.y_root - self._drag[1]))

    def _place(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = max(0, sw - w - 28)
        y = max(0, sh - h - 210)
        self.root.geometry("+%d+%d" % (x, y))

    def _clear(self):
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self._images = []

    def _done_edit(self):
        self.body.configure(state="disabled")

    def _insert_text(self, text):
        self.body.insert("end", text)

    def _insert_image(self, path, display=False):
        try:
            photo = tk.PhotoImage(file=path)
        except tk.TclError:
            return False
        target = 46 if display else 30
        factor = max(1, int(round(photo.height() / float(target))))
        if factor > 1:
            photo = photo.subsample(factor, factor)
        self._images.append(photo)
        if display:
            self.body.insert("end", "\n")
        self.body.image_create("end", image=photo, padx=4, pady=3)
        if display:
            self.body.insert("end", "\n")
        return True

    def _autosize(self):
        try:
            n = self.body.count("1.0", "end", "displaylines")[0]
        except (tk.TclError, TypeError, IndexError):
            n = 8
        n = max(4, min(int(n) + 1, 24))
        if n != self._lines:
            self._lines = n
            self.body.configure(height=n)
            if not self._user_moved:
                self._place()

    def show(self):
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            self.root.update_idletasks()
            if not self._user_moved:
                self._place()

    def hide(self):
        self._visible = False
        self.root.withdraw()

    def set_status(self, text, color=MUTED):
        self.status.configure(text=text, fg=color)

    def start(self):
        self._full_text = ""
        self.show()
        self.set_status("正在思考…", ACCENT)
        self._clear()
        self._done_edit()
        self._lines = 6
        self.body.configure(height=6)
        if not self._user_moved:
            self._place()
        self.root.attributes("-topmost", True)
        self.root.lift()

    def set_answer(self, text):
        self._full_text = text
        self._clear()
        self._insert_text(text)
        self._done_edit()
        self._autosize()

    def render_rich(self, text=None):
        text = self._full_text if text is None else text
        if not text:
            return
        if not latex_render.available():
            self.set_answer(text)
            return
        self._clear()
        for kind, val in split_math(text):
            if kind == "text":
                self._insert_text(val)
                continue
            path = latex_render.render(val)
            if path and self._insert_image(path, display=(kind == "display")):
                continue
            self._insert_text(val if kind == "display" else "$%s$" % val)
        self._done_edit()
        self._autosize()

    def finish(self, text, ok=True):
        if text:
            self._full_text = text
            self.render_rich(text)
        elif not self._full_text:
            self.set_answer("（无内容）")
        self.set_status("完成" if ok else "出错", ACCENT if ok else ERR)
        self._autosize()

    def error(self, msg):
        self.set_status("出错", ERR)
        self.set_answer("⚠ " + msg)
