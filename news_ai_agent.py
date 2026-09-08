import feedparser
import json
import re
import requests
import time
import html
import random
import hashlib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import List
from deep_translator import GoogleTranslator

# Global cache for the current execution to avoid redundant API calls
translation_cache = {}

# ===== TRANSLATION OPTIMIZATION FUNCTIONS (TARGET: TELUGU) =====

def clean_text_for_translation(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    text = re.sub(r'([.!?])\1{2,}', r'\1', text)
    text = re.sub(r'(\?|&)(utm_[^=]+|fbclid|gclid|ref|source)=[^&\s]+', '', text, flags=re.IGNORECASE)
    
    def truncate_long_urls(match):
        url = match.group(0)
        return url[:140] + "...[link]" if len(url) > 150 else url
    text = re.sub(r'https?://[^\s<>"]+', truncate_long_urls, text)
    return re.sub(r'\s+', ' ', text).strip()

def split_text_intelligently(text: str, max_length: int) -> list:
    if len(text) <= max_length:
        return [text]
    chunks = []
    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) + 1 <= max_length:
            current_chunk += (" " + p) if current_chunk else p
        else:
            if current_chunk: chunks.append(current_chunk)
            if len(p) > max_length:
                sentences = re.split(r'(?<=[।.?!])\s+', p)
                current_chunk = ""
                for s in sentences:
                    if len(current_chunk) + len(s) + 1 <= max_length:
                        current_chunk += (" " + s) if current_chunk else s
                    else:
                        if current_chunk: chunks.append(current_chunk)
                        if len(s) > max_length:
                            words = s.split(' ')
                            current_chunk = ""
                            for w in words:
                                if len(current_chunk) + len(w) + 1 <= max_length:
                                    current_chunk += (" " + w) if current_chunk else w
                                else:
                                    if current_chunk: chunks.append(current_chunk)
                                    current_chunk = w
                        else: current_chunk = s
            else: current_chunk = p
    if current_chunk: chunks.append(current_chunk)
    
    final_chunks = []
    for c in chunks:
        c = c.strip()
        if not c: continue
        if len(c) > max_length:
            for i in range(0, len(c), max_length): final_chunks.append(c[i:i+max_length].strip())
        else: final_chunks.append(c)
    return final_chunks

def translate_single_chunk_with_fallback(text: str, chunk_name: str, max_retries: int = 3) -> tuple:
    """Translates a single chunk, reducing its own size on failure without affecting other chunks."""
    if not text: 
        return "", True
    
    # Try 900 first. If it fails repeatedly, reduce size for THIS specific chunk only.
    sizes_to_try = [900, 450, 200]
    
    for size in sizes_to_try:
        sub_chunks = split_text_intelligently(text, size)
        translated_parts = []
        chunk_success = True
        
        for i, sub_chunk in enumerate(sub_chunks, 1):
            sub_name = f"{chunk_name} (sub {i}, len:{len(sub_chunk)})"
            success = False
            
            for attempt in range(max_retries):
                try:
                    cache_key = hashlib.md5(sub_chunk.encode('utf-8')).hexdigest()
                    is_cache_hit = cache_key in translation_cache
                    
                    if is_cache_hit:
                        translated = translation_cache[cache_key]
                        print(f"         ✅ {sub_name}: Cache hit")
                        success = True
                    else:
                        translator = GoogleTranslator(source='auto', target='te')
                        translated = translator.translate(sub_chunk)
                        if translated and isinstance(translated, str):
                            translated = translated.strip()
                            if any(err in translated.lower() for err in ['error 500', 'server error', 'bad request', 'too many requests', 'translation error']):
                                raise ValueError("Translator returned error")
                            translation_cache[cache_key] = translated
                            print(f"         ✅ {sub_name}: Translated via API")
                            success = True
                    
                    if success:
                        translated_parts.append(translated)
                        # 0 seconds delay for cache hit, 0.3-0.7s for successful API call
                        if not is_cache_hit:
                            time.sleep(random.uniform(0.3, 0.7))
                        break
                        
                except Exception as e:
                    # Exponential backoff ONLY on failure
                    delay = (2 ** attempt) + random.uniform(0.3, 0.7)
                    print(f"         ⚠️ {sub_name}: RETRY {attempt + 1}/{max_retries} (waiting {delay:.1f}s...)")
                    time.sleep(delay)
            
            if not success:
                chunk_success = False
                break # Break sub_chunks loop, try next smaller size for this chunk
                
        if chunk_success:
            return " ".join(translated_parts), True
            
    print(f"      ❌ {chunk_name}: Translation failed after all retries and size reductions.")
    return text, False

def translate_text_with_adaptive_chunking(text: str, text_name: str, max_retries: int = 3) -> tuple:
    if not text or not isinstance(text, str): 
        return text, True
    cleaned = clean_text_for_translation(text)
    if not cleaned: 
        return text, True
        
    start_time = time.time()
    # First, try to split the whole text into 900 char chunks
    main_chunks = split_text_intelligently(cleaned, 900)
    print(f"   🌐 {text_name}: Starting translation ({len(main_chunks)} chunk(s))...")
    
    translated_chunks = []
    all_succeeded = True
    
    for i, chunk in enumerate(main_chunks, 1):
        chunk_name = f"{text_name} part {i}"
        translated_part, success = translate_single_chunk_with_fallback(chunk, chunk_name, max_retries)
        translated_chunks.append(translated_part)
        if not success:
            all_succeeded = False
            
    elapsed = time.time() - start_time
    status = "✅ Success" if all_succeeded else "⚠️ Partial/Failed (Fallback to original used for failed parts)"
    print(f"   ⏱️ {text_name} translation completed in {elapsed:.2f}s. Status: {status}")
    
    return " ".join(translated_chunks), all_succeeded

# ===== ADVANCED ARTICLE FILTERING =====

def should_skip_article(title: str, description: str) -> bool:
    combined_text = f"{title} {description}".lower()
    skip_keywords = [
        'horoscope', 'astrology', 'zodiac', 'రాశి', 'జాతక', 'దినఫలం', 'వార ఫలం', 'మాస ఫలం', 'rasiphalalu', 'dinaphalam',
        'temple', 'దేవాలయం', 'ఆలయం', 'గుడి', 'devasthanam', 'tirupati', 'స్వామి', 'దర్శనం', 'puja', 'పూజ',
        'promotion', 'ప్రమోషన్', 'ప్రకటన', 'advertisement', 'subscribe', 'follow us', 'మా ఛానల్', 'సబ్స్క్రైబ్'
    ]
    return any(kw in combined_text for kw in skip_keywords)

# ===== SMART DESCRIPTION FALLBACK (ENHANCED FOR BBC & TV9) =====

def fetch_article_description_fallback(url: str, max_paragraphs: int = 4) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        p_tags = re.findall(r'<p[^>]*>(.*?)</p>', response.text, re.IGNORECASE | re.DOTALL)
        clean_parts = []
        for p in p_tags:
            text = re.sub(r'<[^>]+>', ' ', p)
            text = html.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 50 and not any(bad in text.lower() for bad in ['advertisement', 'ప్రకటన', 'subscribe', 'మా ఛానల్']):
                clean_parts.append(text)
                if len(clean_parts) >= max_paragraphs: break
        return " ".join(clean_parts)
    except Exception as e:
        return ""

# ===== HIGH-QUALITY IMAGE EXTRACTION (GUARDIAN, BBC, INDIAN EXPRESS) =====

def extract_image_candidates(entry: dict, base_url: str) -> List[str]:
    candidates = []
    def add_candidate(url):
        if not url: return
        url = str(url).strip()
        if url.startswith('//'): url = 'https:' + url
        elif url.startswith('/'):
            parsed_base = urlparse(base_url)
            url = f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
        if url.startswith('http'):
            url = url.rstrip('.,)"\'')
            if url not in candidates: candidates.append(url)

    if 'media_content' in entry:
        for media in entry['media_content']: add_candidate(media.get('url', ''))
    if 'enclosures' in entry:
        for enc in entry['enclosures']: add_candidate(enc.get('href', ''))
    if 'media_thumbnail' in entry:
        for thumb in entry['media_thumbnail']: add_candidate(thumb.get('url', ''))
            
    content_text = entry.get('content', [{}])[0].get('value', '') if 'content' in entry else ''
    if not content_text: content_text = entry.get('summary', '') or entry.get('description', '')
    if content_text:
        for img_src in re.findall(r'<img[^>]+src=["\']?([^"\'>\s]+)["\']?', content_text, re.IGNORECASE):
            add_candidate(img_src)
    return candidates

def score_image(url: str) -> int:
    if not url: return -9999
    url_lower = url.lower()
    if any(bad in url_lower for bad in ['logo', 'icon', 'avatar', 'spacer', 'pixel', 'dot.gif']): return -9999
    score = 0
    if 'guim.co.uk' in url_lower: score += 100
    if 'ichef.bbci.co.uk' in url_lower: score += 100 # BBC images get a boost
    if 'indianexpress.com' in url_lower: score += 100 # Boost for Indian Express images
    if url.startswith('https://'): score += 50
    if '/master/' in url_lower: score += 200
    
    width_match = re.search(r'[?&]width=(\d+)', url_lower)
    if width_match:
        w = int(width_match.group(1))
        if w >= 1200: score += 300
        elif w >= 800: score += 200
        elif w <= 300: score -= 200
    return score

def upgrade_image_url(url: str) -> str:
    if not url: return url
    
    # Upgrade Guardian Images
    if 'guim.co.uk' in url:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        query_params['width'] = ['1200']
        query_params['quality'] = ['90']
        if 'auto' not in query_params: query_params['auto'] = ['format']
        flat_params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}
        return urlunparse(parsed._replace(query=urlencode(flat_params, doseq=True)))
        
    # Upgrade BBC Images (Replace low-res path with 1024px)
    if 'ichef.bbci.co.uk' in url:
        url = re.sub(r'(/ace/[^/]+/)\d+/', r'\g<1>1024/', url)
        url = re.sub(r'ichef\.bbci\.co\.uk/news/\d+/', 'ichef.bbci.co.uk/news/1024/', url)
        url = re.sub(r'ichef\.bbci\.co\.uk/news/branded_[a-z]+/\d+/', 'ichef.bbci.co.uk/news/branded_telugu/1024/', url)
        
    # Upgrade Indian Express Images (Ensure high resolution)
    if 'indianexpress.com' in url:
        url = re.sub(r'/\d{3,4}x\d{3,4}/', '/1200x900/', url)
        
    return url

def validate_image_url(url: str) -> tuple:
    if not url: return False, 0, ""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        if response.status_code == 200 and 'image/' in response.headers.get('Content-Type', ''):
            return True, response.status_code, response.headers.get('Content-Type', '')
        return False, response.status_code, ""
    except: return False, 0, ""

def get_best_image(candidates: List[str]) -> str:
    if not candidates: return "https://i.ibb.co/placeholder.jpg"
    scored = [(url, score_image(url)) for url in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    
    for url, _ in scored:
        test_urls = [url]
        upgraded = upgrade_image_url(url)
        if upgraded != url: test_urls.insert(0, upgraded)
        
        for test_url in test_urls:
            if validate_image_url(test_url)[0]:
                return test_url
    return scored[0][0] if scored else candidates[0]

# ===== NEWS RSS AGENT =====

class NewsRSSAgent:
    def __init__(self):
        self.today_date = datetime.now().strftime("%d-%m-%Y")
        
    def format_date(self, entry: dict) -> str:
        parsed_time = entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed_time:
            try: return time.strftime("%d-%m-%Y", parsed_time)
            except: pass
        raw_date = entry.get('published') or entry.get('pubDate') or ''
        if not raw_date: return self.today_date
        try: return parsedate_to_datetime(raw_date).strftime("%d-%m-%Y")
        except:
            match = re.search(r'(\d{4}-\d{2}-\d{2})', raw_date)
            if match:
                y, m, d = match.group(1).split('-')
                return f"{d}-{m}-{y}"
            return self.today_date
        
    def is_article_fresh(self, entry: dict, max_age_days: int = 3) -> bool:
        parsed_time = entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed_time:
            try:
                pub_date = datetime(*parsed_time[:6])
                return (datetime.now() - pub_date).total_seconds() < (max_age_days * 86400)
            except: pass
        return True
        
    def fetch_rss_feeds(self, rss_sources: List[dict]) -> List[List[str]]:
        all_results = []
        print(f"📰 Starting RSS Feed Aggregation...")
        print("=" * 60)
        
        for source in rss_sources:
            url, name = source["url"], source["name"]
            max_articles = source.get("max_articles", 20)
            translate = source.get("translate_to_telugu", False)
            max_age = source.get("max_age_days", 3)
            prefix = name.replace(" Telugu", "").strip()
            
            print(f"\n🔄 Fetching from: {name} - Target: {max_articles} articles")
            
            try:
                headers = {'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'}
                fetch_url = f"{url}{'&' if '?' in url else '?'}_cb={int(time.time())}"
                response = requests.get(fetch_url, headers=headers, timeout=15)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                
                if not feed.entries:
                    print(f"⚠️ No entries found for {name}")
                    continue
                
                fetched_count, skipped_old = 0, 0
                
                for entry in feed.entries:
                    if fetched_count >= max_articles: break
                    try:
                        original_link = entry.get('link', '').strip()
                        
                        # Freshness filtering applies to The Guardian and The Hindu National
                        if name in ["The Guardian", "The Hindu National"] and not self.is_article_fresh(entry, max_age):
                            skipped_old += 1
                            continue
                        
                        raw_title = clean_text_for_translation(entry.get('title', 'No Title'))
                        raw_desc = entry.get('summary') or entry.get('description', '')
                        if not raw_desc or len(raw_desc) < 100:
                            if 'content' in entry and entry['content']: raw_desc = entry['content'][0].get('value', '')
                            
                        # Fetch richer descriptions for BBC, NTV, TV9, and The Hindu National
                        target_len = 150 if name in ["BBC Telugu", "TV9 Telugu", "The Hindu National"] else 100
                        max_para = 6 if name in ["BBC Telugu", "TV9 Telugu"] else 4
                        
                        if not raw_desc or len(clean_text_for_translation(raw_desc)) < target_len:
                            scraped = fetch_article_description_fallback(original_link, max_paragraphs=max_para)
                            if scraped: 
                                raw_desc = scraped
                                print(f"   🔗 Scraped rich description for: {raw_title[:40]}...")
                        
                        description = clean_text_for_translation(raw_desc)
                        
                        if should_skip_article(raw_title, raw_desc):
                            print(f"   ⏭️ Skipping unwanted (promo/temple/astro): {raw_title[:50]}...")
                            continue
                        
                        if len(description) < 20: description = "Read the full story for more details."
                        
                        # ===== EXPLICIT TRANSLATION BLOCK =====
                        if translate:
                            print(f"\n🌐 Translating {name} article {fetched_count + 1}...")
                            
                            # Translate Title
                            t_title, t1_success = translate_text_with_adaptive_chunking(raw_title, f"{name} Title", max_retries=3)
                            if t1_success:
                                print(f"   ✅ {name} title translated")
                                raw_title = t_title
                            else:
                                print(f"   ❌ {name} title translation failed after retries. Keeping original.")
                                
                            # Translate Description
                            t_desc, t2_success = translate_text_with_adaptive_chunking(description, f"{name} Description", max_retries=3)
                            if t2_success:
                                print(f"   ✅ {name} description translated")
                                description = t_desc
                            else:
                                print(f"   ❌ {name} description translation failed after retries. Keeping original.")
                        
                        if not description.startswith(f"{prefix}:"): description = f"{prefix}: {description}"
                        
                        candidates = extract_image_candidates(entry, url)
                        image_url = get_best_image(candidates)
                        article_date = self.format_date(entry)
                        
                        all_results.append([raw_title, description, image_url, article_date, original_link])
                        fetched_count += 1
                    except Exception as e:
                        continue
                        
                print(f"✅ Fetched {fetched_count} articles. (Skipped: {skipped_old} old)")
            except Exception as e:
                print(f"❌ Error fetching {name}: {e}")
        
        print(f"\n✅ Aggregation complete! Total: {len(all_results)} articles")
        return all_results

    def save_output(self, results: List[List[str]], filename: str = "telugu_news_output.json"):
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"💾 Saved to {filename}")

def main():
    agent = NewsRSSAgent()
    
    rss_sources = [
        {"url": "https://ntvtelugu.com/feed", "name": "NTV Telugu", "max_articles": 30, "translate_to_telugu": False},
        {"url": "https://www.thehindu.com/news/national/feeder/default.rss", "name": "The Hindu National", "max_articles": 15, "translate_to_telugu": True, "max_age_days": 2},
        {"url": "https://www.theguardian.com/world/rss", "name": "The Guardian", "max_articles": 10, "translate_to_telugu": True, "max_age_days": 2},
         {"url": "https://feeds.bbci.co.uk/telugu/rss.xml", "name": "BBC Telugu", "max_articles": 10, "translate_to_telugu": False}
    ]
    
    results = agent.fetch_rss_feeds(rss_sources)
    if not results:
        print("⚠️ No articles fetched.")
        exit(1)
    
    agent.save_output(results)

if __name__ == "__main__":
    main()
