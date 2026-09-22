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

系统还会自动执行 CNS 正刊（Nature、Science、Cell）和相关子刊专项查询；CNS 论文默认使用 30 天回溯窗口，避免因每日短窗口错过近期论文。可通过 `CNS_LOOKBACK_DAYS` 调整。

### Google Scholar

默认通过 SerpApi 的 `google_scholar` 引擎查询，避免在 GitHub Actions 中直接高频访问 Scholar 页面：

```text
SERPAPI_API_KEY=
```

默认每次运行执行三个主题查询和三个 CNS 专项查询，已有缓存时优先复用结果，并与其他来源去重。

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

配置 OpenAI 兼容接口后，核心 5 篇会生成中文精读字段；没有模型密钥时自动退化为摘要规则：

```text
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

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

网页沿用腾达个人主页的深蓝导航、左侧研究方向导航和白底学术排版。首页提供：

- CNS 正刊、CNS 子刊快捷筛选，以及来源、主题、期刊组、具体期刊和全文检索；
- 核心推荐与扩展阅读分区、发表时间排序、精读要点和一键复制引用；
- 来源接入状态、每个来源的本期数量、重点期刊目录和历史归档阅读页；
- 响应式导航、键盘跳转、空状态提示和适合打印的版式。

界面模板与静态资源位于 `tools/templates/` 和 `tools/assets/`，运行 `python tools/build_site.py` 后会复制到 `site/`。

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
