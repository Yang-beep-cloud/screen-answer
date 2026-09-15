import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 45123

NO_TOOLS = [
    "bash",
    "read",
    "glob",
    "grep",
    "edit",
    "write",
    "task",
    "todowrite",
    "skill",
    "apply_patch",
    "question",
    "invalid",
]


def find_opencode():
    home = os.path.expanduser("~")
    exe_cands = [
        os.path.join(home, "AppData", "Roaming", "npm", "node_modules", "opencode-ai", "bin", "opencode.exe"),
        "/usr/local/bin/opencode",
        "/opt/homebrew/bin/opencode",
    ]
    for c in exe_cands:
        if os.path.isfile(c):
            return c
    found = shutil.which("opencode")
    if found and not found.lower().endswith((".cmd", ".ps1", ".bat")):
        return found
    cmd = os.path.join(home, "AppData", "Roaming", "npm", "opencode.cmd")
    if os.path.isfile(cmd):
        return cmd
    return found


class OpenCodeBackend:
    def __init__(self, cfg):
        oc = cfg.get("opencode", {})
        self.port = int(oc.get("port", DEFAULT_PORT))
        self.provider = oc.get("provider", "ustc")
        self.model = oc.get("model", "glm-5.3-flash")
        self.agent = oc.get("agent", "build")
        self.enable_web = oc.get("enable_web", True)
        self.host = "127.0.0.1"
        self.base = "http://%s:%d" % (self.host, self.port)
        self.proc = None
        self.session_id = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _auth_header(self):
        pw = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        return "Basic " + token

    def _request(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": self._auth_header(), "Content-Type": "application/json"},
        )
        with self._opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None

    def health(self, timeout=3):
        try:
            self._request("GET", "/global/health", timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_server(self, on_stage=None, wait=60):
        if self.health():
            return True
        exe = find_opencode()
        if not exe:
            raise RuntimeError("未找到 opencode 可执行文件，请先 npm i -g opencode-ai")
        if on_stage:
            on_stage("正在启动 opencode 服务…")
        creation = 0
        if os.name == "nt":
            creation = 0x08000000
        self.proc = subprocess.Popen(
            [exe, "serve", "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(1.5)
            if self.health():
                return True
            if self.proc.poll() is not None:
                raise RuntimeError("opencode 服务启动失败（进程已退出）")
        raise RuntimeError("opencode 服务启动超时")

    def _ensure_session(self):
        if self.session_id:
            return self.session_id
        s = self._request("POST", "/session", {})
        self.session_id = s["id"]
        return self.session_id

    def _tools(self):
        tools = {t: False for t in NO_TOOLS}
        tools["webfetch"] = bool(self.enable_web)
        tools["websearch"] = bool(self.enable_web)
        return tools

    def answer_screen(
        self,
        png_bytes,
        prompt,
        on_delta=None,
        on_stage=None,
        on_done=None,
        on_error=None,
        stop_event=None,
        poll=2.0,
        timeout=600,
    ):
        def run():
            try:
                self.ensure_server(on_stage)
                if on_stage:
                    on_stage("正在创建会话…")
                sid = self._ensure_session()
                body = {
                    "parts": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "file",
                            "mime": "image/png",
                            "filename": "screen.png",
                            "url": "data:image/png;base64,"
                            + base64.b64encode(png_bytes).decode("ascii"),
                        },
                    ],
                    "model": {"providerID": self.provider, "modelID": self.model},
                    "agent": self.agent,
                    "tools": self._tools(),
                }
                if on_stage:
                    on_stage("opencode 正在作答…")
                self._request("POST", "/session/%s/prompt_async" % sid, body, timeout=60)

                deadline = time.time() + timeout
                last = ""
                final = None
                while time.time() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        break
                    time.sleep(poll)
                    msgs = self._request("GET", "/session/%s/message" % sid, timeout=60)
                    for m in msgs:
                        info = m.get("info", {})
                        if info.get("role") != "assistant":
                            continue
                        texts = [
                            p.get("text", "")
                            for p in m.get("parts", [])
                            if p.get("type") == "text" and p.get("text")
                        ]
                        joined = "\n".join(texts)
                        if joined and joined != last:
                            last = joined
                            if on_delta:
                                on_delta(joined)
                        if info.get("time", {}).get("completed"):
                            final = joined
                    if final is not None:
                        break
                if on_done:
                    on_done(final or last, "", None)
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(str(exc))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
