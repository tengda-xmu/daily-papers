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
        import os
        if os.getenv('WECHAT_USE_SHARED_SNAPSHOT') == '1':
            from src.public_sources import ROOT, read
            from src.models import RawRecord, in_date_window
            payload = read(ROOT/'data/social-articles.json')
            if payload:
                records = [RawRecord(source=self.name, source_id=r['id'], title=r['title'], venue=r.get('account',''),
                    abstract=r.get('summary',''), published_at=r.get('published_at',''), landing_url=r['url'],
                    raw_metadata={'access_mode':'public_index' if r.get('evidence_kind') == 'search_snippet' else 'article', 'social_column':r.get('column')})
                    for r in payload.get('entries',[]) if r.get('platform') == 'wechat' and in_date_window(r.get('published_at'), since, until)]
                statuses = [s for s in payload.get('sources',[]) if s.get('id','').startswith('wechat-')]
                status = 'partial' if not statuses or any(s['status'] not in ('ok','no_data') for s in statuses) else 'ok' if records else 'no_data'
                self._status = SourceStatus(self.name, status, len(records), '复用本轮独立公众号采集结果；覆盖范围见科研线索与 AI 前沿的来源状态。')
                return records
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
