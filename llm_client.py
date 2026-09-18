import base64
import json
import os
import re
import threading

import requests

import web_fetch

TOOL_XML_RE = re.compile(r"<tool_calls>.*?</tool_calls>", re.S | re.I)
INVOKE_XML_RE = re.compile(r"<invoke\b.*?</invoke>", re.S | re.I)
LOOSE_XML_RE = re.compile(r"</?(tool_calls|invoke|parameter)\b[^>]*>", re.I)

_ANSWER_LINE_RES = [
    re.compile(r"^\**\s*([A-D]{2,4})\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*[.、．]\s*(正确|错误)\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*[.、．]\s*(\S.*?)\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*\**$"),
    re.compile(r"^\**\s*(正确|错误)\s*\**$"),
    re.compile(r"^答案[：:]\s*\**\s*([A-D])\s*[.、．]\s*(\S.*?)\s*\**$"),
    re.compile(r"^答案[：:]\s*\**\s*([A-D]{1,4})\s*\**$"),
    re.compile(r"^答案[：:]\s*\**\s*(正确|错误)\s*\**$"),
]
_NUMBERED_RE = re.compile(r"^(\d{1,2})\s*[.、．)]\s*(.+)$")


def _clean_answer_line(line):
    line = line.strip()
    if not line:
        return None
    for r in _ANSWER_LINE_RES:
        m = r.match(line)
        if not m:
            continue
        g = m.groups()
        if len(g) == 2 and g[0] in ("正确", "错误"):
            return g[0]
        if len(g) == 1:
            v = g[0]
            return "".join(sorted(set(v))) if re.fullmatch(r"[A-D]{2,4}", v) else v
        if len(g) == 2:
            if g[1] in ("正确", "错误"):
                return "%s. %s" % (g[0], g[1])
            return "%s. %s" % (g[0], g[1].strip())
    return None


def normalize_answer(text):
    """把「纯选择题/判断题」的回答规范化；问答题、检索指引等原样返回。"""
    if not text:
        return text
    t = text.strip()
    if len(t) > 300:
        return text
    lines = [l for l in t.split("\n") if l.strip()]
    if not lines or len(lines) > 6:
        return text
    out = []
    for line in lines:
        cleaned = _clean_answer_line(line)
        if cleaned is not None:
            out.append(cleaned)
            continue
        m = _NUMBERED_RE.match(line.strip())
        if m:
            inner = _clean_answer_line(m.group(2))
            if inner is not None:
                out.append("%s. %s" % (m.group(1), inner))
                continue
        return text
    if not out:
        return text
    return "\n".join(out)


def strip_tool_xml(text):
    if not text:
        return text
    t = TOOL_XML_RE.sub("", text)
    t = INVOKE_XML_RE.sub("", t)
    t = LOOSE_XML_RE.sub("", t)
    return t.strip()

DEFAULT_SYSTEM_PROMPT = """你是屏幕答题助手，尤其擅长「AI + 信息素养」类题目（信息检索、数据库使用、AI 工具、学术规范与伦理）。

【最重要】这类题大多是「实操验证题」：题目会指向某个网站、数据库、文件或系统，必须真正去查证才能得到准确答案。严禁凭记忆猜测，严禁编造。

题型与应对：
1. 数据库检索题（给出数据库+字段+检索词+时间/类型筛选）：按准确语法去查；查不到就输出检索指引。
2. 网站导航题（某网站某栏目下的某项数据）：定位到具体页面，看具体字段。
3. 文档细节题（第N页、最后一个字、表格底纹、页数、作者数、参考文献数）：必须打开原文核对。
4. 软件操作题（某软件某功能在哪/叫什么/快捷键）：说清在哪个菜单能找到。
5. 纯知识题：直接作答。

答题方法：
1. 先读题，找出信息源线索：网址、数据库名、文件名、机构名、系统名、年份、卷期、页码等。
2. 需要外部信息时调用工具查：
   - search_web：搜索引擎定位信息源
   - fetch_web：抓取网页正文（会自动处理 JS 渲染的页面；PubMed 检索页会自动给出精确命中数）
   - pubmed_search：题目问「PubMed 检索结果多少篇」时用这个，返回官方精确数字
   - github_files：题目问「GitHub 某仓库某文件夹有几个文件」时用这个，返回官方精确清单
   可多轮调用，直到信息足够。
3. 在拿到的内容里定位题目问的那个具体细节，逐一核对每个选项。题目问「数量/区间」时必须真的看到数字，不能估。
4. 多选题必须逐项独立验证每个选项：能成立的都要选上，不要因为「这用法不常见」就排除。
5. 计算题、逻辑题得出答案后要验算或回代检查一遍再输出。
6. **判断题**：先把陈述拆开，涉及计算、字符位置、日期、数量、序号的必须实际算一遍再判断，
   不要凭「看起来对」就答正确。例如 MID("APPLE",3,2) 要逐字符数：A(1) P(2) P(3) L(4) E(5)，
   从第3位取2个得「PL」，若题干说结果是「PP」则该陈述错误。

【PDF 文档】fetch_web 会自动解析 PDF 并标注每页（【第 N 页】），同时给出总页数。注意**页码偏移**：题目说「正文第 N 页」时，正文往往不是从 PDF 第 1 页开始（前面有封面、目录），要先找到正文起始页再换算，例如封面+目录占 2 页时，正文第14页 = PDF 第16页。

以下站点本程序实测可直接抓取，优先去抓：
国家药监局 nmpa.gov.cn、奎章阁 wenxianxue.cn、PubMed、课程思政平台 xhsz.news.cn、
研招网 yz.chsi.com.cn、CNKI 首页、国家图书馆 nlc.cn、国家哲社文献中心 ncpssd.cn、
中国互联网联合辟谣 piyao.org.cn、问卷星 wjx.cn、云展网、国家标准全文公开系统、
Nature、科技部 most.gov.cn（含 PDF）、
MIT Theses libraries.mit.edu、CNKI RSS rss.cnki.net、GitHub、arXiv、
全国标准信息公共服务平台 std.samr.gov.cn、NCBI bookshelf、
国家社科基金 fz.people.com.cn、HIPPTER、共产党员网 12371.cn、微词云、
福建省图书馆 fjlib.net、中国庭审公开网 tingshen.court.gov.cn、
OALib、维普 cqvip.com、阿里云开发者社区、中国证券业协会 www.sac.net.cn

以下站点需要 JS 渲染，程序会自动处理，可以正常抓：
国家自然科学基金 kd.nsfc.cn、一席 yixi.tv、川大图书馆、百度学术、360图片、
国家智慧教育平台 graduate.smartedu.cn、讯飞星火、USPTO、智慧树 zhihuishu.com、
国家统计局 data.stats.gov.cn

以下站点实测抓不到（反爬/需登录/需交互），**不要浪费轮次去抓，直接输出检索指引**：
IEEE Xplore、Taylor&Francis、牛津学术、百度指数、UNESCO 数字图书馆、
智谱清言、纳米AI、ECharts 示例页、PubScholar、
国家知识产权局专利检索系统、中国证券业协会从业人员查询页 gs.sac.net.cn、国家卫健委、
千问 qianwen.com、知乎直答、ACM DL、豆包、中国专利公布公告系统、星火科研助手 paper.xfyun.cn、
ScienceDirect（403）、Wiley（Cloudflare 安全验证）、
CNKI 中国法律智库 lawpro.cnki.net、CNKI 中国学术会议网 conf.cnki.net、
腾讯ima、万方智研平台、重庆大学图书馆、iconfont

【检索语法速查】写检索式或指引时必须用对：
- PubMed：字段标签放方括号内，如 heart failure[Title]、CRISPR[Title/Abstract]、therapy[Title]；
  精确短语用双引号 "heart failure"[Title]（引号内不分词）；布尔 AND/OR/NOT 大写；
  文献类型过滤器叫 Article type（RCT 即 Randomized Controlled Trial 属于其中）
- IEEE Xplore：高级检索选 Document Title 字段；通配符 * 截词，如 wireless netw*
- Taylor & Francis：高级检索分字段，Title 填题名、Affiliations 填作者单位
- CNKI：高级检索可选 篇名/主题/作者/文献来源，支持「精确」匹配；来源类别筛 CSSCI/北大核心/CSCD
- 万方：高级检索，主题字段；核心收录筛 CSSCI/北大核心
- 维普：高级检索，期刊级别筛 北大核心/CSSCI/CSCD
- 国家自然科学基金 kd.nsfc.cn：「信息检索」→「结题项目」→「高级检索」，按 结题年度/资助类型/申请代码
- 国家哲学社会科学文献中心 ncpssd.cn：「资源」→「集刊」，左侧「核心分类」筛 CSSCI
- 百度指数 index.baidu.com：「人群画像」→「添加对比」→ 选时间范围 → 点「省份」
- 百度高级搜索：可限定文档格式（doc/xls/ppt/pdf 等）
- 百度学术：检索后点「引用」可导出 APA / MLA / 国标7714 三种格式
- USPTO 专利：用 Patent Public Search
- 国家药监局：药品 → 境外生产药品

【软件操作速查】
- WPS/Word 替换的特殊格式：^p 段落标记、^t 制表符、^m 手动分页符、^# 任意数字
- Word 快速分页：Ctrl+Enter（不是 Ctrl+Shift）
- Excel 字符串拆分重组：LEFT / MID / RIGHT 配 &、CONCATENATE、TEXT、REPLACE
- Excel VLOOKUP：第1参数=查找值，第2参数=查找范围（必须包含查找值与返回值列），
  第3参数=返回值在范围内的相对列号，第4参数=FALSE 表示精确匹配
- QQ 截图工具栏：A = 添加文本（另有马赛克、长截图、钉在桌面等）

【其它检索常识】
- 百度高级语法：filetype:PDF 限定文献类型、site:域名 限定来源网站（域名不带 http://）；
  「file:PDF」不是有效语法
- 万方：引号括起来是精确匹配，对连字符、空格高度敏感，会漏检「白细胞介素 6」「IL-6」等不同写法
- GB/T 7714 文献类型标识：期刊 J、会议 C、学位论文 D、专著 M、报纸 N、报告 R、标准 S、专利 P
- 预印本：arXiv 数学大类收录起始于 1992 年
- 维普：高级检索可选精确匹配，筛 CSSCI/北大核心/CSCD，结果可按学科主题聚合
- ScienceDirect：官方帮助页明确支持的检索技术只有 AND/OR/NOT、连字符(=NOT)、括号、
  双引号短语、复数与拼写变体；**不支持截词符 *（输入 electro* 不会匹配 electron/electrode）**
- 中国庭审公开网可用案号检索；国家统计局 data.stats.gov.cn 走「地区数据→分省年度数据」

【信息源需要登录或付费时】不要编造答案，也不要只回「无法核实」。改为输出一份**能直接照做的检索指引**，写清：
- 用哪个库（并说明有无免费替代，如国家哲学社会科学文献中心 ncpssd.cn、机构图书馆入口）
- 入口路径：具体到点哪个按钮，如「CNKI 首页 → 高级检索」
- 检索式：哪个字段 + 什么关键词 + 逻辑，如「篇名 = 信息素养 AND 出版年度 = 2015-2021」
- 筛选条件：文献类型、来源类别、学科、时间范围要勾选什么
- 排序方式：按被引 / 按相关度 / 按时间，以及点哪里
- 看哪个字段来对答案：如「结果第 1 条的『来源』列（万方叫『刊名』）就是期刊名，与选项比对」

输出格式（严格遵守）：
- 单选题：只输出选项字母和答案，例如「A. 8」。不要任何解释、理由或括号说明。
- 多选题：只输出选项字母，按字母顺序连写，例如「ABC」。不要任何解释、理由或括号说明。
- 判断题：只输出「正确」或「错误」。
- 填空题：只输出填空内容，多个空用「；」分隔。
- 问答题/计算题：输出完整解题过程和最终答案。
- 上面「需要登录付费」的情形：输出检索指引，不要给答案。
- 截图中有多道题：按题号逐题作答，每题套用上面的格式。
- 截图里没有可识别的题目：只回复「未检测到题目。」

数学公式用 LaTeX 写在 $...$ 或 $$...$$ 中（程序会自动渲染成图片）。不要输出代码块。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "用搜索引擎检索信息，返回若干条结果的标题、网址和摘要。适合先用它定位信息源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，尽量精确"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web",
            "description": "抓取指定网址的正文内容。用于查看搜索结果中某个页面的具体内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的完整网址"},
                    "query": {
                        "type": "string",
                        "description": "可选。用于从长网页中筛选出最相关的段落",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pubmed_search",
            "description": (
                "用 NCBI 官方接口精确查询 PubMed 命中文献数（比抓网页准确）。"
                "题目问「PubMed 检索结果有多少篇」时必须用这个工具。"
                "支持 PubMed 字段语法，如 CRISPR[Title/Abstract] NOT therapy[Title]。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": 'PubMed 检索式，例如 "heart failure"[Title] AND 2020:2024[dp]',
                    },
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_files",
            "description": (
                "用 GitHub 官方接口精确列出仓库某个目录下的文件（题目常问「某文件夹有几个文件」）。"
                "比抓网页准确，也能绕开 github.com 网页访问不稳的问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "仓库所有者，如 onnx"},
                    "repo": {"type": "string", "description": "仓库名，如 onnx"},
                    "path": {"type": "string", "description": "目录路径，如 LICENSES；留空表示根目录"},
                    "ref": {"type": "string", "description": "分支或 tag，如 main；可留空"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
]


class LLMError(Exception):
    pass


class VisionClient:
    def __init__(self, cfg):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.system_prompt = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        self.max_tokens = cfg.get("max_tokens", 32768)
        self.temperature = cfg.get("temperature", 0.2)
        self.timeout = cfg.get("timeout", 300)
        self.enable_web = cfg.get("enable_web_fetch", True)
        self.web_max_chars = cfg.get("web_max_chars", web_fetch.DEFAULT_PAGE_CHARS)
        self.web_total_chars = cfg.get("web_total_chars", web_fetch.DEFAULT_TOTAL_CHARS)
        self.web_max_rounds = cfg.get("web_max_rounds", 3)
        self.web_max_urls = cfg.get("web_max_urls", 6)
        self.api_key = os.environ.get(cfg.get("api_key_env", "USTC_API_KEY"), "").strip()
        if not self.api_key:
            raise LLMError(
                "未找到 API Key。请设置环境变量 " + cfg.get("api_key_env", "USTC_API_KEY")
            )

    def _data_url(self, png_bytes):
        return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    def _messages(self, png_bytes, question=None):
        user_content = [
            {"type": "text", "text": question or "请按格式要求解答截图中的题目。"},
            {"type": "image_url", "image_url": {"url": self._data_url(png_bytes)}},
        ]
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _stream_once(self, messages, on_delta, on_reasoning, stop_event, allow_tools=True):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            "messages": messages,
        }
        if self.enable_web and allow_tools:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = self.base_url + "/v1/chat/completions"
        content = []
        reasoning = []
        calls = {}
        finish = None

        with requests.post(
            url, headers=headers, data=json.dumps(payload), stream=True, timeout=self.timeout
        ) as resp:
            if resp.status_code != 200:
                raise LLMError("接口返回 %s: %s" % (resp.status_code, resp.text[:400]))
            for raw in resp.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    break
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                    if on_reasoning:
                        on_reasoning("".join(reasoning))
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["arguments"] += fn["arguments"]
                piece = delta.get("content")
                if piece is None:
                    piece = (choice.get("message") or {}).get("content")
                if piece:
                    content.append(piece)
                    if on_delta:
                        on_delta("".join(content))

        tool_calls = [calls[k] for k in sorted(calls)]
        return "".join(content).strip(), "".join(reasoning).strip(), finish, tool_calls

    def _run_tool(self, call, on_stage):
        name = call.get("name", "")
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            return "参数解析失败"
        if name == "search_web":
            q = (args.get("query") or "").strip()
            if not q:
                return "缺少 query"
            if on_stage:
                on_stage("正在搜索：%s" % q[:40])
            hits = web_fetch.search(q, max_results=self.web_max_urls)
            if on_stage:
                on_stage("搜索到 %d 条结果" % len(hits))
            return web_fetch.format_search_for_prompt(hits, q)
        if name == "fetch_web":
            u = (args.get("url") or "").strip()
            if not u.startswith("http"):
                return "网址无效"
            if on_stage:
                on_stage("正在抓取：%s" % u[:50])
            res = web_fetch.fetch(
                u, query=(args.get("query") or ""), max_chars=self.web_max_chars
            )
            if on_stage:
                on_stage("已抓取 %d 字" % res["chars"])
            return web_fetch.format_for_prompt([res])
        if name == "pubmed_search":
            term = (args.get("term") or "").strip()
            if not term:
                return "缺少 term"
            if on_stage:
                on_stage("正在查 PubMed：%s" % term[:50])
            r = web_fetch.pubmed_search(term)
            if not r["ok"]:
                return "PubMed 接口调用失败：%s" % r["error"]
            if on_stage:
                on_stage("PubMed 命中 %s 篇" % r["count"])
            return "【PubMed 精确命中数】%s 篇\n检索式：%s\n翻译后：%s" % (
                r["count"], term, r.get("translation", ""))
        if name == "github_files":
            owner = (args.get("owner") or "").strip()
            repo = (args.get("repo") or "").strip()
            if not owner or not repo:
                return "缺少 owner 或 repo"
            path = (args.get("path") or "").strip()
            ref = (args.get("ref") or "").strip() or None
            if on_stage:
                on_stage("正在查 GitHub：%s/%s/%s" % (owner, repo, path))
            g = web_fetch.github_dir(owner, repo, path, ref)
            if not g["ok"]:
                return "GitHub 查询失败：%s" % g["error"]
            return g["text"]
        return "未知工具"

    def answer_screen(
        self,
        png_bytes,
        on_delta=None,
        on_reasoning=None,
        on_stage=None,
        on_done=None,
        on_error=None,
        stop_event=None,
    ):
        def run():
            try:
                messages = self._messages(png_bytes)
                text = reasoning = ""
                finish = None
                for round_no in range(self.web_max_rounds + 1):
                    last_round = round_no >= self.web_max_rounds
                    if last_round:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "（工具调用次数已用完，现在不能再调用工具。）\n"
                                    "请立即处理：\n"
                                    "1) 若已获得的信息足以确定答案，直接按输出格式给出答案；\n"
                                    "2) 若该题必须访问需登录/付费的数据库（如 CNKI、万方、维普、ScienceDirect）"
                                    "而无法核实，请按系统提示词的要求输出【检索指引】，"
                                    "写清入口、检索式、筛选条件、排序方式、看哪个字段对答案；\n"
                                    "3) 不要再输出任何工具调用标记或 JSON。"
                                ),
                            }
                        )
                    text, reasoning, finish, calls = self._stream_once(
                        messages,
                        on_delta,
                        on_reasoning,
                        stop_event,
                        allow_tools=not last_round,
                    )
                    if stop_event is not None and stop_event.is_set():
                        break
                    if not calls or last_round:
                        break

                    messages.append(
                        {
                            "role": "assistant",
                            "content": text or None,
                            "tool_calls": [
                                {
                                    "id": c["id"] or ("call_%d" % i),
                                    "type": "function",
                                    "function": {
                                        "name": c["name"],
                                        "arguments": c["arguments"] or "{}",
                                    },
                                }
                                for i, c in enumerate(calls)
                            ],
                        }
                    )
                    for i, c in enumerate(calls):
                        result = self._run_tool(c, on_stage)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": c["id"] or ("call_%d" % i),
                                "content": result,
                            }
                        )
                    if on_stage:
                        on_stage("已获取资料，正在作答…")

                final = normalize_answer(strip_tool_xml(text))
                if not final and reasoning:
                    final = strip_tool_xml(reasoning)
                if on_done:
                    on_done(final, reasoning, finish)
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(str(exc))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t
