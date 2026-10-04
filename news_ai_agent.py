"""
Native-language RSS aggregator (NO translation, so no rate-limit problems).

Outputs (each a list of [title, description, image_url, date, link]):
  newstelugu.json -> NTV Telugu + BBC Telugu
  english.json    -> The Hindu National + The Guardian
  hindi.json      -> Hindi channels (BBC Hindi, NDTV, Amar Ujala, Bhaskar ...)

Needs: pip install feedparser requests
"""
import json
import re
import time
import html
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import List

import feedparser
import requests

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
PLACEHOLDER_IMG = "https://i.ibb.co/placeholder.jpg"

# ===== CONFIG: edit feeds here =====
# If a feed URL is dead, it is just skipped with a message - the others still work.
FEEDS = {
    "newstelugu.json": [
        {"url": "https://ntvtelugu.com/feed", "name": "NTV Telugu", "max_articles": 30},
        {"url": "https://feeds.bbci.co.uk/telugu/rss.xml", "name": "BBC Telugu", "max_articles": 10},
    ],
    "english.json": [
        {"url": "https://www.thehindu.com/news/national/feeder/default.rss", "name": "The Hindu National",
         "max_articles": 10, "max_age_days": 2},
        {"url": "https://www.theguardian.com/world/rss", "name": "The Guardian",
         "max_articles": 10, "max_age_days": 2},
    ],
    "hindi.json": [
        {"url": "https://feeds.bbci.co.uk/hindi/rss.xml", "name": "BBC Hindi", "max_articles": 10},
        {"url": "https://feeds.feedburner.com/ndtvkhabar-latest", "name": "NDTV Hindi", "max_articles": 10},
        {"url": "https://www.amarujala.com/rss/breaking-news.xml", "name": "Amar Ujala", "max_articles": 10},
        {"url": "https://www.bhaskar.com/rss-v1--category-1061.xml", "name": "Dainik Bhaskar", "max_articles": 10},
    ],
}

SKIP_KEYWORDS = [
    'horoscope', 'astrology', 'zodiac', 'రాశి', 'జాతక', 'దినఫలం', 'వార ఫలం', 'మాస ఫలం',
    'rasiphalalu', 'dinaphalam', 'राशिफल', 'भविष्यफल', 'कुंडली',
    'temple', 'దేవాలయం', 'ఆలయం', 'గుడి', 'devasthanam', 'tirupati', 'స్వామి', 'దర్శనం', 'puja', 'పూజ',
    'promotion', 'ప్రమోషన్', 'ప్రకటన', 'advertisement', 'subscribe', 'follow us', 'మా ఛానల్', 'సబ్స్క్రైబ్',
]


# ===== TEXT HELPERS =====
def clean_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def should_skip_article(title: str, description: str) -> bool:
    combined = f"{title} {description}".lower()
    return any(kw in combined for kw in SKIP_KEYWORDS)


def fetch_description_fallback(url: str, max_paragraphs: int = 4) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.raise_for_status()
        parts = []
        for p in re.findall(r'<p[^>]*>(.*?)</p>', r.text, re.IGNORECASE | re.DOTALL):
            t = clean_text(p)
            if len(t) > 50 and not any(b in t.lower() for b in ['advertisement', 'ప్రకటన', 'subscribe', 'मా ఛానల్']):
                parts.append(t)
                if len(parts) >= max_paragraphs:
                    break
        return " ".join(parts)
    except Exception:
        return ""


# ===== IMAGE HELPERS =====
def extract_image_candidates(entry: dict, base_url: str) -> List[str]:
    candidates = []

    def add(url):
        if not url:
            return
        url = str(url).strip()
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            p = urlparse(base_url)
            url = f"{p.scheme}://{p.netloc}{url}"
        if url.startswith('http'):
            url = url.rstrip('.,)"\'')
            if url not in candidates:
                candidates.append(url)

    for m in entry.get('media_content', []) or []:
        add(m.get('url', ''))
    for e in entry.get('enclosures', []) or []:
        if 'image' in (e.get('type') or 'image'):
            add(e.get('href', ''))
    for t in entry.get('media_thumbnail', []) or []:
        add(t.get('url', ''))

    content = ''
    if entry.get('content'):
        content = entry['content'][0].get('value', '')
    if not content:
        content = entry.get('summary', '') or entry.get('description', '')
    for src in re.findall(r'<img[^>]+src=["\']?([^"\'>\s]+)["\']?', content or '', re.IGNORECASE):
        add(src)
    return candidates


def score_image(url: str) -> int:
    u = url.lower()
    if any(bad in u for bad in ['logo', 'icon', 'avatar', 'spacer', 'pixel', 'dot.gif']):
        return -9999
    score = 0
    if 'guim.co.uk' in u or 'ichef.bbci.co.uk' in u:
        score += 100
    if u.startswith('https://'):
        score += 50
    if '/master/' in u:
        score += 200
    m = re.search(r'[?&]width=(\d+)', u)
    if m:
        w = int(m.group(1))
        score += 300 if w >= 1200 else 200 if w >= 800 else -200 if w <= 300 else 0
    return score


def upgrade_image_url(url: str) -> str:
    if 'guim.co.uk' in url:
        p = urlparse(url)
        q = parse_qs(p.query)
        q['width'] = ['1200']
        q['quality'] = ['90']
        q.setdefault('auto', ['format'])
        flat = {k: v[0] if len(v) == 1 else v for k, v in q.items()}
        return urlunparse(p._replace(query=urlencode(flat, doseq=True)))
    if 'ichef.bbci.co.uk' in url:
        url = re.sub(r'(/ace/[^/]+/)\d+/', r'\g<1>1024/', url)
        url = re.sub(r'ichef\.bbci\.co\.uk/news/\d+/', 'ichef.bbci.co.uk/news/1024/', url)
    return url


def validate_image_url(url: str) -> bool:
    try:
        r = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True)
        return r.status_code == 200 and 'image/' in r.headers.get('Content-Type', '')
    except Exception:
        return False


def get_best_image(candidates: List[str]) -> str:
    if not candidates:
        return PLACEHOLDER_IMG
    scored = sorted(((u, score_image(u)) for u in candidates), key=lambda x: x[1], reverse=True)
    for url, s in scored:
        if s <= -9999:
            continue
        for test in dict.fromkeys([upgrade_image_url(url), url]):
            if validate_image_url(test):
                return test
    return scored[0][0]


# ===== AGGREGATOR =====
def format_date(entry: dict) -> str:
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if parsed:
        try:
            return time.strftime("%d-%m-%Y", parsed)
        except Exception:
            pass
    raw = entry.get('published') or entry.get('updated') or ''
    try:
        return parsedate_to_datetime(raw).strftime("%d-%m-%Y")
    except Exception:
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return datetime.now().strftime("%d-%m-%Y")


def is_fresh(entry: dict, max_age_days: int) -> bool:
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if parsed:
        try:
            return (datetime.utcnow() - datetime(*parsed[:6])).total_seconds() < max_age_days * 86400
        except Exception:
            pass
    return True


def fetch_source(source: dict) -> List[List[str]]:
    url, name = source["url"], source["name"]
    max_articles = source.get("max_articles", 20)
    max_age = source.get("max_age_days")
    prefix = name.replace(" Telugu", "").strip()
    results = []

    print(f"\n🔄 {name}: fetching up to {max_articles} articles")
    try:
        fetch_url = f"{url}{'&' if '?' in url else '?'}_cb={int(time.time())}"
        resp = requests.get(fetch_url, headers={**HEADERS, 'Cache-Control': 'no-cache'}, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"   ❌ {name} failed: {e}")
        return results

    if not feed.entries:
        print(f"   ⚠️ No entries for {name}")
        return results

    skipped_old = 0
    for entry in feed.entries:
        if len(results) >= max_articles:
            break
        try:
            if max_age and not is_fresh(entry, max_age):
                skipped_old += 1
                continue

            link = entry.get('link', '').strip()
            title = clean_text(entry.get('title', 'No Title'))
            raw_desc = entry.get('summary') or entry.get('description', '')
            if (not raw_desc or len(clean_text(raw_desc)) < 100) and entry.get('content'):
                raw_desc = entry['content'][0].get('value', '') or raw_desc

            if not raw_desc or len(clean_text(raw_desc)) < 150:
                scraped = fetch_description_fallback(link, 5)
                if scraped:
                    raw_desc = scraped

            description = clean_text(raw_desc)
            if should_skip_article(title, description):
                print(f"   ⏭️ Skipped promo/temple/astro: {title[:50]}")
                continue
            if len(description) < 20:
                description = "Read the full story for more details."
            if not description.startswith(f"{prefix}:"):
                description = f"{prefix}: {description}"

            image = get_best_image(extract_image_candidates(entry, url))
            results.append([title, description, image, format_date(entry), link])
        except Exception:
            continue

    print(f"   ✅ {name}: {len(results)} articles (old skipped: {skipped_old})")
    return results


def save_json(data: List[List[str]], filename: str):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 Saved {len(data)} articles -> {filename}")


def main():
    print("📰 Native RSS aggregation (no translation)")
    print("=" * 60)
    for filename, sources in FEEDS.items():
        print(f"\n===== {filename} =====")
        combined = []
        for src in sources:
            combined.extend(fetch_source(src))
        if combined:
            save_json(combined, filename)
        else:
            print(f"⚠️ No articles for {filename}; existing file left untouched.")
    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
