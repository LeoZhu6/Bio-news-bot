import os
import re
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup
import trafilatura


# ========= 配置区 =========
COMPANIES = [
    ("Pfizer 辉瑞", ("Pfizer", "辉瑞")),
    ("Merck 默沙东", ("Merck", "默沙东", "MSD")),
    ("J&J 强生", ("Johnson & Johnson", "J&J", "强生")),
    ("Roche 罗氏", ("Roche", "罗氏")),
    ("Novartis 诺华", ("Novartis", "诺华")),
    ("AstraZeneca 阿斯利康", ("AstraZeneca", "阿斯利康")),
    ("GSK 葛兰素史克", ("GSK", "GlaxoSmithKline", "葛兰素史克")),
    ("Sanofi 赛诺菲", ("Sanofi", "赛诺菲")),
    ("BMS 百时美施贵宝", ("Bristol Myers Squibb", "BMS", "百时美施贵宝")),
    ("AbbVie 艾伯维", ("AbbVie", "艾伯维")),
    ("Amgen 安进", ("Amgen", "安进")),
    ("Eli Lilly 礼来", ("Eli Lilly", "Lilly", "礼来")),
    ("Novo Nordisk 诺和诺德", ("Novo Nordisk", "诺和诺德")),
    ("Moderna", ("Moderna",)),
    ("BioNTech", ("BioNTech",)),
    ("恒瑞医药", ("恒瑞医药", "Hengrui")),
    ("百济神州", ("百济神州", "BeiGene")),
    ("药明康德", ("药明康德", "WuXi AppTec")),
    ("复星医药", ("复星医药", "Fosun Pharma")),
    ("信达生物", ("信达生物", "Innovent")),
    ("君实生物", ("君实生物", "Junshi Biosciences")),
]

EXTRA_KEYWORDS_DEFAULT = [
    "FDA", "EMA", "NMPA",
    "clinical trial", "Phase 1", "Phase 2", "Phase 3",
    "acquisition", "merger", "partnership", "licensing",
    "approval", "complete response letter", "CRL",
    "earnings", "guidance"
]

MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))
DAYS_LOOKBACK = int(os.getenv("DAYS_LOOKBACK", "2"))

# 更长摘要：默认 5 条要点；每条最长 420 字符（英文/中文字符都按长度算）
BULLETS_PER_ITEM = int(os.getenv("BULLETS_PER_ITEM", "5"))
BULLET_MAX_CHARS = int(os.getenv("BULLET_MAX_CHARS", "420"))

# 免费 LibreTranslate：公共实例可能限流/不稳，所以做轮询+失败降级（不让 workflow 崩）
# 你可以在 GitHub Actions env 里设置 LIBRETRANSLATE_URL 为你更稳定的实例
LIBRETRANSLATE_URLS = [
    os.getenv("LIBRETRANSLATE_URL", "").strip(),
    "https://libretranslate.de/translate",
    "https://translate.astian.org/translate",
]
LIBRETRANSLATE_URLS = [u for u in LIBRETRANSLATE_URLS if u]


# ========= 通用工具 =========
def google_news_rss_url(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def clean_html_to_text(s: str) -> str:
    soup = BeautifulSoup(s or "", "lxml")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def parse_entry_time(entry):
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    try:
        return datetime(*tm[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def esc(x: str) -> str:
    return html.escape(x or "")


def try_get_og_image(url: str, timeout: float = 10.0):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for attrs in (
            {"property": "og:image"},
            {"name": "og:image"},
            {"property": "twitter:image"},
            {"name": "twitter:image"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                img = tag["content"].strip()
                if img.startswith("http"):
                    return img
    except Exception:
        return None
    return None


# ========= 翻译（免费） =========
def _split_text(text: str, chunk_size: int = 900) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks, buf = [], []
    n = 0
    # 按换行优先切，减少语义破碎
    for part in re.split(r"(\n+)", text):
        if not part:
            continue
        if n + len(part) > chunk_size and buf:
            chunks.append("".join(buf).strip())
            buf, n = [], 0
        buf.append(part)
        n += len(part)
    if buf:
        chunks.append("".join(buf).strip())
    return chunks


def libre_translate(text: str, source: str = "auto", target: str = "zh") -> str:
    """
    免费翻译：轮询多个 LibreTranslate 公共实例。
    - source 使用 auto，提高混合语言文本的成功率
    - 遇到限流/服务错误自动切换实例
    - 全部失败则返回原文（保证任务不失败）
    """
    text = (text or "").strip()
    if not text:
        return ""

    chunks = _split_text(text, chunk_size=900)
    out_chunks = []

    for ch in chunks:
        translated = None

        for url in LIBRETRANSLATE_URLS:
            try:
                r = requests.post(
                    url,
                    timeout=25,
                    data={
                        "q": ch,
                        "source": source,
                        "target": target,
                        "format": "text",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                )

                # 常见限流/不稳定：换下一个实例
                if r.status_code in (429, 500, 502, 503, 504):
                    continue

                r.raise_for_status()
                data = r.json()
                translated = (data.get("translatedText") or "").strip()
                if translated:
                    break
            except Exception:
                continue

        out_chunks.append(translated if translated else ch)

    return "\n".join(out_chunks).strip()


# ========= 正文抽取与摘要 =========
def fetch_article_text(url: str) -> str:
    """尽量抽取新闻正文；遇到付费墙/反爬会失败，调用处会自动降级到 RSS summary."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        return (extracted or "").strip()
    except Exception:
        return ""


def naive_bullets(text: str, max_bullets: int = 5) -> list[str]:
    """
    轻量摘要：取前若干句作为要点（足够稳定，不依赖额外 API）。
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []

    parts = re.split(r"(?<=[\.\!\?。！？])\s+", t)
    bullets = []
    for p in parts:
        p = p.strip()
        if len(p) < 30:
            continue
        bullets.append(p[:BULLET_MAX_CHARS])
        if len(bullets) >= max_bullets:
            break

    if not bullets:
        bullets = [t[:BULLET_MAX_CHARS]]

    return bullets


# ========= Telegram =========
def tg_send_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        timeout=20,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    r.raise_for_status()


def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    r = requests.post(
        url,
        timeout=25,
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        params={"photo": photo_url},
    )
    # 图片失败不影响主流程
    return r.status_code < 400


# ========= 抓取新闻 =========
def fetch_news():
    extra_keywords = [x.strip() for x in os.getenv("EXTRA_KEYWORDS", "").split(",") if x.strip()]
    if not extra_keywords:
        extra_keywords = EXTRA_KEYWORDS_DEFAULT

    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)
    all_items = []
    seen_links = set()

    for company_name, queries in COMPANIES:
        base = "(" + " OR ".join([f'"{q}"' if " " in q else q for q in queries]) + ")"
        extra = "(" + " OR ".join([f'"{k}"' if " " in k else k for k in extra_keywords]) + ")"
        q = f"{base} {extra}"

        feed = feedparser.parse(google_news_rss_url(q))
        for e in feed.entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link or link in seen_links:
                continue

            published = parse_entry_time(e)
            if published and published < cutoff:
                continue

            source = ""
            if e.get("source") and isinstance(e["source"], dict):
                source = (e["source"].get("title") or "").strip()

            rss_summary = clean_html_to_text(e.get("summary", "") or e.get("description", "") or "")

            all_items.append(
                {
                    "company": company_name,
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": published.isoformat() if published else "",
                    "rss_summary": rss_summary,
                }
            )
            seen_links.add(link)

    all_items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return all_items[:MAX_ITEMS]


def build_cn_digest(items: list[dict]) -> tuple[str, list[dict]]:
    lines = []
    lines.append("<b>🧬 医药大厂新闻速递（中文要点）</b>")
    lines.append(f"<i>{esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}</i>")
    lines.append("")

    enriched = []

    for idx, it in enumerate(items, 1):
        # 抽正文；不足则用 RSS summary/标题降级
        article_text = fetch_article_text(it["link"])
        base_text = article_text if len(article_text) >= 250 else (it.get("rss_summary") or it["title"])

        bullets_src = naive_bullets(base_text, max_bullets=BULLETS_PER_ITEM)

        # 关键：source=auto，且有多实例轮询
        title_cn = libre_translate(it["title"], source="auto", target="zh")
        bullets_cn = [libre_translate(b, source="auto", target="zh") for b in bullets_src]

        company = it.get("company", "")
        source = it.get("source", "")

        # 不用链接形式：纯文本输出
        lines.append(f"{idx}. <b>{esc(title_cn[:180])}</b>")
        lines.append(f"<i>{esc(company)} · {esc(source)}</i>")
        for b in bullets_cn:
            b = (b or "").strip()
            if b:
                lines.append(f"• {esc(b)}")
        lines.append("")

        enriched.append(
            {
                "title_cn": title_cn,
                "bullets_cn": bullets_cn,
                "company": company,
                "source": source,
                "link": it["link"],
            }
        )

    lines.append("—")
    lines.append("<i>说明：为合规与稳定性，推送为“中文摘要/要点复述”，不直接转发原文全文。</i>")
    return "\n".join(lines).strip(), enriched


def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing BOT_TOKEN or CHAT_ID (use GitHub Secrets).")

    items = fetch_news()
    if not items:
        tg_send_message(token, chat_id, "<b>🧬 医药新闻</b>\n\n今天未抓到要闻。")
        return

    digest, enriched = build_cn_digest(items)
    tg_send_message(token, chat_id, digest)

    # 可选：给前 3 条尝试配图（失败不影响主流程）
    for it in enriched[:3]:
        img = try_get_og_image(it["link"])
        if not img:
            continue
        caption = f"🖼️ {it['title_cn'][:180]}"
        tg_send_photo(token, chat_id, img, caption)


if __name__ == "__main__":
    main()
