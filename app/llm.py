import json
import re
import time
from html import unescape
from typing import Any

import httpx

from .config import Settings


class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.allow_llm
            and self.settings.llm_api_key
            and self.settings.llm_base_url
            and self.settings.llm_model
        )

    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        result, _ = await self.chat_json_with_usage(system, user)
        return result

    async def chat_json_with_usage(self, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("LLM is not configured")
        data, _ = await self._chat(system, user)
        content = data["choices"][0]["message"]["content"]
        return parse_json_object(content), data.get("usage") or {}

    async def test_connection(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "message": "LLM config is incomplete",
                "latency_ms": None,
                "model": self.settings.llm_model,
            }
        started = time.perf_counter()
        try:
            data, status_code = await self._chat(
                "You are a health check endpoint. Return only a compact JSON object.",
                '{"ok":true,"message":"pong","provider_ready":true}',
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            message = data["choices"][0]["message"]["content"]
            parsed = parse_json_object(message)
            usage = data.get("usage") or {}
            return {
                "ok": True,
                "message": str(parsed.get("message") or "pong"),
                "latency_ms": latency_ms,
                "model": self.settings.llm_model,
                "status_code": status_code,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": latency_ms,
                "model": self.settings.llm_model,
            }

    async def _chat(self, system: str, user: str) -> tuple[dict[str, Any], int]:
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.llm_base_url}/chat/completions"
        async with httpx.AsyncClient(
            timeout=self.settings.llm_timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400 and "response_format" in response.text:
                payload.pop("response_format", None)
                response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json(), response.status_code


def parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"</p\s*>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def article_paragraphs(article: dict[str, Any]) -> list[str]:
    html = str(article.get("content_html") or "")
    if html:
        matches = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.S | re.I)
        paragraphs = [strip_html(item) for item in matches]
        return [item for item in paragraphs if len(item) > 18]
    content = strip_html(str(article.get("content") or ""))
    raw_parts = re.split(r"(?<=[。！？!?；;])\s+|\n{2,}", content)
    return [part.strip() for part in raw_parts if len(part.strip()) > 18]


def article_text_blocks(article: dict[str, Any], max_blocks: int = 24) -> list[str]:
    paragraphs = article_paragraphs(article)
    if paragraphs:
        return paragraphs[:max_blocks]
    content = strip_html(str(article.get("content") or article.get("content_html") or ""))
    lines = [line.strip() for line in content.splitlines() if len(line.strip()) > 18]
    return lines[:max_blocks]


def article_outline(article: dict[str, Any], max_chars: int) -> str:
    title = str(article.get("title") or "")
    description = str(article.get("description") or "")
    text = strip_html(str(article.get("content") or article.get("content_html") or ""))
    paragraphs = article_text_blocks(article, max_blocks=18)
    parts: list[str] = [
        f"公众号：{article.get('mp_name') or ''}",
        f"标题：{title}",
        f"摘要：{description}",
        f"正文可用：{'是' if bool(text) else '否'}",
    ]
    if paragraphs:
        parts.append("正文要点片段：")
        for index, paragraph in enumerate(paragraphs, 1):
            parts.append(f"{index}. {paragraph[:260]}")
    if text and len("\n".join(parts)) < max_chars:
        remaining = max_chars - len("\n".join(parts))
        if remaining > 200:
            parts.append("正文补充片段：")
            parts.append(text[:remaining])
    return "\n".join(parts)[:max_chars]


def heuristic_article_summary(article: dict[str, Any]) -> dict[str, Any]:
    title = str(article.get("title") or "")
    description = str(article.get("description") or "")
    content = strip_html(str(article.get("content") or article.get("content_html") or ""))
    paragraphs = article_paragraphs(article)
    tags = keyword_tags(" ".join([title, description, content[:1600]]))
    value_score = heuristic_score(title, description, content, tags)

    lead = paragraphs[0] if paragraphs else description or title
    evidence = build_heuristic_points(paragraphs, title, description)
    one_sentence = build_heuristic_one_sentence(title, description, lead, tags)
    takeaways = [point[:120] for point in evidence[:3]]

    return {
        "one_sentence": one_sentence,
        "thesis": lead[:180],
        "key_points": evidence[:5],
        "takeaways": takeaways,
        "why_read": infer_why_read(tags, value_score, content, paragraphs),
        "recommended_for": infer_audience(tags, value_score),
        "tags": tags[:6],
        "value_score": value_score,
        "difficulty": infer_difficulty(content, tags),
        "confidence": "low" if not content else "medium",
        "limited_evidence": not bool(content),
        "reading_time_minutes": estimate_reading_time(content),
        "method": "heuristic",
    }


def heuristic_profile(account: dict[str, Any], articles: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join(
        [str(account.get("mp_name") or ""), str(account.get("mp_intro") or "")]
        + [str(item.get("title") or "") for item in articles[:30]]
        + [str((item.get("summary") or {}).get("one_sentence") or "") for item in articles[:15]]
    )
    tags = keyword_tags(text)
    scored = [float(item.get("value_score") or 0) for item in articles if item.get("value_score")]
    avg_score = round(sum(scored) / len(scored), 1) if scored else heuristic_score(text, "", "", tags)
    has_depth = sum(1 for item in articles if int(item.get("content_chars") or 0) > 3000)
    capability = infer_capability(tags, avg_score, has_depth)
    strengths = infer_strengths(tags)
    weaknesses = infer_weaknesses(tags, articles)
    sample_titles = [str(item.get("title") or "") for item in articles[:6]]
    return {
        "capability_judgment": capability,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "tags": tags[:6],
        "score": avg_score,
        "confidence": "high" if len(articles) >= 10 else "medium",
        "style": infer_style(tags, sample_titles),
        "best_use_cases": infer_use_cases(tags),
        "method": "heuristic",
    }


def build_heuristic_one_sentence(title: str, description: str, lead: str, tags: list[str]) -> str:
    if title and description:
        return f"这篇文章围绕“{title}”展开，核心在于：{description[:96]}"
    focus = "、".join(tags[:3]) if tags else "主题判断"
    if lead:
        return f"这篇文章主要讨论{focus}，重点信息落在：{lead[:110]}"
    return f"这是一篇关于{focus}的短文，目前更适合作为线索输入而不是完整研究结论。"


def build_heuristic_points(paragraphs: list[str], title: str, description: str) -> list[str]:
    points: list[str] = []
    for part in paragraphs[:10]:
        compact = re.sub(r"\s+", " ", part).strip()
        if len(compact) < 24 or compact in points:
            continue
        points.append(compact[:150])
        if len(points) >= 5:
            break
    if not points and description:
        points.append(description[:150])
    if not points:
        points.append(title[:150])
    return points


def keyword_tags(text: str) -> list[str]:
    tags: list[str] = []
    rules = [
        ("宏观", ["宏观", "美元", "美债", "联储", "利率", "通胀", "财政", "流动性"]),
        ("固收", ["固收", "债券", "信用", "城投", "转债", "reits"]),
        ("A股", ["a股", "大盘", "指数", "板块", "个股", "复盘"]),
        ("商品", ["商品", "期货", "原油", "pta", "px", "eg", "煤", "铜", "黄金"]),
        ("科技", ["ai", "光模块", "通信", "算力", "cpo", "tfln", "半导体"]),
        ("医药", ["医药", "创新药", "临床", "医保", "药企"]),
        ("日本", ["日本", "日元", "东京", "央行", "加息"]),
        ("交易", ["交易", "择时", "策略", "仓位", "技术", "趋势"]),
        ("纪要", ["纪要", "调研", "电话会", "会议"]),
        ("深度", ["框架", "深度", "模型", "测算", "数据"]),
    ]
    lower_text = text.lower()
    for tag, words in rules:
        if any(word.lower() in lower_text for word in words):
            tags.append(tag)
    return tags or ["综合"]


def heuristic_score(title: str, description: str, content: str, tags: list[str]) -> float:
    score = 5.6
    length = len(content)
    if length > 15000:
        score += 1.2
    elif length > 7000:
        score += 0.9
    elif length > 2500:
        score += 0.5
    elif length > 800:
        score += 0.2
    if "深度" in tags:
        score += 0.8
    if "纪要" in tags:
        score += 0.5
    if any(tag in tags for tag in ["宏观", "固收", "商品", "科技", "医药"]):
        score += 0.4
    if not content:
        score -= 1.4
    hype_words = ["重磅", "必看", "疯涨", "暴涨", "惊呆"]
    if any(word in title + description for word in hype_words):
        score -= 0.3
    return max(1.0, min(10.0, round(score, 1)))


def infer_why_read(tags: list[str], score: float, content: str, paragraphs: list[str]) -> str:
    if score >= 8:
        return "信息密度和判断价值都比较高，适合进入优先阅读队列。"
    if "纪要" in tags:
        return "更适合作为一手交流线索，阅读时最好结合原始数据和后续验证。"
    if "交易" in tags:
        return "适合快速观察市场节奏与情绪变化，但不宜直接替代独立判断。"
    if not content:
        return "当前只有标题或摘要，可用证据不足，建议等全文补抓后再看。"
    if len(paragraphs) <= 2:
        return "篇幅偏短，更像快评或观点摘录，适合快速浏览。"
    return "适合作为主题跟踪材料阅读，重点看作者的论据、数据和结论。"


def infer_difficulty(content: str, tags: list[str]) -> str:
    if len(content) > 9000 or "深度" in tags or "宏观" in tags:
        return "high"
    if len(content) > 2500:
        return "medium"
    return "low"


def infer_audience(tags: list[str], score: float) -> str:
    if "宏观" in tags or "固收" in tags:
        return "研究员、投资经理、需要跨资产框架的人"
    if "商品" in tags or "交易" in tags:
        return "交易员、商品研究员、关注市场节奏的人"
    if "科技" in tags or "医药" in tags:
        return "行业研究员、主题投资者"
    if score >= 8:
        return "适合做重点深读"
    return "适合做快速主题扫描"


def infer_capability(tags: list[str], score: float, has_depth: int) -> str:
    if "固收" in tags or "宏观" in tags:
        return "偏研究框架型作者，宏观、利率和信用链条分析能力更突出。"
    if "商品" in tags:
        return "偏产业和交易结合型作者，对供需、库存和价格节奏比较敏感。"
    if "科技" in tags or "医药" in tags:
        return "偏垂直行业跟踪型作者，更适合做主题研究和产业信息输入。"
    if "交易" in tags:
        return "偏市场复盘和择时型作者，对盘面线索和情绪变化更敏感。"
    if score >= 7 and has_depth >= 3:
        return "综合研究能力不错，具备持续输出的潜力，但仍需要更多样本校准。"
    return "更适合作为补充信息源，价值在于提供线索与视角，而不是单点定论。"


def infer_strengths(tags: list[str]) -> list[str]:
    mapping = {
        "宏观": "宏观叙事与跨资产线索整理",
        "固收": "利率、信用和债券市场框架",
        "商品": "商品产业链与期货逻辑",
        "科技": "科技产业主题跟踪",
        "医药": "医药行业观察",
        "交易": "市场节奏与交易线索提炼",
        "纪要": "会议信息整理与纪要提炼",
        "深度": "长文研究与框架化分析",
        "日本": "日本政策与市场观察",
    }
    strengths = [mapping[tag] for tag in tags if tag in mapping]
    return strengths[:5] or ["主题信息整理"]


def infer_weaknesses(tags: list[str], articles: list[dict[str, Any]]) -> list[str]:
    weaknesses: list[str] = []
    low_evidence_count = sum(
        1 for item in articles if not item.get("summary") or (item.get("summary") or {}).get("limited_evidence")
    )
    if len(articles) < 5:
        weaknesses.append("样本量还不够大，作者画像需要继续校准。")
    if "交易" in tags:
        weaknesses.append("更适合观察节奏与线索，不宜直接替代独立研究。")
    if "纪要" in tags:
        weaknesses.append("纪要类内容依赖原始交流质量，观点需要二次验证。")
    if low_evidence_count > max(1, len(articles) // 2):
        weaknesses.append("可用全文样本偏少，部分判断仍建立在标题和摘要上。")
    return weaknesses[:4]


def infer_style(tags: list[str], sample_titles: list[str]) -> str:
    if "纪要" in tags:
        return "偏资料整理和纪要汇编"
    if "深度" in tags:
        return "偏长文研究和框架拆解"
    if any("复盘" in title for title in sample_titles):
        return "偏市场复盘和快评"
    return "偏主题跟踪和观点表达"


def infer_use_cases(tags: list[str]) -> list[str]:
    if "宏观" in tags or "固收" in tags:
        return ["做宏观框架输入", "跟踪利率信用变化", "辅助资产配置讨论"]
    if "商品" in tags:
        return ["跟踪供需变化", "辅助交易准备", "观察产业链边际变化"]
    if "科技" in tags or "医药" in tags:
        return ["做行业跟踪", "补充主题观点", "寻找产业催化线索"]
    if "交易" in tags:
        return ["盘后复盘", "观察短期情绪", "跟踪节奏变化"]
    return ["做主题扫描", "补充信息输入"]


def estimate_reading_time(content: str) -> int:
    if not content:
        return 1
    return max(1, round(len(content) / 850))
