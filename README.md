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

ResearchGate 采用本地 Playwright 连接器。首次运行会打开浏览器，手动登录后保存本地会话；不会把 Cookies 提交到仓库。

```powershell
python -m pip install -r connectors/researchgate_sync/requirements.txt
playwright install chromium
python connectors/researchgate_sync/export.py --login --url https://www.researchgate.net/profile/YOUR_PROFILE
python -m tools.publish_researchgate
```

输出文件为 `data/inbox/researchgate.json`。生产环境可把它推送到 `connector-data` 分支，主工作流会在运行前读取该分支。

### 微信公众号

在个人电脑、NAS 或 VPS 上运行 WeRSS，完成扫码和公众号订阅后，将 RSS/Atom/JSON 地址配置为：

```text
WECHAT_RSS_URLS=https://your-werss-host/feed/one.xml,https://your-werss-host/feed/two.xml
```

示例：

```powershell
docker compose -f docker-compose.wechat.yml up -d
```

微信登录会话只保存在 WeRSS 的本地数据目录，不放进 GitHub Actions。

## 摘要与推送

相关性筛选后，推荐顺序为 **CNS 子刊 → CNS 正刊 → 其他相关期刊**；同层级优先考虑大模型和智能体，再比较方向匹配、资料完整性及发表时间。关键词必须同时符合工程方向，通用营销或聊天应用不会因提到大模型而进入推荐。科学机器学习和本构模型等方法论文会在解读中说明迁移到结构研究的条件。

核心最多 5 篇、扩展最多 5 篇，**两个分区都必须具备完整中文解读**，不使用英文摘要或占位说明补足数量。未完成精读的相关论文继续保存在 `data/daily.json` 的 `papers` 候选库。预印本显示版本标识，首次投稿和版本修订日期分开说明。

核心与扩展每篇均提供中文标题、导读与推荐理由，展开“中文精读”可阅读研究问题、方法路线、创新比较、证据发现、局限边界、方向关联和后续研究建议。解读注明原文链接及全文或摘要依据，并区分作者结果与分析建议。

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

arXiv、OpenAlex、Crossref、Semantic Scholar 和 PubMed 可使用公开接口；匿名访问可能受到服务商限流。OpenAlex 支持可选的 `OPENALEX_API_KEY` 与 `OPENALEX_MAILTO`，Semantic Scholar 支持 `SEMANTIC_SCHOLAR_API_KEY`。Web of Science 在 Clarivate 开通 Starter API 后设置 `WOS_API_KEY`，可申请试用或机构方案。Elsevier、Google Scholar、ResearchGate 和微信公众号仍分别需要授权或连接器数据。

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
