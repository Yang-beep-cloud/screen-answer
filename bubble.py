import re
import tkinter as tk

import latex_render

BG = "#1e1f26"
FG = "#f5f5f7"
ACCENT = "#3b6fe0"
MUTED = "#9aa0b5"
BORDER = "#3a3d4d"
ERR = "#f7768e"

DEFAULT_LINES = 16
DEFAULT_COLS = 58
MIN_LINES = 8
MAX_LINES = 34

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
        self.bar = bar
        self.grip = tk.Label(
            bar, text="⣿", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 10), cursor="fleur"
        )
        self.grip.pack(side="left", padx=(0, 6))
        self.title = tk.Label(
            bar, text="屏幕答题（按住此行可拖动）", bg=BG, fg=ACCENT,
            font=("Microsoft YaHei UI", 10, "bold"), cursor="fleur",
        )
        self.title.pack(side="left")
        self.close = tk.Label(
            bar, text="✕", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 11), cursor="hand2"
        )
        self.close.pack(side="right")
        self.close.bind("<Button-1>", lambda e: self.hide())
        self.copy = tk.Label(
            bar, text="复制", bg=BG, fg=ACCENT, font=("Microsoft YaHei UI", 9), cursor="hand2"
        )
        self.copy.pack(side="right", padx=(0, 10))
        self.copy.bind("<Button-1>", self._on_copy_click)

        self.status = tk.Label(
            self.inner, text="准备就绪", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8), anchor="w"
        )
        self.status.pack(fill="x", padx=12, pady=(0, 2))

        wrap = tk.Frame(self.inner, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.wrap = wrap

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
            height=DEFAULT_LINES,
            width=DEFAULT_COLS,
            cursor="arrow",
            yscrollcommand=self.scroll.set,
            padx=2,
            pady=2,
        )
        self.body.pack(side="left", fill="both", expand=True)
        self.scroll.configure(command=self.body.yview)
        self.body.configure(state="disabled")

        for seq, fn in (("<Button-1>", self._start_move), ("<B1-Motion>", self._on_move)):
            for w in (self.frame, self.inner, self.bar, self.grip, self.title, self.status, self.wrap):
                w.bind(seq, fn)

        self.body.configure(state="normal")
        self.body.bind("<Control-c>", self._on_ctrl_c)
        self.body.bind("<Control-C>", self._on_ctrl_c)
        self.body.bind("<Button-3>", self._on_right_click)
        self.body.bind("<Control-a>", self._on_select_all)
        self.body.bind("<Control-A>", self._on_select_all)
        self.body.bind("<Key>", lambda e: "break")
        self._make_readonly()

        self.menu = tk.Menu(
            self.root, tearoff=0, bg="#2a2c36", fg=FG,
            activebackground=ACCENT, activeforeground="#ffffff",
            borderwidth=0, font=("Microsoft YaHei UI", 9),
        )
        self.menu.add_command(label="复制选中", command=self.copy_selection)
        self.menu.add_command(label="复制全部", command=self.copy_all)
        self.menu.add_separator()
        self.menu.add_command(label="全选", command=self.select_all)

        self._drag = (0, 0)
        self._visible = False
        self._user_moved = False
        self._full_text = ""
        self._lines = DEFAULT_LINES
        self._images = []
        self._ok = True

    @property
    def visible(self):
        return self._visible

    def _make_readonly(self):
        """保持 state=normal 以便选中与复制，但拦截所有按键防止编辑。"""
        self.body.configure(state="normal")

    def _on_ctrl_c(self, event=None):
        self.copy_selection()
        return "break"

    def _on_select_all(self, event=None):
        self.select_all()
        return "break"

    def _on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def _on_copy_click(self, event=None):
        self.copy_all()
        return "break"

    def select_all(self):
        self.body.tag_add("sel", "1.0", "end-1c")
        self.body.mark_set("insert", "1.0")
        return "break"

    def copy_selection(self):
        try:
            first = self.body.index("sel.first")
            last = self.body.index("sel.last")
            text = self.body.get(first, last)
        except tk.TclError:
            return self.copy_all()
        # 若选中的是全文，则用原始文本，避免公式图片丢失
        try:
            if self.body.compare(first, "<=", "1.0") and self.body.compare(last, ">=", "end-1c"):
                return self.copy_all()
        except tk.TclError:
            pass
        if not text:
            return self.copy_all()
        return self._to_clipboard(text)

    def copy_all(self):
        text = self._full_text or self.body.get("1.0", "end-1c")
        return self._to_clipboard(text)

    def _to_clipboard(self, text):
        if not text:
            self.set_status("没有可复制的内容", MUTED)
            return "break"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            n = len(text)
            self.set_status("已复制 %d 字" % n, ACCENT)
            self.root.after(1600, lambda: self.set_status(
                "完成" if self._ok else "出错", ACCENT if self._ok else ERR))
        except tk.TclError:
            self.set_status("复制失败", ERR)
        return "break"

    def _start_move(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_move(self, event):
        self._user_moved = True
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        keep = 48
        x = max(keep - w, min(x, sw - keep))
        y = max(0, min(y, sh - keep))
        self.root.geometry("+%d+%d" % (x, y))

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
        # 保持 normal 以便选中/复制，按键已被 <Key> 绑定拦截，无法编辑
        self.body.configure(state="normal")

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

    def _max_lines(self):
        try:
            sh = self.root.winfo_screenheight()
        except tk.TclError:
            return MAX_LINES
        line_h = max(12, int(self.body.winfo_fpixels("1i") / 6.5))
        fit = max(MIN_LINES, (sh - 200) // line_h)
        return max(MIN_LINES, min(MAX_LINES, fit))

    def _autosize(self):
        try:
            n = self.body.count("1.0", "end", "displaylines")[0]
        except (tk.TclError, TypeError, IndexError):
            n = DEFAULT_LINES
        n = max(MIN_LINES, min(int(n) + 1, self._max_lines()))
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
        self._ok = True
        self.show()
        self.set_status("正在思考…", ACCENT)
        self._clear()
        self._done_edit()
        self._lines = DEFAULT_LINES
        self.body.configure(height=DEFAULT_LINES)
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
        self._ok = ok
        self.set_status("完成" if ok else "出错", ACCENT if ok else ERR)
        self._autosize()

    def error(self, msg):
        self._ok = False
        self.set_status("出错", ERR)
        self.set_answer("⚠ " + msg)
