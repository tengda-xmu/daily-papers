# Daily Papers

面向以下研究方向的每日论文推荐站：

- AI 驱动智能运维
- AI 驱动结构生成式设计与可靠性优化
- AI 驱动结构疲劳与可靠性设计

系统接入 Elsevier/Scopus、Google Scholar、ResearchGate 和微信公众号 RSS，另外直接接入 arXiv、OpenAlex、Crossref、Semantic Scholar 和 PubMed 的公开元数据接口，统一去重、打分、摘要，并部署到 GitHub Pages。Web of Science 使用 Clarivate Starter API 适配器，需要申请 API Key；ScienceDirect、Springer Nature、Wiley、IEEE、ACM、ASME、ASCE、AIAA、SAGE、Taylor & Francis、SIAM 等出版平台按期刊和原文链接归类筛选。

## 本地运行

```powershell
$python = "C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe"
& $python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m src.pipeline
python tools/build_site.py
Start-Process site\index.html
```

没有密钥时公开来源仍会运行；需要授权的来源保留明确的缺失状态，不影响其他来源生成日报。

## 四类来源

### Elsevier / Scopus

使用 Elsevier Search API；`pybliometrics` 已列入依赖，便于后续使用 Scopus/ScienceDirect 的高级接口。配置：

```text
ELSEVIER_API_KEY=
ELSEVIER_INSTTOKEN=
```

可用 `ELSEVIER_QUERIES` 覆盖默认查询，多个查询使用 `||` 分隔。

采集先尝试 `COMPLETE` 视图；如果当前授权返回 401/403，则重试官方支持的 `STANDARD` 元数据视图，并在本轮余下查询中复用。网页会注明标准视图，完整摘要等字段仍受机构授权限制。请求按年份约束范围，返回结果再按实际日期筛选；单个来源失败时保留已获取且符合日期要求的记录。

系统还会自动执行 CNS 正刊（Nature、Science、Cell）和相关子刊专项查询；CNS 论文默认使用 180 天回溯窗口，兼顾方向匹配与近期研究，可通过 `CNS_LOOKBACK_DAYS` 调整。常规来源仍检索近 30 天，每日更新不代表每篇论文都在当天发表。

无需 Elsevier 密钥的 `CNS 子刊专项` 适配器通过 Crossref 按 ISSN 单独检索九本相关子刊，包括 npj Artificial Intelligence、Microsystems & Nanoengineering、Nature Communications 等。先执行大模型／智能体专项，再执行常规工程检索，默认共十八次查询。查询词与期刊列表位于 `config/cns-search.json`，单刊失败会保留其他期刊结果。

### Google Scholar

默认通过 SerpApi 的 `google_scholar` 引擎查询，避免在 GitHub Actions 中直接高频访问 Scholar 页面：

```text
SERPAPI_API_KEY=
```

默认每次运行执行三个主题查询和三个 CNS 专项查询，已有缓存时优先复用结果，并与其他来源去重。

Scholar 请求带发表年份范围，缓存同时区分查询与年份。GitHub Actions 会保存并恢复缓存，默认 24 小时内的重复查询复用结果。导出数据与缓存会清除凭据字段，请求错误也不会把含密钥的 URL 写入公开运行状态。

除主题查询外，系统会执行 CNS 正刊和子刊专项查询；网页支持按 `CNS 正刊`、`CNS 子刊` 筛选。

### ResearchGate

ResearchGate 支持两条元数据渠道：优先读取近期成功的本地 Playwright 导出；导出缺失、过期或本期无记录时，使用 SerpApi 的 Google Scholar 公开索引。配置已有的 `SERPAPI_API_KEY` 即可启用公开索引，无需新增密钥，也不要求 GitHub Actions 登录 ResearchGate。

公开索引每天默认增加一次查询，覆盖大模型、智能体和三个研究方向，复用现有 Scholar 的 24 小时缓存。结果仅接受 ResearchGate 域名下的论文链接，将索引中的 PDF 地址转换为论文主页，不下载全文。来源状态明确标记“公开索引已接入”；保留索引来源、检索片段和年份精度，不把未知年份标成今日发表，也不把检索片段替代已核验摘要。该渠道不能保证覆盖 ResearchGate 全部内容，亦不表示本地账号已经登录。设 `RESEARCHGATE_PUBLIC_INDEX=0` 可仅使用本地导出。

如需追踪指定作者主页，仍可使用本地连接器。首次运行会打开独立浏览器，手动登录后保存本地会话；普通 Chrome 的登录状态不会自动共享到连接器的 Edge，会话与 Cookies 不提交到仓库。

```powershell
python -m pip install -r connectors/researchgate_sync/requirements.txt
playwright install chromium
python connectors/researchgate_sync/export.py --login --url https://www.researchgate.net/profile/YOUR_PROFILE
python -m tools.publish_researchgate
```

输出文件为 `data/inbox/researchgate.json`。生产环境可把它推送到 `connector-data` 分支，主工作流会在运行前读取该分支。

### 微信公众号

默认使用公开索引采集公众号文章线索：Windows 每天 06:35 轮换最多 3 次公开检索，
按已订阅账号和近 30 天发布日期筛选，把标题、短片段和检索入口发布到 `connector-data`，
07:00 的 GitHub 日报读取；快照缺失或超过 24 小时则由云端尝试公开检索。
线索在首页“微信公众号 · 科研线索”单独展示，不占核心与扩展论文名额。
WeRSS 继续管理订阅，扫码和会话保留在电脑上。
本机管理地址为 `http://127.0.0.1:8001/`，不必配置公网地址或把扫码会话放进 Actions。
参见 [安装与日常同步](connectors/wechat_sync/README.md)。

另有每天 18:00 的公众号自动发现任务：轮换检索 AI、智能体、大模型、可靠性、航空航天等主题，
每天最多 2 次检索、自动新增 3 个符合科研条件的账号，订阅上限 60 个；规则位于
[`config/wechat_accounts.json`](config/wechat_accounts.json)。新增账号加入后续公开结果的筛选目录；
网站配置页可查看当前订阅目录与发现状态。搜索索引不保证覆盖每个账号的全部文章。

微信后台列表持续返回 `200013`；[上游维护者报告接口关闭](https://github.com/wechat-article/wechat-article-exporter/issues/200)，
不能假定等待一天或重新扫码即可恢复。当前 `article_mode=public_index` 不再调用该受限列表接口。
公开索引使用实际发布日期，链接明确标注为“公开检索入口”，不声称取得原文或核验全文。
请求间隔至少 30 秒，结果缓存 24 小时；遇验证码停用网络请求至少 24 小时，保留成功数据。
Windows 任务需要电脑开机并登录；晚间 WeRSS 账号发现仍依赖有效扫码授权。
本机 `WECHAT_RSS_URLS` 指向文章地址 `/feed/all.json?limit=100`，主仓库另支持
`WECHAT_IMPORT_PATH=data/inbox/wechat.json`。RSS 根目录并不是文章列表。

在个人电脑、NAS 或 VPS 上运行 WeRSS，完成扫码和公众号订阅后，将 RSS/Atom/JSON 地址配置为：

```text
WECHAT_RSS_URLS=https://your-werss-host/feed/one.xml,https://your-werss-host/feed/two.xml
```

示例：

```powershell
docker compose -f docker-compose.wechat.yml up -d
```

微信登录会话只保存在 WeRSS 的本地数据目录，不放进 GitHub Actions。

### Web of Science

在 [Clarivate Starter API 门户](https://developer.clarivate.com/apis/wos-starter) 登录并完成邮箱验证，
进入 Applications 注册应用（例如名称 `Daily Papers`、ID `daily-papers-tengda-xmu`），
再为应用订阅 Web of Science Starter API。Web of Science 网页访问权限不会自动生成 API Key。

官方 Free Trial Plan 每天 50 次请求，不返回被引次数；符合条件的机构成员可申请
Free Institutional Member Plan，每天 5,000 次请求。免费计划与授权以门户当前审批结果为准。

密钥获批后，将其单独保存至项目根目录 `WOS_API_KEY.txt`（已被 Git 忽略），运行：

```powershell
python -m tools.connect_wos --check
python -m tools.connect_wos --activate
```

`--check` 仅执行一次真实检索来验证权限，不保存密钥。`--activate` 验证通过后，
通过 stdin 将密钥保存为 GitHub Actions Secret `WOS_API_KEY`，同时更新本机 `.env`，
并触发 Daily papers。仅需激活时可直接运行第二条命令。工具不打印密钥，也不将密钥放入命令行参数。

每日适配器按三个研究方向执行最多 3 次请求，间隔至少 1.1 秒，通过官方 `publishTimeSpan`
参数筛选发布日期。Starter API 提供基础文献元数据；无被引次数时保留未知值，
不把缺失值当成零，不将元数据当成全文解读。出现 401/403/429 时停止后续请求，保留已成功获得的记录。

## 摘要与推送

相关性筛选后，推荐顺序为 **CNS 子刊 → CNS 正刊 → 其他相关期刊**；同层级优先考虑大模型和智能体，再比较方向匹配、资料完整性及发表时间。关键词必须同时符合工程方向，通用营销或聊天应用不会因提到大模型而进入推荐。科学机器学习和本构模型等方法论文会在解读中说明迁移到结构研究的条件。

核心最多 5 篇、扩展最多 5 篇，**两个分区都必须具备完整中文解读**，不使用英文摘要或占位说明补足数量。未完成精读的相关论文继续保存在 `data/daily.json` 的 `papers` 候选库。预印本显示版本标识，首次投稿和版本修订日期分开说明。

核心与扩展每篇均提供中文标题、导读与推荐理由，展开“中文精读”可阅读研究问题、方法路线、创新比较、证据发现、局限边界、方向关联和后续研究建议。解读注明原文链接及全文或摘要依据，并区分作者结果与分析建议。

标题下方导读用 3–4 句说明具体研究问题、关键方法、主要发现与方向关联，长度控制在 120–220 个字符。定量结果保留比较对象与验证范围，摘要不足时说明证据边界，避免把方法迁移建议写成论文结论。当前人工核验导读优先于模型缓存；自动解读规则更新后使用新的缓存版本。

`data/curated/reading-notes.json` 保存已依据公开论文整理的 10 篇解读，其中 5 篇聚焦大模型或智能体。已有精读（含预印本）按实际首次发表日期进入 180 天推荐窗口，普通来源的新增采集仍使用 30 天窗口。为后续新论文自动生成中文精读，配置 OpenAI 兼容接口：

```text
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

自动解读仅依据可取得的摘要，每次运行最多尝试 `MAX_CORE + MAX_EXTENDED` 篇（默认 10 篇），通过中文与字段完整性检查后存入 `data/analyses/`，两个分区均复用缓存。资料变化会重新生成。未配置模型、响应不合格或生成失败时继续采集论文并保留已完成的精读，新论文保存在候选库；因此未授权模型时，不能保证每天新增 10 篇中文精读。

企业微信机器人配置：

```text
WECHAT_WORK_WEBHOOK_URL=
```

企业微信配置方式：在企业微信群右上角 `…` → `群机器人` → `添加机器人`，复制机器人 Webhook 地址；在 GitHub 仓库 `Settings → Secrets and variables → Actions` 中新建 `WECHAT_WORK_WEBHOOK_URL`。本项目使用群机器人推送，不保存企业微信登录密码，也不需要把企业微信 Cookies 提交到仓库。

工作流每天北京时间 07:00 运行，也支持 `workflow_dispatch` 手动触发。它会生成 `site/`、保存 `data/archive/` 历史数据、部署 GitHub Pages，并发送核心论文摘要。

网页右上角的“手动更新”按钮会打开该 GitHub Actions 工作流页面。登录 GitHub 后点击 `Run workflow`，即可立即执行一轮抓取、生成日报、部署网页和企业微信推送；按钮不在公开网页中保存 GitHub Token。

## GitHub Actions Secrets

在仓库 `Settings → Secrets and variables → Actions` 中按需配置：

```text
ELSEVIER_API_KEY
ELSEVIER_INSTTOKEN
SERPAPI_API_KEY
WOS_API_KEY
OPENALEX_MAILTO
OPENALEX_API_KEY
SEMANTIC_SCHOLAR_API_KEY
ARXIV_CONTACT
WECHAT_RSS_URLS
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL
WECHAT_WORK_WEBHOOK_URL
```

ResearchGate 会话目录、API 密钥和个人登录信息不能提交到 Git。缺失或失败的单个来源不会阻塞其他来源更新，页面会显示对应运行状态。

arXiv、OpenAlex、Crossref、Semantic Scholar 和 PubMed 可使用公开接口；匿名访问可能受到服务商限流。OpenAlex 支持可选的 `OPENALEX_API_KEY` 与 `OPENALEX_MAILTO`，Semantic Scholar 支持 `SEMANTIC_SCHOLAR_API_KEY`。Web of Science 在 Clarivate 开通 Starter API 后设置 `WOS_API_KEY`，可申请试用或机构方案。Elsevier、Google Scholar 仍分别需要 API 授权；微信公众号支持公开索引或连接器数据，ResearchGate 可复用 SerpApi 公开索引或读取本地导出。

网页的[来源配置指南](https://tengda-xmu.github.io/daily-papers/setup.html)提供每个来源的授权入口和步骤，也可在本地安全输入一个 GitHub Secret：

```powershell
python -m tools.configure --set SERPAPI_API_KEY
python -m tools.configure --check
```

`--set` 隐藏输入并通过标准输入交给 GitHub CLI，不写入仓库或日志；`--check` 只报告本地配置是否存在。项目会自动读取被 Git 忽略的 `.env`，已有环境变量优先。GitHub Actions 使用仓库 Secrets，无法反向读取它们的值。

默认检索最近 30 天论文并每日更新，可通过 `LOOKBACK_DAYS` 或 `config/sources.yml` 调整。页面明确显示检索范围；相关性过滤要求工程应用与 AI 方法相结合，合格论文不足 10 篇时保留实际数量。arXiv 查询失败时尝试[官方每日 RSS](https://info.arxiv.org/help/rss.html)，并在来源状态注明该模式；Semantic Scholar 使用[官方批量检索](https://api.semanticscholar.org/api-docs/graph)并限制日期，避免反复调用相关性搜索导致限流。

## 来源与期刊目录

网页的“全部来源”下拉框来自 `config/sources.yml`，“全部期刊组”和“全部具体期刊”来自 `config/venues.yml`。期刊目录按 CNS、Elsevier 工程与材料、Springer Nature、Wiley、IEEE、ACM、ASME、ASCE、AIAA、SAGE、Taylor & Francis、SIAM 等分组；文章若来自多个来源，会同时出现在对应来源筛选中。

## 网页界面

网页沿用腾达个人主页的深蓝导航与白底学术排版，采用居中的单栏阅读布局。首页提供：

- 搜索与 CNS 正刊、CNS 子刊快捷筛选；来源、主题、期刊组和具体期刊收在“筛选”中；
- 核心推荐与扩展阅读分区，默认显示短导读，两区的“中文精读”均可展开七部分分析、原始英文题名、依据与引用；
- 来源接入状态与重点期刊目录默认折叠，点击展开；历史归档保持同样的阅读布局；
- 响应式导航、键盘跳转、空状态提示和适合打印的版式。

界面模板与静态资源位于 `tools/templates/` 和 `tools/assets/`，运行 `python tools/build_site.py` 后会复制到 `site/`。

论文对话顶部仅显示标题、连接状态和“模型与资料”入口，点击后可选模型、上传/获取 PDF、查看资料范围及管理浏览器配对。对话阅读区占用主要空间，仍可拖动调节侧栏宽度与阅读区高度，输入区单独滚动。

在“中英翻译 → 全文翻译”完成后，阅读区下方常驻的 **导出译文 PDF** 以及译文末尾的同名按钮可下载原文与译文对照文件，保留资料页码标记。导出锁定对应全文翻译，不会混入后续提问；历史译文重新打开后仍可导出，中断或失败的翻译明确标为“部分译文”。“对话 PDF / 对话文本”用于导出完整会话；所有导出均使用本机已保存内容，不重新调用模型。

核心推荐按 DOI 匹配 `data/curated/figures.json` 中已核验的代表图，每篇只展示一张，并附独立编写的中文图解、作者、原文图页和许可链接。点击可打开大图，Esc 关闭，“查看原尺寸”可在新窗口查看图片。图片保存在 `tools/assets/figures/`，随站点部署；历史归档使用相同目录。无已核验配图的论文继续显示导读和原文链接。

增加配图时，应先阅读论文图注，选择真正描述框架、原理或流程的图片，核对文章许可及图中第三方署名，并在目录记录来源、尺寸和 SHA-256。当前配图来自出版方公开图片，文件保持原样；遵循各图标注的 CC BY 或 CC BY-NC-ND 许可，用于本学术网站的非商业阅读介绍。未来新论文需补充已核验的配图记录，程序不根据题名猜图，也不把原图重新绘制成示意图。

仅修改网页时，可手动运行 `Publish website` 工作流，使用已有论文数据生成并部署页面。每日采集和企业微信推送继续由 `Daily papers` 工作流负责。

## 发布到 GitHub

先在 `tengda-xmu` 账号下创建空仓库 `daily-papers`，然后在本地执行：

```powershell
git remote add origin https://github.com/tengda-xmu/daily-papers.git
git branch -M main
git push -u origin main
```

在仓库 `Settings → Pages` 中将发布方式设为 `GitHub Actions`。第一次工作流运行后，Pages 会显示实际访问地址。

## 目录

```text
src/                         抓取、标准化、去重、排序和摘要
config/                      来源与主题配置
connectors/researchgate_sync ResearchGate 本地连接器
data/                        每日数据与历史归档
tools/build_site.py          静态网页生成器
.github/workflows/daily.yml  每日抓取、部署和推送
tests/                       解析、去重和降级测试
```
## 手动检索文献

首页和导航中的 **手动检索** 打开 `search.html`，独立查询现有 11 类来源，支持关键词、日期和来源选择，每来源返回最多 5 / 10 / 25 条，跨来源合并 DOI、arXiv 标识及标题作者相同的论文。它不会更改每日推荐或触发企业微信推送。

**检索排序**支持相关性优先、最新发表、热度（被引次数），浏览器会记住选择。修改上方排序后点击“检索文献”获取新结果；结果区的“当前结果重排”只调整已返回的列表，不调用接口，引用导出沿用当前列表顺序。

- **相关性优先**：结合来源返回顺序、标题与摘要的关键词匹配进行合并。对日期或热度检索结果再选择相关性重排时，只比较关键词匹配，不将被引排名视为相关性。
- **最新发表**：日期降序，日期缺失的记录置后。仅有年份的记录按该年排序，不补造精确日期；各库的出版、上线与预印本日期可能不同。
- **热度（被引次数）**：被引次数降序，结果显示数值，悬停可查看来源。合并记录取已返回来源的最高计数，不相加；不同库统计口径不同。缺失计数排在已知计数（包括 0 次）之后，不代表未被引用，也不等同公众号阅读量或论文质量。

Crossref/CNS、Scopus、OpenAlex、Semantic Scholar 会将三类排序传给原始检索接口；arXiv、PubMed 支持相关性与日期排序，未提供被引计数。Google Scholar、ResearchGate、微信公众号及当前 Web of Science 接入在已取得的候选内重排，页面“来源检索状态”注明范围，不表示全库排名。各排序分别缓存 24 小时，切换检索条件可能产生新的来源 API 请求。

先运行 **启动论文助手.cmd**。页面复用论文对话的浏览器配对；检索只调用文献来源，不启动 Codex 模型、不消耗对话额度。API 密钥从本机忽略的 `.env` 读取，SerpApi 等来源按账号额度计费。公网网页被浏览器限制访问回环地址时，可用 [本机检索页](http://127.0.0.1:43127/search.html)。

- 同时最多运行 4 个来源，每个来源 50 秒超时后终止独立进程；结果逐步显示，可以停止并保留已返回的结果。成功检索缓存 24 小时；重新提交同样查询时只对未成功的来源重试。检索中重复提交同一请求不会重复调用接口。
- CNS 子刊专项对 `config/cns-search.json` 的 9 本期刊使用合并 ISSN 查询。ResearchGate 查询 SerpApi 公开索引并合并本机导出。微信公众号合并订阅 RSS 与本机历史导出后匹配关键词；不足所选数量时，查询所有公开索引账号，不受日报订阅白名单限制，仍按所选日期过滤。公开索引只返回一页线索，不保证达到所选上限，也不代表公众号全量历史；无已核验原文时提供搜狗微信检索入口。
- 微信公众号继续共享既有公开索引预算（每日最多 3 次新查询）及验证码冷却，24 小时内复用原始查询缓存；页面显示日期筛选后的条数、限额或访问受限等具体原因，并提供在搜狗微信继续检索的入口。修复前因订阅白名单产生的空结果缓存自动失效；其他付费来源缓存保留。arXiv 手动查询失败时显示失败，不用无关的每日 RSS 充当查询结果。
- Elsevier / Scopus 手动检索使用 `STANDARD` 标准元数据视图，按所选相关性、日期或被引次数检索。API 的 `date` 参数只能限定年份，程序再按 Scopus 期刊日期精确过滤；不足所选数量时继续翻页，最多 3 页（每页 25 条）、一次临时网络错误重试。最新排序的前两页若被未来期刊日期占据、有效候选不足，最后一页改用相关性补充并按日期重排，状态明确标记为部分结果及“不是全库最新排名”。达到上限、授权不足或请求中断时显示具体原因，不将尚未扫描的结果说成“全库无结果”；修复前由首屏未来期刊日期造成的空缓存自动失效。Scopus 收录多个出版社，结果不限 Elsevier 旗下期刊；标准视图的作者与摘要可能不完整。
- 每条结果提供原文链接；有可读取的公开 PDF 时支持下载。受机构订阅限制或自动访问受限时，保留原文/全文入口，不承诺所有论文都能自动下载。PDF 获取复用本机助手的 HTTPS 主机白名单、逐跳重定向校验和 20 MB / 300 页限制。
- 支持复制单条、导出当前筛选结果的 **GB/T 7714-2025** 引用（UTF-8 文本）。实现期刊文章、会议论文、预印本与网页的常用著录格式；只有提供结构化姓名时才生成缩写，未知作者、出版年、文献类型、卷期页码会提示核对，不生成缺失字段。此工具不代替投稿前的文献管理软件核对。

标准版本依据：[全国标准信息公共服务平台](https://std.samr.gov.cn/gb/search/gbDetailedCNF?id=4507EFE13D37CB6AE06397BE0A0A601F)（2025 版于 2026-07-01 实施，代替 2015 版）。检索缓存及 PDF 保存在 `.local/codex-bridge/manual-search/`，不随 GitHub 网站发布。重启后重新提交相同查询会复用缓存。

## 手动添加期刊

展开首页 **重点期刊目录 → 添加期刊 / 管理期刊**，或打开 [管理页](https://tengda-xmu.github.io/daily-papers/journals.html)。运行本机论文助手后，页面复用已有浏览器配对，无需模型对话。

1. 输入纸质版或电子版 ISSN，点击 **验证期刊**。Crossref 返回正式刊名，可调整显示名称，并选择已有分组或创建新分组。
2. 点击 **保存到本机**。支持编辑、暂停、启用和移除；同一期刊的纸质版与电子版 ISSN 会去重。保存后可用 **检索此刊** 按 ISSN 查询，关键词留空时浏览指定日期范围内的新论文；结果沿用 PDF、原文及 GB/T 7714 引用功能。
3. 点击 **同步 GitHub**。通过本机已登录的 GitHub CLI，只更新仓库 `main` 的 `config/custom-journals.json`。线上目录自动部署，下次每日任务纳入定向检索；本次同步不立即发送企业微信日报。

最多管理 25 本自定义期刊，需要 Crossref 收录其 ISSN。每个启用期刊每日增加一次 Crossref 查询（上限 25 条，沿用日报时间范围与研究方向排序），计入现有 Crossref 来源。刊名与目录分组不等于 API 来源；来源总数仍为 11。未匹配研究方向、无新文章或评分未入选时，不保证进入核心或扩展推荐。

本机待同步配置在忽略的 `.local/journals/state.json`；首次使用以公开配置为基准。同步采用三方合并和 GitHub 文件 SHA 校验，保留远程独立新增；同一期刊并发修改时保留本机数据并提示冲突。只有认证并配对的本站/本机页面能修改；公开配置不包含令牌、密钥或会话。退出/重启助手不会丢失期刊列表；公共网站编辑仍需本机助手运行。

接口依据：[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、[GitHub Contents API](https://docs.github.com/en/rest/repos/contents)。

## 本机 Codex 论文对话

双击根目录的 **启动论文助手.cmd**，即可连接本机已经登录的 Codex。网页每篇论文新增 **Codex 对话**，支持中文总结、追问、翻译、PDF 与配图解释；首次使用在线侧栏需要从本机页面复制配对码，默认记住当前浏览器，后续打开网页或重启助手会自动恢复连接，也可随时取消记住。电脑需要保持运行，使用现有 Codex 账号额度。详见 [连接器说明](connectors/codex_bridge/README.md)。
