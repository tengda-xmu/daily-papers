# 微信公众号本机同步

本机运行 WeRSS，微信扫码会话只留在电脑；每日把文章元数据和采集状态同步到
`connector-data`，GitHub Actions 07:00 读取。无需把本机服务暴露到公网。

当前 Windows 安装使用 `wufulin/wechat-mp-rss` 的
`4da70d790a44af8f3bcc72a76b7a141bd1715a1f`，独立 Python 环境与前端构建存放在
被忽略的 `.local/`。运行目录是 `.local/werss-source`，管理地址
<http://127.0.0.1:8001/>。私人管理账号位于该目录的 `.env`，不要提交或分享。
授权是在微信公众平台上进行；需要手机完成平台要求的身份验证。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/start_wechat.ps1
# 在 WeRSS 管理页面扫码、添加公众号后，手动采集并同步：
powershell -NoProfile -ExecutionPolicy Bypass -File tools/sync_wechat.ps1
# 创建每日 06:35 任务：
powershell -NoProfile -ExecutionPolicy Bypass -File tools/schedule_wechat.ps1 -Enable
```

任务运行时电脑需要开机并登录 Windows；脚本会先启动本机服务，使用已保存的授权。
本机安装开启 WeRSS 的已授权会话恢复，失效时仍需要重新扫码。
默认仅抓取各订阅的第一页，关闭正文采集、全文 RSS、图片转存与其他通知任务。
检索与推荐相关性由主项目的三个研究方向过滤。

`tools/werss_compat.py` 针对固定版本做小范围兼容修正：微信 `200013` 限频错误
向上返回、采集请求验证 TLS、请求前至少间隔 30 秒、接口等待当前页面采集完成。
另修复重启恢复已授权会话后登录状态标记未更新的问题，仅在账户验证成功后更新。
遇到限频立即终止剩余订阅，不轮换网络或连续重试。下次每日任务再尝试。
升级 WeRSS 时需要先复核这些补丁。

## 自动发现与扩展订阅

`config/wechat_accounts.json` 管理领域关键词、优先账号、排除词和数量预算。
注册任务后，`DailyPapers-WeChat-Discovery` 每天北京时间 18:00 自动检索新账号：

- 每日最多 2 次检索、3 个新订阅，检索间隔至少 30 秒，缓存 14 天；当前上限 60 个订阅。
- 搜索词持续轮换，覆盖人工智能、智能体、大模型、可靠性、航空航天、结构材料、疲劳和健康监测。
- 同一关键词的前 3 页轮换（每页 5 个账号），不会一直只查看第一页；每次实际请求均占用当天预算。
- 优先匹配已列出的科研账号；其他账号需要名称和简介同时满足领域与科研特征。
- 排除营销、招聘、中小学教育和会员事务账号；同名、证据不足的账号留在本地候选记录，不自动加入。
- 按微信账号 ID 去重，保留候选供后续运行；不会自动删除原有订阅。达到上限后需调整配置。
- 遇到搜索限频停止，至少等待 24 小时；授权失效记录状态，等待重新扫码。

`discover.py --bootstrap` 仅供首次扩展，每批最多 10 次检索和 20 个新增账号。
日常手动触发仍受冷却时间和常规预算控制：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/discover_wechat.ps1
# 重新注册/启用早间同步及晚间发现两个任务：
powershell -NoProfile -ExecutionPolicy Bypass -File tools/schedule_wechat.ps1 -Enable
```

本地缓存和筛选原因在 `.local/wechat-discovery.json`。公开目录
`data/inbox/wechat-subscriptions.json` 只同步公众号名称、微信号、领域标签、添加时间和发现状态。
网站的“微信公众号”配置区可折叠查看当前目录。账号身份来自微信搜索；自动规则并不验证文章结论。
《航空学报》的官方微信入口可见[期刊官网](https://hkxb.buaa.edu.cn/)，当前搜索名称为“航空学报CJA”。

新增订阅通过本机 WeRSS 的 `fetch_articles=false` 扩展参数延迟首次采集，避免批量建号
立即触发文章请求。每日 06:35 最多轮换更新 12 个账号，优先处理最久未尝试的订阅；
07:00 云端日报读取最新已导出的内容。晚间新增的订阅目录在下次网站更新后可见。

`data/inbox/wechat.json` 仅允许标题、公众号名、短摘要、发布日期与微信原文链接，
`wechat-status.json` 仅允许采集时间、状态、订阅名称及是否授权。
无文章或采集失败时保留之前的数据；状态文件可以单独更新，避免把失败显示成成功。
原文长链接只保留 `__biz`、`mid`、`idx`、`sn`，剔除登录和跟踪参数。

```powershell
# 仅导出 WeRSS 已经收集的近 30 天文章：
python connectors/wechat_sync/export.py
python -m tools.publish_wechat
# 仅发布一次实际采集状态，保留原来的文章快照：
python -m tools.publish_wechat --status-only
```

微信公众号属于科研线索来源，不能因订阅号声称某结论就自动视作原论文结论。
未核验论文与缺少中文精读的条目保留为候选，不占用核心或扩展精读名额。
