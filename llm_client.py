import base64
import json
import os
import re
import threading

import requests

import web_fetch

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_DIR = os.path.join(HERE, "data")
MODULE_LIST_PATH = os.path.join(KNOWLEDGE_DIR, "模块清单.md")
KNOWLEDGE_PATH = os.path.join(KNOWLEDGE_DIR, "知识要点.md")

_KNOWLEDGE_CACHE = {"modules": None, "full": None}


def load_module_list():
    if _KNOWLEDGE_CACHE["modules"] is None:
        try:
            with open(MODULE_LIST_PATH, "r", encoding="utf-8") as f:
                _KNOWLEDGE_CACHE["modules"] = f.read().strip()
        except OSError:
            _KNOWLEDGE_CACHE["modules"] = ""
    return _KNOWLEDGE_CACHE["modules"]


def load_full_knowledge():
    if _KNOWLEDGE_CACHE["full"] is None:
        try:
            with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
                _KNOWLEDGE_CACHE["full"] = f.read()
        except OSError:
            _KNOWLEDGE_CACHE["full"] = ""
    return _KNOWLEDGE_CACHE["full"]


def search_knowledge(query, limit=3):
    """在知识要点里按关键词检索，返回最相关的段落。"""
    text = load_full_knowledge()
    if not text:
        return ""
    if not query:
        return text[:4000]
    blocks = re.split(r"\n(?=#{2,3} )", text)
    tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", query.lower())]
    if not tokens:
        return text[:4000]
    scored = []
    for b in blocks:
        low = b.lower()
        score = sum(low.count(t) for t in tokens)
        if score:
            scored.append((score, len(b), b))
    if not scored:
        return "知识要点里没有与「%s」直接相关的内容。\n\n" % query + text[:1500]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return "\n\n".join(b for _, _, b in scored[:limit])

TOOL_XML_RE = re.compile(r"<tool_calls>.*?</tool_calls>", re.S | re.I)
INVOKE_XML_RE = re.compile(r"<invoke\b.*?</invoke>", re.S | re.I)
LOOSE_XML_RE = re.compile(r"</?(tool_calls|invoke|parameter)\b[^>]*>", re.I)

_ANSWER_LINE_RES = [
    re.compile(r"^\**\s*([A-D]{2,4})\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*[.、．]\s*(正确|错误)\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*[.、．]\s*(\S.*?)\s*\**$"),
    re.compile(r"^\**\s*([A-D])\s*\**$"),
    re.compile(r"^\**\s*(正确|错误)\s*\**$"),
    re.compile(r"^\**\s*答案[：:]\s*\**\s*([A-D])\s*[.、．]\s*(\S.*?)\s*\**$"),
    re.compile(r"^\**\s*答案[：:]\s*\**\s*([A-D]{1,4})\s*\**$"),
    re.compile(r"^\**\s*答案[：:]\s*\**\s*(正确|错误)\s*\**$"),
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

    # 首行就是干净答案、后面却跟着解释 → 只保留答案
    first = _clean_answer_line(lines[0])
    if first is not None and len(lines) > 1:
        rest_are_answers = all(
            _clean_answer_line(l) is not None or _NUMBERED_RE.match(l.strip())
            for l in lines[1:]
        )
        if not rest_are_answers:
            return first

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

【绝对禁止凭记忆作答的情形】
题目指向下列来源时，本程序无法访问（需登录或有反爬验证码），**必须输出检索指引，
严禁给出选项字母或数字**：
CNKI/知网（滑块验证码）、万方、维普、IEEE Xplore、Taylor&Francis、牛津学术、
ScienceDirect、Wiley、UNESCO 数字图书馆、百度指数、智谱清言、ACM DL、豆包、
千问、知乎直答、腾讯ima、万方智研、CNKI 法律智库、CNKI 学术会议网、
重庆大学图书馆、iconfont、国家知识产权局专利检索、证券业协会从业人员查询、
国家卫健委、星火科研助手、中国专利公布公告系统

判断标准只有一条：**没有通过工具真正拿到数据，就不要给答案。**
即使你「记得」这道题的答案，也不能输出 —— 记忆可能过时或错误，必须让用户自行核实。

【通用铁律：任何网站都适用，不限于上面的清单】
- 只有当**你实际抓取到的内容里明确出现**了题目所问的信息（栏目名、数字、字段、功能名等），
  才可以给出选项答案，并在心里确认「这一条是我在原文里看到的」。
- 只要出现下面任一情况，**一律输出检索指引，不许给选项字母**：
  · 抓取失败、被反爬拦截、需要登录；
  · 抓到了页面，但内容里**没有**题目所问的那项信息；
  · 你对某个选项的依据只能来自「印象」「常识」「应该差不多」。
- **不要因为「这个站点看起来是公开的」就假定自己知道答案。**
  站点公开 ≠ 你一定抓到了那条信息。判断依据只能是「我这次实际抓到了吗」。

唯一例外（可凭知识直接作答）：
1. 通用工具知识：Excel/Word/WPS 函数与快捷键、检索语法与运算符、
   GB/T 7714 文献类型标识、PubMed 字段标签、文献管理软件的基本用法等；
2. 方法论与概念题：如何提高检索质量、AI 使用规范与学术伦理原则、
   信息核验思路、不同 AI 工具的功能定位与差异辨析（如 Trust Card 与
   Claim Radar 的用途区别、深度研究模式的作用）等；
3. 这类题的正确答案来自确定的规则或原理，不依赖某个网站当天的具体内容，
   因此**直接按知识判断即可，不必联网找证据**。
   只有「某网站/数据库/文件里的具体数据、栏目名、页码、数量」才必须有抓到的原文依据。

【输出选项字母前的最后自检（必做）】
在给出任何选项字母之前，先问自己一句：
**「题目问的那项信息（栏目名／数字／字段名／功能名／作者／日期），
我在这轮实际抓到的原文里，具体是哪一句看到的？」**
- 能指出具体句子 → 可以给答案。
- 指不出来（只能说「印象里」「应该是」「一般是这样」「按常理」）→ **不许给答案，改输出检索指引**。
- 特别注意：**网站公开可抓 ≠ 你已经抓到了那条信息**。
  抓到了网站首页、但首页里没有题目问的那一项，同样属于「没抓到」，必须给指引。

题型与应对：
1. 数据库检索题（给出数据库+字段+检索词+时间/类型筛选）：按准确语法去查；查不到就输出检索指引。
2. 网站导航题（某网站某栏目下的某项数据）：定位到具体页面，看具体字段。
3. 文档细节题（第N页、最后一个字、表格底纹、页数、作者数、参考文献数）：必须打开原文核对。
4. 软件操作题（某软件某功能在哪/叫什么/快捷键）：说清在哪个菜单能找到。
5. 纯知识题：直接作答。

官方备赛要点（据大赛培训材料）：
- 以探索、实操型题目为主，重点考「栏目结构、检索路径、功能入口、页面细节」
- 常见出题方向：项目许可协议、项目主题、数据集类型、模型任务类型；
  某个按钮的功能、快捷键、输出文件格式、是否收费
- 涉及平台功能、操作细节和具体结果时，必须通过实际平台或官方文件核实

答题方法：
1. 先读题，找出信息源线索：网址、数据库名、文件名、机构名、系统名、年份、卷期、页码等。
2. 需要外部信息时调用工具查：
   - search_web：搜索引擎定位信息源
   - fetch_web：抓取网页正文（会自动处理 JS 渲染的页面；PubMed 检索页会自动给出精确命中数）
   - pubmed_search：题目问「PubMed 检索结果多少篇」时用这个，返回官方精确数字
   - github_files：题目问「GitHub 某仓库某文件夹有几个文件」时用这个，返回官方精确清单
   可多轮调用，直到信息足够。
3. 在拿到的内容里定位题目问的那个具体细节，逐一核对每个选项。题目问「数量/区间」时必须真的看到数字，不能估。
4. 多选题必须逐项独立验证，**只选有明确依据的选项**：
   - 只有在查到的原文/页面里**明确出现或明确成立**的选项才选；
   - 没查到依据、只是「看起来合理」或「比较常见」的选项**不要选**；
   - 也不要把「可能相关」当成「成立」，宁可少选也不臆选；
   - 逐项给出该选项成立的依据（在原文哪里看到的），再决定选不选。
   **注意**：这里的「依据」对**通用知识题**来说就是你对函数/语法/规则的掌握，
   例如 TEXT/LEFT/MID/VLOOKUP 的用法、检索运算符的含义、GB/T 7714 的标识，
   属于确定的知识，**直接按知识判断即可，不必去联网找证据**，
   也不要因为网上搜不到就否定它。
   仅当题目依赖「某个网站/数据库/文件里的具体内容」时，才必须有抓取到的原文依据。
   **题干出现「包括哪些」「有哪些」「正确的有」「说法正确的是」等措辞、
   且选项之间不互斥时，很可能是多选**；若你判断成立的选项不止一个，
   必须全部输出，绝不能只输出一个字母。
   反之，若题干明确是单选或选项互斥，才输出单个字母。
5. 计算题、逻辑题得出答案后要验算或回代检查一遍再输出。
6. **判断题**：先把陈述拆开，涉及计算、字符位置、日期、数量、序号的必须实际算一遍再判断，
   不要凭「看起来对」就答正确。例如 MID("APPLE",3,2) 要逐字符数：A(1) P(2) P(3) L(4) E(5)，
   从第3位取2个得「PL」，若题干说结果是「PP」则该陈述错误。
7. **题干问「错误的是 / 不正确的是 / 不包括 / 不属于」时**：要**逐项判断每个选项本身是否成立**，
   然后选出那些**不成立**的选项，可能不止一个，不要只挑一个就提交。
   例：Word 替换中「任意数字用 ^%」不成立（应为 ^#）、「图形用 ^p」不成立（应为 ^g），
   若题干问「错误的是」，则这两项都要选。
   反之，问「正确的是」就选成立的。先判断选项本身对错，再按题干要求取舍。

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
- CNKI：高级检索可选 篇名/主题/作者/文献来源，支持「精确」匹配；来源类别筛 CSSCI/北大核心/CSCD。
  **同字段内的逻辑符：+ 表示「或」、* 表示「与」、- 表示「非」**
  （例如 篇名填「AI + 人工智能」表示篇名含AI或人工智能；主题填「法律 + 法规」）
  CNKI 检索接口带滑块验证码（captchaType=blockPuzzle），程序无法自动查询，必须输出指引
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
- Zotero「通过标识符添加条目」(Add Item by Identifier) 支持的标识符**只有**：
  **DOI、ISBN、PubMed ID(PMID)、arXiv ID、ADS Bibcode**。
  **不支持 ISSN**、不支持 CNKI 链接、不支持标题（官方支持页原文列举）。
  这属于确定的功能规则，直接按此判断，不要联网搜（搜到的二手教程常把 ISSN 也列进去）。
- Zotero 导入题录方式：DOI/标识符、数据库检索、本地 PDF 拖入、RIS/BibTeX 题录文件
- Mendeley / 知网研学 同样支持 DOI、RIS/BibTeX 导入

【其它检索常识】
- 百度高级语法：filetype:PDF 限定文献类型、site:域名 限定来源网站（域名不带 http://）；
  「file:PDF」不是有效语法
- 万方：引号括起来是精确匹配，对连字符、空格高度敏感，会漏检「白细胞介素 6」「IL-6」等不同写法
- GB/T 7714-2025 文献类型标识（官方培训给出）：
  [J] 期刊、[M] 图书、[D] 学位论文、[C] 会议、[P] 专利、
  [EB] 网页、[DS] 数据集、[PP] 预印本
- QQ 快捷键：截图 Ctrl+Alt+A、录屏 Ctrl+Alt+R、屏幕识图 Ctrl+Alt+O、屏幕翻译 Ctrl+Alt+F
- Excel 高频函数：LEFT/RIGHT/MID/FIND/TRIM/REPLACE/SUBSTITUTE/CONCATENATE/CLEAN/
  VLOOKUP/COUNTIF/COUNTIFS/SUMIF/SUMIFS/IF/INDEX/MATCH
  （重点记函数作用，按场景选对函数，不必背参数）
- 各场景 AI 工具（官方备赛清单）：
  通用：DeepSeek、豆包、腾讯元宝、智谱清言、通义/千问、Kimi、讯飞星火、文心、纳米AI
  办公：PPT(Kimi/通义千问/豆包/智谱清言)、写作(文心/豆包/讯飞星火)、
  数据处理(豆包/智谱清言)、会议整理(通义千问)
  科研：LeapSpace、CNKI AI、星火科研助手、秘塔AI搜索、豆包、AMiner
  多模态：图像视频(豆包/智谱清言/万相/可灵AI)、音乐(豆包)、数字人(闪剪/蝉镜)
  文献管理：知网研学、Zotero、Mendeley（导入方式：DOI/数据库检索/本地PDF/RIS·BibTeX题录）
  文献追踪：CNKI 关键词订阅与 RSS、万方「我的订阅」、PubMed Create RSS / Create Alert
- 免费学习资源入口：高校信息素养教育数据库 suyang.zxhnzq.com/lecture、
  知网学术大讲堂 k.cnki.net/home、万方视频 video.wangfangdata.com.cn、
  学习强国 xuexi.cn、和鲸社区 HeyWhale、百度 AI Studio、DataFountain
- 微词云非会员每日仅免费 3 次，不要反复刷新
- 产品界面与功能会更新，以平台官网/APP 实际显示为准
- 维普：高级检索可选精确匹配，筛 CSSCI/北大核心/CSCD，结果可按学科主题聚合
- ScienceDirect：官方帮助页明确支持的检索技术只有 AND/OR/NOT、连字符(=NOT)、括号、
  双引号短语、复数与拼写变体；**不支持截词符 *（输入 electro* 不会匹配 electron/electrode）**
- 中国庭审公开网可用案号检索；国家统计局 data.stats.gov.cn 走「地区数据→分省年度数据」

【信息源需要登录或付费时】不要编造答案，也不要只回「无法核实」。改为输出一份**能直接照做的检索指引**。

**写指引前必须先核实，严禁凭记忆写步骤：**
- 必须至少调用一次工具：`fetch_web` 打开目标网站的公开页面（免登录能看到的首页/栏目页），
  确认**实际的菜单名、按钮名、栏目名**；或 `search_web` 找到官方帮助页/教程。
- 菜单名要照抄页面上真实出现的文字，不要用「可能是」「部分版本显示为」这类猜测措辞。
- 若某一步确实无法核实，必须在该步后明确标注「（此步未核实，以实际页面为准）」。

指引要写清：
- 用哪个库（并说明有无免费替代，如国家哲学社会科学文献中心 ncpssd.cn、机构图书馆入口）
- 入口路径：具体到点哪个按钮，如「CNKI 首页 → 高级检索」
- 检索式：哪个字段 + 什么关键词 + 逻辑，如「篇名 = 信息素养 AND 出版年度 = 2015-2021」
- 筛选条件：文献类型、来源类别、学科、时间范围要勾选什么
- 排序方式：按被引 / 按相关度 / 按时间，以及点哪里
- 看哪个字段来对答案：如「结果第 1 条的『来源』列（万方叫『刊名』）就是期刊名，与选项比对」

指引要**逐字段给出填什么**，让人照抄即可。示例（CNKI 篇名含AI或人工智能、主题含法律或法规、2020-2024 期刊论文）：
1. 打开 https://www.cnki.net → 点检索框右侧「高级检索」
2. 顶部资源类型切到「学术期刊」
3. 时间范围填 2020-01-01 至 2024-12-31
4. 第一行字段选「篇名」，输入 `AI + 人工智能`，匹配方式选「精确」
5. 点「+」加一行，字段选「主题」，输入 `法律 + 法规`
6. 点「检索」，结果列表上方显示的条数即答案，与选项比对
（CNKI 同字段内 + 为「或」、* 为「与」、- 为「非」）

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
    {
        "type": "function",
        "function": {
            "name": "search_and_read",
            "description": (
                "搜索并**自动抓取前几条结果的正文**，一次性拿到多个页面的内容。"
                "当你不确定信息在哪个页面、或上一次只抓到一个空页/无关页时，用这个工具。"
                "比先 search_web 再逐条 fetch_web 更省轮次、更不容易漏掉正确页面。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "count": {
                        "type": "integer",
                        "description": "抓取前几条结果的正文，默认 3，最多 5",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_lookup",
            "description": (
                "查询大赛官方命题范围与样题知识库（含 50 个知识模块的命题要求、"
                "44 道官方样题及正确答案、解析里给出的入口与操作）。"
                "当你不确定某个知识模块考什么、或想参考官方样题的出法与解法时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "关键词，如「截词检索」「CNKI 高级检索」「Zotero」",
                    },
                },
                "required": ["query"],
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
        base_prompt = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        modules = load_module_list()
        self.system_prompt = (
            base_prompt + "\n\n" + modules if modules else base_prompt
        )
        self.max_tokens = cfg.get("max_tokens", 32768)
        self.temperature = cfg.get("temperature", 0)
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
        last_exc = None

        for attempt in range(3):
            content = []
            reasoning = []
            calls = {}
            finish = None
            try:
                with requests.post(
                    url, headers=headers, data=json.dumps(payload),
                    stream=True, timeout=self.timeout,
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
                            entry = calls.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""})
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
                last_exc = None
                break
            except LLMError:
                raise
            except (requests.RequestException, ConnectionError, OSError) as exc:
                last_exc = exc
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(2 + attempt * 3)

        if last_exc is not None:
            raise LLMError("网络中断（已自动重试 3 次）：%s" % last_exc)

        tool_calls = [calls[k] for k in sorted(calls)]
        return "".join(content).strip(), "".join(reasoning).strip(), finish, tool_calls

    @staticmethod
    def _looks_like_guide(text):
        """判断输出是否为「检索指引」而非答案。"""
        if not text:
            return False
        t = text.strip()
        marks = ("检索指引", "操作步骤", "请按以下", "自行核实", "照做",
                 "无法核实", "无法自动", "不能凭记忆", "入口路径", "检索步骤")
        hit = sum(1 for m in marks if m in t)
        if hit == 0:
            return False
        return len(t) > 80 or hit >= 2

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
        if name == "search_and_read":
            q = (args.get("query") or "").strip()
            if not q:
                return "缺少 query"
            try:
                n = int(args.get("count") or 3)
            except (TypeError, ValueError):
                n = 3
            if on_stage:
                on_stage("正在搜索并抓取前 %d 条…" % n)
            got = web_fetch.search_and_read(q, count=n)
            if on_stage:
                on_stage("已获取多页内容 %d 字" % len(got))
            return got
        if name == "knowledge_lookup":
            q = (args.get("query") or "").strip()
            if on_stage:
                on_stage("正在查知识库：%s" % q[:30])
            got = search_knowledge(q)
            return got or "知识库为空。"
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
                tool_used = False
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
                    tool_used = True

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

                # 机械检查：输出的是检索指引，却一次工具都没调用 → 强制核实后重写
                if self._looks_like_guide(final) and not tool_used:
                    if on_stage:
                        on_stage("指引未经核实，正在联网核实…")
                    messages.append({"role": "assistant", "content": final})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "你上面这份指引**一次工具都没有调用**，属于凭记忆写步骤，不可接受。\n"
                                "现在请立即：\n"
                                "1) 用 fetch_web 打开该网站的公开页面（免登录可见的首页/栏目页），"
                                "确认菜单名与按钮名的真实文字；\n"
                                "2) 必要时用 search_web 找官方帮助页或教程；\n"
                                "3) 然后基于**你实际看到的内容**重写指引，菜单名照抄页面文字。\n"
                                "确实无法核实的步骤，必须在该步后标注「（此步未核实，以实际页面为准）」。"
                            ),
                        }
                    )
                    text2, reasoning2, finish2, calls2 = self._stream_once(
                        messages, on_delta, on_reasoning, stop_event, allow_tools=True
                    )
                    for _ in range(self.web_max_rounds):
                        if not calls2:
                            break
                        tool_used = True
                        messages.append(
                            {
                                "role": "assistant",
                                "content": text2 or None,
                                "tool_calls": [
                                    {
                                        "id": c["id"] or ("call_%d" % i),
                                        "type": "function",
                                        "function": {
                                            "name": c["name"],
                                            "arguments": c["arguments"] or "{}",
                                        },
                                    }
                                    for i, c in enumerate(calls2)
                                ],
                            }
                        )
                        for i, c in enumerate(calls2):
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": c["id"] or ("call_%d" % i),
                                    "content": self._run_tool(c, on_stage),
                                }
                            )
                        text2, reasoning2, finish2, calls2 = self._stream_once(
                            messages, on_delta, on_reasoning, stop_event, allow_tools=True
                        )
                    if text2:
                        final = normalize_answer(strip_tool_xml(text2)) or final

                if on_done:
                    on_done(final, reasoning, finish)
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(str(exc))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t
