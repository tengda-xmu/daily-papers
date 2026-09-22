"""Paper source adapters."""
from .elsevier import ElsevierAdapter
from .google_scholar import GoogleScholarAdapter, SerpApiScholarAdapter
from .researchgate_import import ResearchGateImportAdapter
from .wechat_rss import WeChatRSSAdapter

__all__ = [
    "ElsevierAdapter",
    "GoogleScholarAdapter",
    "SerpApiScholarAdapter",
    "ResearchGateImportAdapter",
    "WeChatRSSAdapter",
]
