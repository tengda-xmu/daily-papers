"""Use imported WeChat metadata first, then discover public article leads."""
from src.models import SourceStatus
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.wechat_public_index import WeChatPublicIndexAdapter


def shared_snapshot(payload, since, until):
    from src.models import RawRecord, in_date_window, parse_date
    from datetime import timezone, timedelta
    records = [RawRecord(source='微信公众号', source_id=r['id'], title=r['title'], venue=r.get('account',''),
        abstract=r.get('summary',''), published_at=r.get('published_at',''), landing_url=r['url'],
        raw_metadata={'access_mode':'public_index' if r.get('evidence_kind') == 'search_snippet' else 'article', 'social_column':r.get('column')})
        for r in payload.get('entries',[]) if r.get('platform') == 'wechat' and in_date_window(r.get('published_at'), since, until)]
    statuses = [s for s in payload.get('sources',[]) if s.get('id','').startswith('wechat-')]
    state = 'partial' if not statuses or any(s['status'] not in ('ok','no_data') for s in statuses) else 'ok' if records else 'no_data'
    message = '公众号独立采集：'
    checked = parse_date(payload.get('checked_at'))
    if checked:
        message += f"最近检查 {checked.astimezone(timezone(timedelta(hours=8))):%m-%d %H:%M}（北京时间）。"
    message += ' '.join(f"{s.get('name', '微信公众号')}：{s.get('message') or s['status']}" for s in statuses)
    if not statuses:
        message += '缺少来源状态，已有线索保留。'
    return records, SourceStatus('微信公众号', state, len(records), message)


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
            payload = read(ROOT/'data/social-articles.json')
            if payload:
                records, self._status = shared_snapshot(payload, since, until)
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
