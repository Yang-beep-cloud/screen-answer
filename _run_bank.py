import json
import os
import re
import sys
import time
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

import app as appmod
from llm_client import normalize_answer

HERE = os.path.dirname(os.path.abspath(__file__))
BANK = os.path.join(HERE, "data", "测试集_150题.json")
OUT = os.path.join(HERE, "data", "全库结果.json")
FONT = "C:/Windows/Fonts/msyh.ttc"

TAG = {"multi": "【多选题】", "judge": "【判断题】", "single": "【单选题】"}


def render(q):
    lines = []
    stem = TAG.get(q["type"], "【单选题】") + q["stem"]
    while len(stem) > 34:
        lines.append(stem[:34])
        stem = stem[34:]
    lines.append(stem)
    lines.append("")
    for k in sorted(q["options"]):
        lines.append("%s. %s" % (k, q["options"][k]))
    img = Image.new("RGB", (1500, max(60 + len(lines) * 46, 240)), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 25)
    y = 30
    for ln in lines:
        d.text((30, y), ln, fill="black", font=f)
        y += 46
    b = BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


GUIDE_RE = re.compile(
    r"无法给出选项字母|未能核实|无法核实|无法自动|检索指引|自行核对|自行验证|自行查证"
    r"|请按.{0,8}路径|不能凭(记忆|印象)|未抓到|未能抓|抓取失败|拦截页|请手动|照做"
    r"|不给选项字母|不给答案|不能给出|不给选|请照下面|请按下面|按下列|请按以下"
    r"|无法访问|无法登录|需登录|需要登录|必须登录|请自行|自查|无法确认|无法直接")


def norm(text):
    """把回答归类为 (答案, 类型)。

    注意两个易错点：
    (1) 「A.」这种只有字母加句点、后面没内容的输出，其实就是在选 A；
    (2) 判断题答案里可能出现「正确」二字，但整篇其实是检索指引，
        所以必须先判断是不是指引，再看末行是不是选项。
    """
    t = normalize_answer(text)
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    is_guide = bool(GUIDE_RE.search(t)) and len(t) > 60
    if not lines:
        return "", "guide" if is_guide else "empty"
    last = lines[-1]
    m = re.match(r"^\**\s*([A-D]{2,4})\s*\**$", last)
    if m and not is_guide:
        return "".join(sorted(set(m.group(1)))), "answer"
    m = re.match(r"^\**\s*([A-D])\s*[.、．]?\s*(\S.*)?$", last)
    if m and not is_guide and not re.match(r"^\**\s*([A-D]{2,4})\s*\**$", last):
        return m.group(1), "answer"
    if not is_guide and re.fullmatch(r"\**\s*(正确|错误)\s*\**", last):
        return last.strip("* "), "answer"
    if is_guide:
        return "", "guide"
    return "", "other"


def ask(q, budget=600):
    appmod.capture_async = lambda s, cb, e, _p=render(q): cb(_p)
    a = appmod.App()
    a.root.after(30, a._pump)
    box = {"ans": ""}

    def pump(pred, limit):
        t0 = time.time()
        while time.time() - t0 < limit:
            a.root.update()
            time.sleep(0.02)
            if pred():
                return True
        return False

    t0 = time.time()
    a._request_trigger()
    pump(lambda: a.busy.is_set(), 10)
    pump(lambda: not a.busy.is_set(), budget)
    pump(lambda: False, 0.4)
    box["ans"] = a.bubble._full_text.strip()
    try:
        a.root.destroy()
    except Exception:
        pass
    return box["ans"], time.time() - t0


def main():
    with open(BANK, encoding="utf-8") as f:
        items = json.load(f)
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else len(items)
    done = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for r in json.load(f):
                    done[r["key"]] = r
        except Exception:
            done = {}
    rows = list(done.values())
    for idx in range(start, min(end, len(items))):
        q = items[idx]
        key = "%s-Q%d" % (q["bank"], q["no"])
        if key in done:
            continue
        try:
            ans, el = ask(q)
        except Exception as exc:  # noqa: BLE001
            ans, el = "!!异常 %r" % (exc,), 0.0
        got, kind = norm(ans)
        exp = "".join(sorted(set(q["answer"])))
        if q["type"] == "judge" and got in ("正确", "错误"):
            got = {v: k for k, v in q["options"].items()}.get(got, got)
        if got == exp:
            verdict = "OK"
        elif not got and kind == "guide":
            verdict = "GUIDE"
        elif not got:
            verdict = "EMPTY"
        else:
            verdict = "WRONG"
        row = {
            "key": key, "type": q["type"], "expect": exp, "got": got,
            "verdict": verdict, "sec": round(el, 1), "answer": ans,
        }
        rows.append(row)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("[%3d/%3d] %-16s %-6s 期望=%-5s 实得=%-5s %5.0fs"
              % (idx + 1, len(items), key, verdict, exp, got or "?", el), flush=True)
    ok = sum(1 for r in rows if r["verdict"] == "OK")
    gd = sum(1 for r in rows if r["verdict"] == "GUIDE")
    wr = sum(1 for r in rows if r["verdict"] == "WRONG")
    em = sum(1 for r in rows if r["verdict"] == "EMPTY")
    print("\n===== 汇总（已跑 %d 题）=====" % len(rows))
    print("答对 %d | 给指引 %d | 答错 %d | 空 %d" % (ok, gd, wr, em))


if __name__ == "__main__":
    main()
