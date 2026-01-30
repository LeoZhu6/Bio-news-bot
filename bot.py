import os
import re
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup


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


def google_news_rss_url(query: str) -> str:
    # Google News RSS
    # hl/gl/ceid 这里用 US 英文聚合，覆盖国际媒体；中文公司名也能搜到
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


def format_digest(items):
    lines = []
    lines.append("<b>🧬 医药大厂新闻速递（国内外）</b>")
    lines.append(f"<i>{esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}</i>")
    lines.append("")
    for i, it in enumerate(items, 1):
        title = esc((it.get("title") or "")[:200])
        link = it.get("link") or ""
        company = esc(it.get("company") or "")
        source = esc(it.get("source") or "")
        summary = esc((it.get("summary") or "")[:260])

        headline = f'🔹 <a href="{esc(link)}">{title}</a>' if link else f"🔹 {title}"
        meta = " · ".join([x for x in [company, source] if x])
        if meta:
            meta = f"<i>{meta}</i>"

        lines.append(f"{i}. {headline}")
        if meta:
            lines.append(meta)
        if summary:
            lines.append(summary)
        lines.append("")
    lines.append("—")
    lines.append("<i>来源：Google News RSS 聚合；配图为网页 OG 图（可能因站点限制缺失）。</i>")
    return "\n".join(lines).strip()


def tg_send_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        timeout=15,
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
        timeout=20,
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files=None,
        params={"photo": photo_url},
    )
    # 有些 host 会拒绝 Telegram 拉图，失败就忽略
    if r.status_code >= 400:
        return
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

            summary = clean_html_to_text(e.get("summary", "") or e.get("description", "") or "")

            all_items.append(
                {
                    "company": company_name,
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": published.isoformat() if published else "",
                    "summary": summary,
                }
            )
            seen_links.add(link)

    all_items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return all_items[:MAX_ITEMS]


def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing BOT_TOKEN or CHAT_ID (use GitHub Secrets).")

    items = fetch_news()
    if not items:
        tg_send_message(token, chat_id, "<b>🧬 医药新闻</b>\n\n今天未抓到要闻。")
        return

    tg_send_message(token, chat_id, format_digest(items))

    # 尝试给前 3 条补图
    for it in items[:3]:
        img = try_get_og_image(it["link"])
        if not img:
            continue
        caption = f'🖼️ <a href="{esc(it["link"])}">{esc(it["title"][:180])}</a>'
        tg_send_photo(token, chat_id, img, caption)


if __name__ == "__main__":
    main()
