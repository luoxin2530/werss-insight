import asyncio
import json
import re
import time
import math
from html import unescape
from functools import lru_cache
from typing import Any

import httpx

from .config import BUNDLED_MODELS_DIR, DATA_DIR, Settings


@lru_cache(maxsize=4)
def local_text_embedding(model_name: str) -> Any:
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError("内置本地向量模型需要 fastembed，请更新镜像或执行 pip install -r requirements.txt") from exc
    bundled_dir = BUNDLED_MODELS_DIR / "fastembed"
    cache_dir = bundled_dir if bundled_dir.exists() else DATA_DIR / "models" / "fastembed"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))


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


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        if not self.settings.rag_enabled:
            return False
        if self.provider == "local":
            return bool(self.settings.rag_local_embedding_model)
        return bool(self.settings.rag_api_key and self.settings.rag_api_base_url and self.settings.rag_embedding_model)

    @property
    def provider(self) -> str:
        provider = str(getattr(self.settings, "rag_embedding_provider", "local") or "local").strip().lower()
        return provider if provider in {"local", "remote"} else "local"

    @property
    def model_name(self) -> str:
        if self.provider == "local":
            return self.settings.rag_local_embedding_model
        return self.settings.rag_embedding_model

    def _batch_size(self) -> int:
        base_url = self.settings.rag_api_base_url.lower()
        if "dashscope.aliyuncs.com" in base_url:
            return 10
        return 100

    def _local_prefix(self, purpose: str) -> str:
        model = self.model_name.lower()
        if "e5" in model:
            return "query: " if purpose == "query" else "passage: "
        if "bge" in model and purpose == "query":
            if "zh" in model:
                return "为这个句子生成表示以用于检索相关文章："
            return "Represent this sentence for searching relevant passages: "
        return ""

    async def embed(self, texts: list[str], purpose: str = "passage") -> list[list[float]]:
        if not self.enabled:
            raise RuntimeError("Embedding is not configured")
        clean_texts = [str(text or "").strip() for text in texts if str(text or "").strip()]
        if not clean_texts:
            return []
        if self.provider == "local":
            model = local_text_embedding(self.settings.rag_local_embedding_model)
            prefix = self._local_prefix(purpose)
            prepared = [f"{prefix}{text}" for text in clean_texts]
            return await asyncio.to_thread(
                lambda: [embedding.tolist() for embedding in model.embed(prepared)]
            )

        headers = {
            "Authorization": f"Bearer {self.settings.rag_api_key}",
            "Content-Type": "application/json",
        }
        embeddings: list[list[float]] = []
        batch_size = max(1, self._batch_size())
        base_url = self.settings.rag_api_base_url.rstrip("/")

        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for start in range(0, len(clean_texts), batch_size):
                batch = clean_texts[start : start + batch_size]
                payload = {"model": self.settings.rag_embedding_model, "input": batch}
                response = await client.post(f"{base_url}/embeddings", headers=headers, json=payload)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = (exc.response.text or "").strip()
                    if exc.response.status_code == 400 and "dashscope.aliyuncs.com" in base_url.lower():
                        raise RuntimeError(
                            "DashScope 的 text-embedding-v4 每次最多支持 10 条输入；"
                            "系统已自动分批，但接口仍返回 400。"
                            f"请检查模型名、API Key 和 base_url。接口返回: {detail or exc.response.reason_phrase}"
                        ) from exc
                    raise RuntimeError(
                        f"Embedding API 请求失败：HTTP {exc.response.status_code}，"
                        f"{detail or exc.response.reason_phrase}"
                    ) from exc
                data = response.json()
                batch_embeddings = [item["embedding"] for item in data.get("data") or []]
                if len(batch_embeddings) != len(batch):
                    raise RuntimeError(
                        f"Embedding API 返回数量不匹配：输入 {len(batch)} 条，返回 {len(batch_embeddings)} 条"
                    )
                embeddings.extend(batch_embeddings)
        return embeddings


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
    return f"这是一篇关于{focus}的短文，目前更适合作为线索输入而不是完整分析结论。"


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
        ("教程", ["教程", "入门", "指南", "攻略", "手把手", "怎么做", "实战"]),
        ("观点", ["观点", "看法", "评论", "解读", "判断", "分析"]),
        ("资料", ["资料", "汇总", "盘点", "合集", "清单", "纪要", "要点"]),
        ("深度", ["框架", "深度", "模型", "测算", "长文", "详解"]),
        ("访谈", ["访谈", "对话", "采访", "问答", "q&a"]),
        ("案例", ["案例", "实例", "复盘", "拆解", "演示"]),
        ("产品", ["产品", "工具", "功能", "发布", "更新", "版本"]),
        ("技术", ["技术", "代码", "架构", "算法", "工程", "api", "sdk"]),
        ("行业", ["行业", "产业", "赛道", "趋势", "市场", "生态"]),
        ("新闻", ["新闻", "公告", "快讯", "动态", "通报"]),
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
    if "资料" in tags:
        score += 0.5
    if any(tag in tags for tag in ["教程", "技术", "案例", "行业", "产品"]):
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
    if "资料" in tags:
        return "更适合作为资料参考和后续检索线索，适合边读边做笔记。"
    if "教程" in tags or "技术" in tags:
        return "适合边看边做，重点跟着步骤、方法和示例走。"
    if "案例" in tags:
        return "适合快速浏览案例脉络，再提炼可复用的经验。"
    if not content:
        return "当前只有标题或摘要，可用证据不足，建议等全文补抓后再看。"
    if len(paragraphs) <= 2:
        return "篇幅偏短，更像快评或观点摘录，适合快速浏览。"
    return "适合作为主题跟踪材料阅读，重点看作者的论据、数据和结论。"


def infer_difficulty(content: str, tags: list[str]) -> str:
    if len(content) > 9000 or "深度" in tags or "技术" in tags:
        return "high"
    if len(content) > 2500 or "案例" in tags:
        return "medium"
    return "low"


def infer_audience(tags: list[str], score: float) -> str:
    if "教程" in tags or "技术" in tags:
        return "需要快速上手、动手实践的人"
    if "资料" in tags or "行业" in tags:
        return "需要做资料整理或主题跟踪的人"
    if "观点" in tags or "案例" in tags:
        return "喜欢比较观点和拆解案例的人"
    if score >= 8:
        return "适合做重点深读"
    return "适合做快速浏览"


def infer_capability(tags: list[str], score: float, has_depth: int) -> str:
    if "技术" in tags or "深度" in tags:
        return "偏框架拆解和结构化表达的作者，擅长把复杂主题讲清楚。"
    if "资料" in tags:
        return "偏资料整理和信息汇编型作者，适合快速获得线索。"
    if "案例" in tags:
        return "偏案例分析和经验复盘型作者，适合寻找实操细节。"
    if "观点" in tags:
        return "偏观点表达和评论型作者，适合做对照阅读。"
    if score >= 7 and has_depth >= 3:
        return "综合分析能力不错，具备持续输出的潜力，但仍需要更多样本校准。"
    return "更适合作为补充信息源，价值在于提供线索与视角，而不是单点定论。"


def infer_strengths(tags: list[str]) -> list[str]:
    mapping = {
        "教程": "步骤拆解与上手指导",
        "观点": "评论与判断表达",
        "资料": "信息整理与线索汇编",
        "深度": "长文分析与框架归纳",
        "访谈": "采访整理与问题提炼",
        "案例": "案例分析与经验复盘",
        "产品": "产品观察与功能解读",
        "技术": "技术讲解与实践说明",
        "行业": "行业观察与趋势跟踪",
        "新闻": "事件整理与动态追踪",
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
    if "观点" in tags:
        weaknesses.append("观点类内容更依赖上下文和持续样本，单篇判断容易偏差。")
    if "资料" in tags:
        weaknesses.append("资料类内容依赖原始材料完整性，最好结合全文复核。")
    if low_evidence_count > max(1, len(articles) // 2):
        weaknesses.append("可用全文样本偏少，部分判断仍建立在标题和摘要上。")
    return weaknesses[:4]


def infer_style(tags: list[str], sample_titles: list[str]) -> str:
    if "资料" in tags:
        return "偏资料整理和要点汇编"
    if "深度" in tags:
        return "偏长文分析和框架拆解"
    if any("复盘" in title for title in sample_titles) or "案例" in tags:
        return "偏案例复盘和快评"
    if "观点" in tags:
        return "偏观点表达和评论"
    return "偏主题跟踪和信息整理"


def infer_use_cases(tags: list[str]) -> list[str]:
    if "教程" in tags or "技术" in tags:
        return ["做学习参考", "辅助上手实践", "快速找到操作路径"]
    if "资料" in tags or "行业" in tags:
        return ["做资料整理", "辅助主题扫描", "跟踪趋势变化"]
    if "观点" in tags or "案例" in tags:
        return ["做观点对照", "补充案例参考", "寻找可复用经验"]
    if "深度" in tags:
        return ["做深度阅读", "提炼框架", "沉淀长期笔记"]
    return ["做主题扫描", "补充信息输入"]


def estimate_reading_time(content: str) -> int:
    if not content:
        return 1
    return max(1, round(len(content) / 850))


def split_text_for_rag(text: str, chunk_size: int = 900, overlap: int = 140) -> list[str]:
    cleaned = re.sub(r"\n{3,}", "\n\n", strip_html(text or "")).strip()
    if not cleaned:
        return []
    chunk_size = max(200, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size - 50))
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= chunk_size:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current[:chunk_size])
        if len(paragraph) <= chunk_size:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            end = min(len(paragraph), start + chunk_size)
            chunks.append(paragraph[start:end])
            if end >= len(paragraph):
                current = ""
                break
            start = max(0, end - overlap)
    if current:
        chunks.append(current[:chunk_size])
    deduped: list[str] = []
    seen = set()
    for chunk in chunks:
        key = re.sub(r"\s+", " ", chunk).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(chunk.strip())
    return deduped


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return -1.0
    return dot / (left_norm * right_norm)
