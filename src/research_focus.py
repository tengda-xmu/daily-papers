"""Identify language-model and agent methods without matching generic AI terms."""
import re

FOCUS_QUERIES = (
    '"large language model" "fault diagnosis"',
    '"multi-agent" "structural design"',
    '"large language model" "constitutive"',
)


def focus_tags(title, abstract=""):
    text = re.sub(r"[‐‑–—]", "-", f"{title} {abstract}").casefold()
    tags = []
    if re.search(r"\blarge[\s-]+language[\s-]+models?\b|\bllms?\b|大语言模型|大模型", text):
        tags.append("大模型")
    if re.search(r"\bagentic\b|\b(?:multi|two|dual)[\s-]+agents?\b|\b(?:ai|llm)[\s-]+agents?\b|智能体", text):
        tags.append("智能体")
    return tags


def focus_priority(record):
    tags = focus_tags(record.title, record.abstract)
    # Both methods first, then LLMs, then agent-only approaches.
    return 2 * ("大模型" in tags) + ("智能体" in tags)
