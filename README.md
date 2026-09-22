# Daily Papers

面向以下研究方向的每日论文推荐站：

- AI 驱动智能运维
- AI 驱动结构生成式设计与可靠性优化
- AI 驱动结构疲劳与可靠性设计

系统接入 Elsevier/Scopus、Google Scholar、ResearchGate 和微信公众号 RSS，统一去重、打分、摘要，并部署到 GitHub Pages。

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

没有任何密钥时，程序仍会生成网页，并在页面上显示每个来源的 `configuration_missing` 状态。

## 四类来源

### Elsevier / Scopus

使用 Elsevier Search API；`pybliometrics` 已列入依赖，便于后续使用 Scopus/ScienceDirect 的高级接口。配置：

```text
ELSEVIER_API_KEY=
ELSEVIER_INSTTOKEN=
```

可用 `ELSEVIER_QUERIES` 覆盖默认查询，多个查询使用 `||` 分隔。

### Google Scholar

默认通过 SerpApi 的 `google_scholar` 引擎查询，避免在 GitHub Actions 中直接高频访问 Scholar 页面：

```text
SERPAPI_API_KEY=
```

每次运行只执行三个主题查询，并通过 DOI、标题和作者与其他来源去重。

### ResearchGate

ResearchGate 采用本地 Playwright 连接器。首次运行会打开浏览器，手动登录后保存本地会话；不会把 Cookies 提交到仓库。

```powershell
python -m pip install -r connectors/researchgate_sync/requirements.txt
playwright install chromium
python connectors/researchgate_sync/export.py --url https://www.researchgate.net/profile/YOUR_PROFILE
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

工作流每天北京时间 07:00 运行，也支持 `workflow_dispatch` 手动触发。它会生成 `site/`、保存 `data/archive/` 历史数据、部署 GitHub Pages，并发送核心论文摘要。

## GitHub Actions Secrets

在仓库 `Settings → Secrets and variables → Actions` 中按需配置：

```text
ELSEVIER_API_KEY
ELSEVIER_INSTTOKEN
SERPAPI_API_KEY
WECHAT_RSS_URLS
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL
WECHAT_WORK_WEBHOOK_URL
```

ResearchGate 会话目录、API 密钥和个人登录信息不能提交到 Git。缺失或失败的单个来源不会阻塞其他来源更新，页面会显示对应运行状态。

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
