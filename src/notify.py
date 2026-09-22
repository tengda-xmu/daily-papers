"""Enterprise WeChat notification for the daily digest."""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from src.settings import load_env


def _markdown(payload: dict, start: int = 0, end: int | None = None) -> str:
    papers = payload.get("core", [])[start:end]
    lines = [
        f"## 每日论文推荐（{len(payload.get('core', []))} 篇核心）",
        f"更新时间：{payload.get('generated_at', '')}",
        "",
    ]
    for index, paper in enumerate(papers, start=start + 1):
        title = paper.get("title", "未命名论文")
        url = paper.get("landing_url") or (
            f"https://doi.org/{paper['doi']}" if paper.get("doi") else ""
        )
        summary = paper.get("summary", "")[:240]
        link = f"[{title}]({url})" if url else title
        lines.extend([
            f"**{index}. {link}**",
            f"来源：{paper.get('source', '')}；主题：{', '.join(paper.get('topic_tags', []))}",
            summary,
            "",
        ])
    return "\n".join(lines)


def send_enterprise_wechat(payload: dict, webhook: str | None = None) -> bool:
    webhook = (webhook or os.getenv("WECHAT_WORK_WEBHOOK_URL", "")).strip()
    if not webhook:
        return False
    papers = payload.get("core", [])
    if not papers:
        text = "## 每日论文推荐\n今日没有通过主题筛选的核心论文。"
        chunks = [text]
    else:
        chunks = []
        for start in range(0, len(papers), 3):
            chunks.append(_markdown(payload, start, start + 3))
    for content in chunks:
        body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode("utf-8")
        request = Request(webhook, data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"Enterprise WeChat returned {result}")
    return True


if __name__ == "__main__":
    from pathlib import Path
    load_env()
    data = json.loads(Path(os.getenv("DAILY_JSON", "data/daily.json")).read_text(encoding="utf-8"))
    sent = send_enterprise_wechat(data)
    print("Enterprise WeChat: sent" if sent else "Enterprise WeChat: skipped (WECHAT_WORK_WEBHOOK_URL not configured)")
