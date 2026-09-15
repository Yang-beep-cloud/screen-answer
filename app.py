import json
import os
import queue
import threading
import time
import tkinter as tk

from pynput import keyboard

from bubble import ACCENT, Bubble
from button import FloatButton
from capture import capture_async
from llm_client import LLMError, VisionClient

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "error.log")

PROMPT = "请按格式要求解答截图中的题目。"


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_api_key_from_opencode(provider):
    path = os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "auth.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(provider)
    if isinstance(entry, dict) and entry.get("key"):
        return entry["key"]
    return None


def ensure_api_key(cfg):
    env_name = cfg.get("api_key_env", "USTC_API_KEY")
    if os.environ.get(env_name, "").strip():
        return True
    provider = cfg.get("api_key_provider", "ustc")
    key = load_api_key_from_opencode(provider)
    if key:
        os.environ[env_name] = key
        return True
    return False


class App:
    def __init__(self):
        self.cfg = load_config()
        self.backend_name = self.cfg.get("backend", "api")
        ensure_api_key(self.cfg)
        self.root = tk.Tk()
        self.root.withdraw()
        self.bubble = Bubble(self.root)
        self.button = FloatButton(
            self.root, self._request_trigger, self.cfg.get("hotkey_label", "Ctrl+Alt+Q")
        )
        self.client = None
        self.opencode = None
        self.busy = threading.Event()
        self.stop_event = threading.Event()
        self.hotkey = self.cfg.get("hotkey", "<ctrl>+<alt>+q")
        self._listener = None
        self._tray = None
        self._queue = queue.Queue()
        self._closing = False
        self._capture_delay = self.cfg.get("capture_delay_ms", 200)

    def post(self, fn, *args):
        self._queue.put((fn, args))

    def _pump(self):
        if self._closing:
            return
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                log("dispatch error: %r" % (exc,))
                try:
                    self.bubble.error("内部错误: %s" % exc)
                except Exception:  # noqa: BLE001
                    pass
        self.root.after(30, self._pump)

    def _ensure_client(self):
        if self.backend_name == "opencode":
            if self.opencode is None:
                from opencode_backend import OpenCodeBackend

                self.opencode = OpenCodeBackend(self.cfg)
            return self.opencode
        if self.client is None:
            self.client = VisionClient(self.cfg)
        return self.client

    def _request_trigger(self):
        self.post(self._trigger)

    def _trigger(self):
        if self.busy.is_set():
            return
        self.busy.set()
        self.stop_event.clear()
        self.bubble.hide()
        self.button.hide()
        self.root.after(self._capture_delay, self._begin_capture)

    def _begin_capture(self):
        try:
            self._ensure_client()
        except (LLMError, RuntimeError) as exc:
            log("client error: %s" % exc)
            self._restore_ui()
            self._reset()
            self.bubble.error(str(exc))
            return
        scale = self.cfg.get("capture_scale", 1.0)
        capture_async(scale, self._on_capture, self._on_capture_error)

    def _restore_ui(self):
        self.button.show()

    def _reset(self):
        self.busy.clear()
        self.button.set_busy(False)

    def _on_capture(self, png_bytes):
        self.post(self._on_capture_main, png_bytes)

    def _on_capture_main(self, png_bytes):
        self._restore_ui()
        self.bubble.start()
        self.bubble.set_status("已截图，模型思考中…", ACCENT)
        client = self._ensure_client()
        kwargs = dict(
            on_delta=lambda t: self.post(self.bubble.set_answer, t),
            on_stage=lambda t: self.post(self.bubble.set_status, t, ACCENT),
            on_done=lambda c, r, f: self.post(self._on_done, c, r, f),
            on_error=lambda e: self.post(self._on_error, e),
            stop_event=self.stop_event,
        )
        if self.backend_name == "opencode":
            client.answer_screen(png_bytes, PROMPT, **kwargs)
        else:
            kwargs["on_reasoning"] = lambda t: self.post(self._on_reasoning, t)
            client.answer_screen(png_bytes, **kwargs)

    def _on_reasoning(self, text):
        tail = text.replace("\n", " ")[-70:]
        self.bubble.set_status("思考中… " + tail, ACCENT)

    def _on_capture_error(self, msg):
        self.post(self._on_capture_error_main, msg)

    def _on_capture_error_main(self, msg):
        log("capture error: %s" % msg)
        self._restore_ui()
        self._reset()
        self.bubble.error("截图失败: " + msg)

    def _on_done(self, content, reasoning, finish):
        self._reset()
        if not content:
            if reasoning:
                note = "（模型只输出了思考过程，未给出正式答案）\n\n"
                if finish == "length":
                    note = "（思考过长，已达 max_tokens 上限，请调大 config.json 里的 max_tokens）\n\n"
                self.bubble.finish(note + reasoning, finish != "length")
                log("empty content, finish=%s" % finish)
                return
            self.bubble.finish("", True)
            log("empty content and empty reasoning, finish=%s" % finish)
            return
        self.bubble.finish(content, True)

    def _on_error(self, msg):
        log("api error: %s" % msg)
        self._reset()
        self.bubble.error(msg)

    def _on_hotkey(self):
        self.post(self._hotkey_action)

    def _hotkey_action(self):
        if self.busy.is_set():
            return
        if self.bubble.visible:
            self.bubble.hide()
        else:
            self._trigger()

    def start_hotkeys(self):
        try:
            self._listener = keyboard.GlobalHotKeys({self.hotkey: self._on_hotkey})
            self._listener.start()
        except Exception as exc:  # noqa: BLE001
            log("hotkey error: %r" % (exc,))
            self.post(self.bubble.error, "快捷键注册失败: " + str(exc))

    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon_img)
        d.ellipse((4, 4, 60, 60), fill=(30, 31, 38, 255))
        d.ellipse((20, 20, 44, 44), fill=(47, 107, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("答题", lambda i, it: self._request_trigger(), default=True),
            pystray.MenuItem("显示/隐藏按钮", lambda i, it: self.post(self._toggle_button)),
            pystray.MenuItem("隐藏气泡", lambda i, it: self.post(self.bubble.hide)),
            pystray.MenuItem("退出", lambda i, it: self.post(self._quit)),
        )
        self._tray = pystray.Icon("screen-answer", icon_img, "屏幕答题", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _toggle_button(self):
        if self.button.visible:
            self.button.hide()
        else:
            self.button.show()

    def _quit(self):
        self._closing = True
        self.stop_event.set()
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.opencode is not None:
                self.opencode.stop()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    def run(self):
        self.start_hotkeys()
        self.start_tray()
        self.root.after(30, self._pump)
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
