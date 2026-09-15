import tkinter as tk

ACCENT = "#2f6bff"
ACCENT_HOVER = "#4d84ff"
ACCENT_BUSY = "#6b7280"
WHITE = "#ffffff"
SUB = "#dbe4ff"
RING = "#ffffff"
OUTLINE = "#0b1f5c"


class FloatButton:
    def __init__(self, master, on_click, hotkey_text="Ctrl+Alt+Q"):
        self.on_click = on_click
        self._busy = False
        self._drag = (0, 0)
        self._moved = False
        self._visible = True

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

        self.outline = tk.Frame(self.top, bg=OUTLINE)
        self.outline.pack()
        self.ring = tk.Frame(self.outline, bg=RING)
        self.ring.pack(padx=1, pady=1)
        self.box = tk.Frame(self.ring, bg=ACCENT)
        self.box.pack(padx=2, pady=2)

        self.label = tk.Label(
            self.box,
            text="答题",
            bg=ACCENT,
            fg=WHITE,
            font=("Microsoft YaHei UI", 20, "bold"),
            padx=34,
            pady=10,
            cursor="hand2",
        )
        self.label.pack(fill="x")
        self.sub = tk.Label(
            self.box,
            text=hotkey_text,
            bg=ACCENT,
            fg=SUB,
            font=("Microsoft YaHei UI", 9),
            pady=0,
            cursor="hand2",
        )
        self.sub.pack(fill="x", pady=(0, 10))

        for w in (self.outline, self.ring, self.box, self.label, self.sub):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._move)
            w.bind("<ButtonRelease-1>", self._release)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

        self.top.update_idletasks()
        self.place_default()
        self.top.deiconify()
        self.top.lift()

    @property
    def visible(self):
        return self._visible

    def _bg(self, color):
        for w in (self.box, self.label, self.sub):
            w.configure(bg=color)

    def _press(self, event):
        self._drag = (event.x_root - self.top.winfo_x(), event.y_root - self.top.winfo_y())
        self._moved = False

    def _move(self, event):
        self._moved = True
        self.top.geometry("+%d+%d" % (event.x_root - self._drag[0], event.y_root - self._drag[1]))

    def _release(self, event):
        if not self._moved and not self._busy:
            self.on_click()

    def _enter(self, event):
        if not self._busy:
            self._bg(ACCENT_HOVER)

    def _leave(self, event):
        if not self._busy:
            self._bg(ACCENT)

    def place_default(self):
        self.top.update_idletasks()
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()
        w = self.top.winfo_width()
        h = self.top.winfo_height()
        self.top.geometry("+%d+%d" % (sw - w - 28, sh - h - 70))

    def set_busy(self, busy):
        self._busy = busy
        if busy:
            self.label.configure(text="思考中…")
            self._bg(ACCENT_BUSY)
        else:
            self.label.configure(text="答题")
            self._bg(ACCENT)

    def show(self):
        if not self._visible:
            self._visible = True
            self.top.deiconify()
            self.top.lift()

    def hide(self):
        self._visible = False
        self.top.withdraw()
