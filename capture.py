import threading
from io import BytesIO

import mss
from PIL import Image


def capture_primary(scale=1.0):
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if scale and scale != 1.0:
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        img = img.resize((w, h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def capture_async(scale, callback, on_error):
    def run():
        try:
            callback(capture_primary(scale))
        except Exception as exc:  # noqa: BLE001
            on_error(str(exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
