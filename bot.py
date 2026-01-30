import os
import re
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup
import trafilatura


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

EXTRA_KEYWORDS_DEFAULT = ["FDA", "EMA", "NMPA", "clinical trial", "Phase 3", "acquisition", "approval"]

MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))
DAYS_LOOKBACK = int(os.getenv("DAYS_LOOKBACK", "2"))

# Free translation via LibreTranslate public instance (no signup)
# Note: public instances may rate-limit; we implement fallback.
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate")


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


def esc(x: str) -> str:
    return html.escape(x or "")


def libre_translate(text: str, source: str = "en", target: str = "zh") -> str:
    text = (text or "").strip()
    if not text:
        return ""
    try:
        r = requests.post(
            LIBRETRANSLATE_URL,
            timeout=20,
            data={
                "q": text,
                "source": source,
                "target": target,
                "format": "text",
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        out = (data.get("translatedText") or "").strip()
        return out or text
    except Exception:
        # Fallback: return original text if translation fails
        return text


def fetch_article_text(url: str) -> str:
    """Try to extract main text. Might fail for paywalls/anti-bot pages."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return (extracted or "").strip()
    except Exception:
        return ""


def naive_bullets(text: str, max_bullets: int = 3) -> list[str]:
    """Simple heuristic summary: take first sentences/clauses."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []

    # Split by sentence-ish punctuation
    parts = re.split(r"(?<=[\.\!\?。！？])\s+", t)
    bullets = []
    for p in parts:
        p = p.strip()
        if len(p) < 30:
            continue
        bullets.append(p)
        if len(bullets) >= max_bullets:
            break

    if not bullets:
        bullets = [t[:220]]
    return [b[:260] for b in bullets]


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
    if r.status_code >= 400:
        return


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
        # Extract article text (best-effort)
        article_text = fetch_article_text(it["link"])
        base_text = article_text if len(article_text) >= 200 else (it.get("rss_summary") or it["title"])

        bullets_en = naive_bullets(base_text, max_bullets=3)

        title_cn = libre_translate(it["title"], source="en", target="zh")
        bullets_cn = [libre_translate(b, source="en", target="zh") for b in bullets_en]

        company = it.get("company", "")
        source = it.get("source", "")

        lines.append(f"{idx}. <b>{esc(title_cn[:120])}</b>")
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

    # Try images for top 3
    for it in enriched[:3]:
        img = try_get_og_image(it["link"])
        if not img:
            continue
        # no link in caption; just text
        caption = f"🖼️ {it['title_cn'][:180]}"
        tg_send_photo(token, chat_id, img, caption)


if __name__ == "__main__":
    main()
