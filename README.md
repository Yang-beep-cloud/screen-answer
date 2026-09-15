# 屏幕答题助手 (Screen Answer)

点按钮或按快捷键 → 自动截取主屏幕 → 大模型看图答题 → 右下角气泡显示答案，公式自动排版成图片。

## 用法

双击 `start.bat`（首次会自动建虚拟环境并装依赖）。

启动后屏幕右下角有一个蓝色 **答题** 按钮（置顶）：

- 点按钮，或按 `Ctrl + Alt + Q`：截图并答题。
- 截图瞬间会自动隐藏按钮和气泡，避免把界面自己拍进去。
- 按住按钮拖动可换位置（拖动不会误触答题）。
- 答题中按钮变灰 `思考中…`，状态栏显示模型实时思考进度。
- 再按 `Ctrl + Alt + Q`：隐藏气泡。
- 托盘图标右键：`答题` / `显示/隐藏按钮` / `隐藏气泡` / `退出`。

出错信息写入 `error.log`。

> `deepseek-flash` 是推理模型，会先思考再作答，通常需要十几秒；状态栏的 `思考中…` 会实时显示思考内容，属正常现象。
>
> **重要**：推理模型的思考也计入 `max_tokens`。上限太小时思考会被截断（`finish_reason=length`），导致答案为空。默认给到 `32768`；若仍出现空答案，看 `error.log` 里的 `finish=length` 再继续调大。

## 输出格式

- **单选题** → 只给答案，例如 `A. 8`
- **多选题** → 只给字母，例如 `ABC`
- **填空题** → 只给填空内容
- **问答题/计算题** → 完整解题过程
- **没有题目** → `未检测到题目。`
- 多道题 → 按题号逐题作答

提示词里明确要求模型「先完整准确读题 → 逐项推理 → 得出结论后验算/回代检查」，
但思考过程只在内部进行，不写进答案，所以既能保证准确率又保持输出简洁。

若想自定义风格，在 `config.json` 加 `system_prompt` 字段即可覆盖默认提示词。

## 联网查证（工具调用）

程序给模型挂了两个**真正的 function calling 工具**（不是靠提示词拼 JSON）：

| 工具 | 作用 |
| --- | --- |
| `search_web` | 搜索引擎检索，返回标题/网址/摘要，用于定位信息源 |
| `fetch_web` | 抓取指定网址正文，可按关键词筛出相关段落 |

模型可**多轮**调用（`web_max_rounds`，默认 6 轮）。轮数用尽时会**禁用工具强制作答**，
避免模型一直检索却给不出答案。

这样设计是为了应对「实操验证题」——例如：

> 在联合国教科文组织官网找到《学生人工智能能力框架》，正文**第 29 页**表格的标题是（）
> 在 CNKI 检索 2015-2021 年篇名含「信息素养」的论文，**被引次数最高**的 CSSCI 论文发表在哪个期刊

这类题必须真的去查，凭记忆答必错。

### 遇到需要登录/付费的库（CNKI、万方、维普…）怎么办

模型**不会编造答案，也不会只回「无法核实」**，而是输出一份**可直接照做的检索指引**，包含七部分：

1. **用哪个库** — 并给出免费替代（国家哲学社会科学文献中心 ncpssd.org、机构图书馆入口等）
2. **入口路径** — 具体到点哪里，如「CNKI 首页 → 高级检索」
3. **检索式** — 哪个字段 + 什么关键词 + 逻辑，如「篇名 = 信息素养 AND 出版年度 = 2015-2021」
4. **筛选条件** — 文献类型（学术期刊）、来源类别（CSSCI/北大核心/CSCD）、学科、时间
5. **排序方式** — 如把「相关度」改成「被引」，并说明 CNKI 叫「按被引排序」、万方叫「按被引次数排序」
6. **看哪个字段对答案** — 如「结果第 1 条的『来源』列（万方叫『刊名』）就是期刊名，与选项比对」
7. **结果不理想时的微调** — 如筛选后条目过少该怎么放宽

工具轮数用尽时会**禁用工具并追加提示**，强制模型要么作答、要么给出指引，
避免它一直检索却给不出结果。

## 本仓库的 `data/` 目录

- `命题范围_全文.txt` — 大赛官方 50 个知识模块的命题范围全文（含每模块的样题与答案解析）
- `样题_44题.json` — 从命题范围中提取的 44 道官方样题（含正确答案），可作评测集

> 这些是大赛官网公开的「命题范围」内容，用于让提示词贴合真实题型，**不包含任何硬编码答案**。

## 公式渲染

答案里的 `$...$`（行内）和 `$$...$$`（独立）会被 **真 LaTeX（MiKTeX）** 编译成图片贴进气泡，不会显示 LaTeX 源码。

- 编译结果缓存在 `cache/tex/`，同一公式只编译一次。
- 首次编译某个公式需初始化 MiKTeX 宏包（可能十几秒），之后约 0.4 秒。
- 若公式编译失败，会退回显示原始文本，不会崩。

## 网页抓取

题目里出现网址时，程序会自动抓取并按需作答：

1. 模型看截图，判断是否需要访问网页；
2. 需要则输出 `{"fetch": ["https://..."], "query": "关键词"}`；
3. 程序用 **trafilatura** 抽取网页正文（去掉导航/侧栏/广告/页脚），再用 `query` 做相关段落筛选；
4. 把内容和截图一起发给模型作答；
5. 信息不够时模型可再输出一次 `fetch`，最多 `web_max_rounds` 轮，直到信息足够。

实测抽取效果（同样页面）：

| 页面 | 旧的正则剥离 | trafilatura |
| --- | --- | --- |
| runoob Python3 教程 | 6095 字 | **827 字** |
| docs.python.org 教程 | 6986 字 | **2300 字** |

单页上限 `web_max_chars`（默认 40000）、全部累计上限 `web_total_chars`（默认 120000）。

> 网络说明：程序直接用系统网络访问网页。若某些站点在你网络下打不开（例如维基百科），抓取会失败并在答案里说明；需要代理时可设置 `HTTPS_PROXY` 环境变量。

## 后端

`config.json` 的 `backend` 决定用哪个后端：

### `"api"`（默认，推荐）

直连 OpenAI 兼容接口，快、稳。

> 官方 DeepSeek 只有 `deepseek-flash` 支持图片；`deepseek-v4-pro` 实测收不到截图（会回「未检测到题目」）。

### `"opencode"`（可选）

接入 opencode 服务端，能用它的 agent 工具（webfetch/websearch），但**实测慢很多**。

程序会自动 `opencode serve` 并复用 `OPENCODE_SERVER_PASSWORD` 做 basic auth，会话期间只开 `webfetch`/`websearch`，其余工具（bash/read/write 等）全部关闭，避免动到你的文件。

> 实测注意：opencode 里 `deepseek` 官方 provider（`@ai-sdk/deepseek`）会丢弃图片，模型收不到截图；要用 USTC 等支持视觉的模型。另外实测一次作答约 200-350 秒，明显慢于直连 API。

## 配置 `config.json`

| 字段 | 说明 |
| --- | --- |
| `backend` | `api` 或 `opencode` |
| `base_url` | 接口地址，默认 `https://api.deepseek.com` |
| `api_key_env` | 存放 Key 的环境变量名 |
| `api_key_provider` | 从 opencode auth.json 读哪个 provider 的 Key |
| `model` | 模型名，默认 `deepseek-flash` |
| `hotkey` / `hotkey_label` | 快捷键及其显示文字 |
| `enable_web_fetch` | 是否允许联网查证（工具调用开关） |
| `web_max_chars` | 单个网页保留的最大字数，默认 `40000` |
| `web_total_chars` | 所有网页累计最大字数，默认 `120000` |
| `web_max_rounds` | 最多几轮工具调用，默认 `6` |
| `web_max_urls` | 搜索/抓取每次最多处理几条，默认 `6` |
| `opencode.port` | opencode 服务端口 |
| `opencode.provider` / `opencode.model` | opencode 使用的模型（需支持视觉） |
| `opencode.enable_web` | opencode 是否开启 webfetch/websearch |
| `max_tokens` | 推理模型会先思考，太小会导致思考被截断、答案为空，默认 `32768` |
| `temperature` | 默认 `0.2` |
| `capture_scale` | 截图缩放，卡顿时调小如 `0.7` |
| `capture_delay_ms` | 隐藏自身界面后等待多少毫秒再截图 |
| `timeout` | 请求超时秒数 |

### API Key

按顺序查找：环境变量 → `~/.local/share/opencode/auth.json` 里 `api_key_provider` 对应的 key。

## 依赖

除 Python 包外，公式渲染需要 **MiKTeX**（提供 `pdflatex`）：

```powershell
winget install MiKTeX.MiKTeX
```

程序会自动在常见路径和 PATH 中查找 `pdflatex`。装不上也不影响答题，只是公式以文本显示。

## 手动运行

```powershell
cd screen-answer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## 文件

- `app.py` — 主程序：快捷键、托盘、主线程任务队列、后端调度
- `capture.py` — 屏幕截图
- `llm_client.py` — 直连 API：多轮问答 + 网页抓取编排
- `web_fetch.py` — 网址提取、trafilatura 正文抽取、相关段落筛选
- `latex_render.py` — MiKTeX 编译 + 光栅化成 PNG（带缓存）
- `bubble.py` — 答案气泡（文本 + 公式图片混排）
- `button.py` — 悬浮答题按钮
- `opencode_backend.py` — 可选 opencode 服务端后端
- `config.json` — 配置
