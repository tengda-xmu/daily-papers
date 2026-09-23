"""Use imported WeChat metadata first, then discover public article leads."""
from src.models import SourceStatus
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.wechat_public_index import WeChatPublicIndexAdapter


class WeChatAdapter:
    name = "微信公众号"

    def __init__(self, rss=None, public_index=None):
        self.rss = rss or WeChatRSSAdapter()
        self.public_index = public_index or WeChatPublicIndexAdapter()
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self):
        return self._status

    def fetch(self, since, until):
        rows = self.rss.fetch(since, until)
        if rows and self.rss.status.status == "ok":
            self._status = self.rss.status
            return rows
        indexed = self.public_index.fetch(since, until)
        if rows:
            combined = {row.landing_url: row for row in [*rows, *indexed]}
            self._status = SourceStatus(self.name, "ok" if indexed and self.public_index.status.status == "ok" else "partial",
                len(combined), self.public_index.status.message + f" 合并保留 {len(combined)} 条本期线索。")
            return list(combined.values())
        self._status = self.public_index.status
        return indexed
