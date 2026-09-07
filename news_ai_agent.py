import feedparser
import json
import re
import requests
import time
import html
import random
import hashlib
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import List
from deep_translator import GoogleTranslator

# Global cache for the current execution to avoid redundant API calls
translation_cache = {}

# ===== TRANSLATION OPTIMIZATION FUNCTIONS (TARGET: TELUGU) =====

def clean_text_for_translation(text: str) -> str:
    """Cleans text to prevent translator crashes while preserving meaningful content."""
    if not text or not isinstance(text, str):
        return ""
    
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    text = re.sub(r'([.!?])\1{2,}', r'\1', text)
    text = re.sub(r'(\?|&)(utm_[^=]+|fbclid|gclid|ref|source)=[^&\s]+', '', text, flags=re.IGNORECASE)
    
    def truncate_long_urls(match):
        url = match.group(0)
        if len(url) > 150:
            return url[:140] + "...[link]"
        return url
    text = re.sub(r'https?://[^\s<>"]+', truncate_long_urls, text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def split_text_intelligently(text: str, max_length: int) -> list:
    """Splits text intelligently: paragraphs -> sentences -> words -> hard split."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
    current_chunk = ""
    
    for p in paragraphs:
        if len(current_chunk) + len(p) + 1 <= max_length:
            current_chunk += (" " + p) if current_chunk else p
        else:
            if current_chunk:
                chunks.append(current_chunk)
            
            if len(p) > max_length:
                sentences = re.split(r'(?<=[।.?!])\s+', p)
                current_chunk = ""
                for s in sentences:
                    if len(current_chunk) + len(s) + 1 <= max_length:
                        current_chunk += (" " + s) if current_chunk else s
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        
                        if len(s) > max_length:
                            words = s.split(' ')
                            current_chunk = ""
                            for w in words:
                                if len(current_chunk) + len(w) + 1 <= max_length:
                                    current_chunk += (" " + w) if current_chunk else w
                                else:
                                    if current_chunk:
                                        chunks.append(current_chunk)
                                    current_chunk = w
                        else:
                            current_chunk = s
            else:
                current_chunk = p
                
    if current_chunk:
        chunks.append(current_chunk)
    
    final_chunks = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        if len(c) > max_length:
            for i in range(0, len(c), max_length):
                final_chunks.append(c[i:i+max_length].strip())
        else:
            final_chunks.append(c)
            
    return final_chunks

def translate_text_with_adaptive_chunking(text: str, text_name: str, max_retries: int = 3) -> tuple:
    """Orchestrates translation with adaptive chunking, exponential backoff, jitter, caching, and graceful degradation."""
    if not text or not isinstance(text, str):
        return text, False
        
    cleaned = clean_text_for_translation(text)
    if not cleaned:
        return text, False
        
    chunk_sizes = [900, 700, 500, 300]
    
    for size in chunk_sizes:
        print(f"   {text_name}: Attempting translation with max chunk size {size}...")
        chunks = split_text_intelligently(cleaned, size)
        print(f"   {text_name}: Split into {len(chunks)} chunks")
        
        translated_chunks = []
        all_succeeded = True
        
        for i, chunk in enumerate(chunks, 1):
            chunk_name = f"{text_name} chunk {i} (len:{len(chunk)})"
            success = False
            
            for attempt in range(max_retries):
                try:
                    cache_key = hashlib.md5(chunk.encode('utf-8')).hexdigest()
                    if cache_key in translation_cache:
                        translated = translation_cache[cache_key]
                        success = True
                    else:
                        translator = GoogleTranslator(source='auto', target='te')
                        translated = translator.translate(chunk)
                        
                        if translated and isinstance(translated, str):
                            translated = translated.strip()
                            error_indicators = ['error 500', 'server error', 'bad request', 'internal server error', 'too many requests', 'service unavailable']
                            if any(err in translated.lower() for err in error_indicators):
                                raise ValueError(f"Translator returned error-like string: {translated[:50]}")
                            
                            translation_cache[cache_key] = translated
                            success = True
                    
                    if success:
                        if attempt > 0:
                            print(f"   {chunk_name}: SUCCESS (after {attempt} retries)")
                        else:
                            print(f"   {chunk_name}: SUCCESS")
                        translated_chunks.append(translated)
                        time.sleep(random.uniform(0.8, 2.0))
                        break
                        
                except Exception as e:
                    delay = (2 ** attempt) + random.uniform(0.5, 1.5)
                    print(f"   {chunk_name}: RETRY {attempt + 1}/{max_retries} ({type(e).__name__}, waiting {delay:.1f}s...)")
                    time.sleep(delay)
            
            if not success:
                all_succeeded = False
                break
                
        if all_succeeded:
            final_text = " ".join(translated_chunks)
            print(f"   {text_name}: SUCCESS")
            return final_text, True
            
        print(f"   {text_name}: Failed at size {size}, trying smaller chunk size...")
        
    print(f"   ❌ {text_name}: FAILED after all retries and fallback sizes.")
    print(f"   ↪ Keeping original text for this article.")
    return text, False

# ===== ASTROLOGY/HOROSCOPE FILTER =====

def is_astrology_news(title: str, description: str) -> bool:
    combined_text = f"{title} {description}".lower()
    astrology_keywords = [
        'horoscope', 'astrology', 'zodiac',
        'రాశి', 'జాతక', 'దినఫలం', 'వార ఫలం', 'మాస ఫలం', 'rasiphalalu', 'dinaphalam'
    ]
    for keyword in astrology_keywords:
        if keyword in combined_text:
            return True
    return False

# ===== SMART DESCRIPTION FALLBACK =====

def fetch_article_description_fallback(url: str) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        
        p_tags = re.findall(r'<p[^>]*>(.*?)</p>', response.text, re.IGNORECASE | re.DOTALL)
        clean_parts = []
        for p in p_tags:
            text = re.sub(r'<[^>]+>', ' ', p)
            text = html.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 50 and not any(bad in text.lower() for bad in ['advertisement', 'ప్రకటన', 'follow us', 'subscribe']):
                clean_parts.append(text)
                if len(clean_parts) >= 3:
                    break
        return " ".join(clean_parts)
    except Exception as e:
        print(f"   ⚠️ Fallback scrape failed for {url}: {e}")
        return ""

# ===== HIGH-QUALITY IMAGE EXTRACTION =====

def extract_guardian_image_candidates(entry: dict, base_url: str) -> List[str]:
    candidates = []
    def add_candidate(url):
        if not url: return
        url = str(url).strip()
        if url.startswith('//'): url = 'https:' + url
        elif url.startswith('/'):
            parsed_base = urlparse(base_url)
            url = f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
        if url.startswith('http://') or url.startswith('https://'):
            url = url.rstrip('.,)"\'')
            if url not in candidates:
                candidates.append(url)

    if 'media_content' in entry:
        for media in entry['media_content']: add_candidate(media.get('url', ''))
    if 'enclosures' in entry:
        for enc in entry['enclosures']: add_candidate(enc.get('href', ''))
    if 'media_thumbnail' in entry:
        for thumb in entry['media_thumbnail']: add_candidate(thumb.get('url', ''))
            
    content_text = ""
    if 'content' in entry and len(entry['content']) > 0:
        content_text = entry['content'][0].get('value', '')
    if not content_text:
        content_text = entry.get('summary', '') or entry.get('description', '')
        
    if content_text:
        img_matches = re.findall(r'<img[^>]+src=["\']?([^"\'>\s]+)["\']?', content_text, re.IGNORECASE)
        for img_src in img_matches: add_candidate(img_src)
            
    for key, value in entry.items():
        if isinstance(value, str) and ('guim.co.uk' in value or 'guardian.co.uk' in value or 'http' in value or 'cnn.com' in value):
            url_matches = re.findall(r'(https?://[^\s<>"\']+)', value, re.IGNORECASE)
            for url_match in url_matches: add_candidate(url_match)
    return candidates

def score_guardian_image(url: str) -> int:
    if not url or 'guim.co.uk' not in url.lower(): return -9999
    url_lower = url.lower()
    if any(bad in url_lower for bad in ['logo', 'icon', 'avatar', 'spacer', 'pixel', 'dot.gif']): return -9999
    score = 0
    if any(sw in url_lower for sw in ['width=140', 'width=120', 'width=100', 'width=180', 'width=200']): score -= 1000
    width_match = re.search(r'[?&]width=(\d+)', url_lower)
    if width_match:
        w = int(width_match.group(1))
        if w >= 1200: score += 300
        elif w >= 800: score += 200
        elif w >= 500: score += 100
        elif w <= 300: score -= 200
    dim_match = re.search(r'/(\d+)_(\d+)_(\d+)_(\d+)/', url_lower)
    if dim_match:
        max_dim = max(int(dim_match.group(3)), int(dim_match.group(4)))
        if max_dim >= 2000: score += 500
        elif max_dim >= 1000: score += 300
        elif max_dim >= 500: score += 100
    if '/master/' in url_lower: score += 200
    if url.startswith('https://'): score += 50
    if 'guim.co.uk' in url_lower: score += 100
    return score

def upgrade_guardian_image_url(url: str) -> str:
    if not url or 'guim.co.uk' not in url: return url
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    query_params['width'] = ['1200']
    query_params['quality'] = ['90']
    if 'auto' not in query_params: query_params['auto'] = ['format']
    if 'fit' not in query_params: query_params['fit'] = ['max']
    flat_params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}
    new_query = urlencode(flat_params, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)

def validate_image_url(url: str) -> tuple:
    if not url or not isinstance(url, str): return False, 0, ""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        if response.status_code == 200 and response.headers.get('Content-Type', '').lower().startswith('image/'):
            return True, response.status_code, response.headers.get('Content-Type', '')
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=True, stream=True)
        if response.status_code == 200 and response.headers.get('Content-Type', '').lower().startswith('image/'):
            return True, response.status_code, response.headers.get('Content-Type', '')
        return False, response.status_code, response.headers.get('Content-Type', '')
    except requests.RequestException:
        return False, 0, ""

def get_best_guardian_image(candidates: List[str]) -> str:
    guardian_candidates = [url for url in candidates if 'guim.co.uk' in url.lower()]
    if not guardian_candidates: return ""
    scored_candidates = [(url, score_guardian_image(url)) for url in guardian_candidates]
    scored_candidates.sort(key=lambda x: x[1], reverse=True)
    for url, score in scored_candidates:
        test_urls = [url]
        width_match = re.search(r'[?&]width=(\d+)', url.lower())
        if any(sw in url.lower() for sw in ['width=140', 'width=120', 'width=100', 'width=180', 'width=200']) or (width_match and int(width_match.group(1)) < 1000):
            upgraded_url = upgrade_guardian_image_url(url)
            if upgraded_url != url: test_urls.insert(0, upgraded_url)
        for test_url in test_urls:
            is_valid, status_code, content_type = validate_image_url(test_url)
            if is_valid: return test_url
    return scored_candidates[0][0] if scored_candidates else ""

# ===== NEWS RSS AGENT =====

class NewsRSSAgent:
    def __init__(self):
        self.today_date = datetime.now().strftime("%d-%m-%Y")
        self.image_placeholder = "https://i.ibb.co/placeholder.jpg"
        
    def extract_image_url(self, entry: dict, base_url: str) -> str:
        candidates = extract_guardian_image_candidates(entry, base_url)
        best_guardian = get_best_guardian_image(candidates)
        if best_guardian: return best_guardian
        for url in candidates:
            url_lower = url.lower()
            if any(bad_word in url_lower for bad_word in ['logo', 'icon', 'spacer', 'pixel', 'avatar', 'dot.gif']):
                continue
            is_valid, status_code, content_type = validate_image_url(url)
            if is_valid: return url
        return candidates[0] if candidates else self.image_placeholder

    def format_date(self, entry: dict) -> str:
        parsed_time = entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed_time:
            try: return time.strftime("%d-%m-%Y", parsed_time)
            except Exception: pass
        raw_date = entry.get('published') or entry.get('pubDate') or ''
        if not raw_date: return self.today_date
        try:
            dt = parsedate_to_datetime(raw_date)
            return dt.strftime("%d-%m-%Y")
        except (ValueError, TypeError, OverflowError):
            match = re.search(r'(\d{4}-\d{2}-\d{2})', raw_date)
            if match:
                y, m, d = match.group(1).split('-')
                return f"{d}-{m}-{y}"
            return self.today_date
        
    def fetch_rss_feeds(self, rss_sources: List[dict]) -> List[List[str]]:
        all_results = []
        print(f"📰 Starting RSS Feed Aggregation...")
        print("=" * 60)
        
        for source in rss_sources:
            url = source["url"]
            name = source["name"]
            max_articles = source.get("max_articles", 20)
            translate_to_telugu = source.get("translate_to_telugu", False)
            prefix = name.replace(" Telugu", "").strip()
            
            print(f"\n🔄 Fetching from: {name} - Target: {max_articles} articles")
            print(f"   🔗 Source URL: {url}")
            
            try:
                # 1. CACHE BUSTING: Force fresh feed from CDN
                cache_buster = int(time.time())
                separator = '&' if '?' in url else '?'
                fetch_url = f"{url}{separator}_cb={cache_buster}"
                
                # 2. STRICT NO-CACHE HEADERS
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache',
                    'Expires': '0'
                }
                response = requests.get(fetch_url, headers=headers, timeout=15)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                
                if not feed.entries:
                    print(f"⚠️ No entries found for {name}")
                    continue
                
                fetched_count = 0
                seen_links = set() # 3. DEDUPLICATION within this run
                
                for entry in feed.entries:
                    if fetched_count >= max_articles:
                        break
                        
                    try:
                        original_link = entry.get('link', '').strip()
                        
                        # Skip if we've already seen this exact article link in this run
                        if original_link in seen_links:
                            print(f"   ⏭️ Skipping duplicate: {original_link[:60]}...")
                            continue
                        seen_links.add(original_link)
                        
                        raw_title = entry.get('title', 'No Title Available')
                        title = clean_text_for_translation(raw_title)
                        if not title: title = "No Title Available"
                        
                        raw_desc = entry.get('summary') or entry.get('description', '')
                        if not raw_desc or len(raw_desc) < 100:
                            if 'content' in entry and len(entry['content']) > 0:
                                raw_desc = entry['content'][0].get('value', '')
                                
                        if not raw_desc or len(clean_text_for_translation(raw_desc)) < 100:
                            if original_link:
                                scraped_desc = fetch_article_description_fallback(original_link)
                                if scraped_desc:
                                    raw_desc = scraped_desc
                                    print(f"   🔗 Scraped rich description from webpage for: {title[:40]}...")
                        
                        description = clean_text_for_translation(raw_desc)
                        
                        if is_astrology_news(raw_title, raw_desc):
                            print(f"   ⏭️ Skipping astrology/horoscope article: {title[:50]}...")
                            continue
                        
                        if len(description) < 20:
                            description = "Read the full story for more details on this breaking news."
                        
                        if translate_to_telugu:
                            print(f"🌐 Translating {name} article {fetched_count + 1} to Telugu...")
                            title, title_success = translate_text_with_adaptive_chunking(title, "Title", max_retries=3)
                            description, desc_success = translate_text_with_adaptive_chunking(description, "Description", max_retries=3)
                            
                            if not title_success or not desc_success:
                                print(f"⚠️ {name} translation partially or fully failed for article {fetched_count + 1}. Keeping original English for failed parts.")
                        
                        if not description.startswith(f"{prefix}:"):
                            description = f"{prefix}: {description}"
                        
                        image_url = self.extract_image_url(entry, url)
                        article_date = self.format_date(entry)
                        
                        all_results.append([title, description, image_url, article_date, original_link])
                        fetched_count += 1
                    except Exception as e:
                        print(f"⚠️ Error processing individual article from {name}: {str(e)}")
                        continue
                        
                print(f"✅ Successfully fetched {fetched_count} valid, unique articles from {name}.")
            except Exception as e:
                print(f"❌ Error fetching RSS from {name}: {str(e)}")
                # Don't fail the entire workflow if one source fails
                continue
        
        print("\n" + "=" * 60)
        print(f"✅ Aggregation complete! Total articles collected: {len(all_results)}")
        return all_results

    def save_output(self, results: List[List[str]], filename: str = "telugu_news_output.json"):
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Output saved to {filename}")
    
    def print_output(self, results: List[List[str]]):
        print("\n" + "=" * 80)
        print("📰 SAMPLE FINAL NEWS OUTPUT (First 3 items)")
        print("=" * 80)
        for idx, article in enumerate(results[:3], 1):
            print(f"\n Article {idx}:")
            print(f"   🏷️  Title: {article[0][:60]}...")
            print(f"    Desc: {article[1][:100]}...")
            print(f"   ️  Image: {article[2]}")
            print(f"   📅 Date: {article[3]}")
            print(f"   🔗 Link: {article[4]}")
            print("-" * 80)

def main():
    agent = NewsRSSAgent()
    
    rss_sources = [
        {"url": "https://ntvtelugu.com/feed", "name": "NTV Telugu", "max_articles": 30, "translate_to_telugu": False},
        {"url": "https://www.theguardian.com/world/rss", "name": "The Guardian", "max_articles": 14, "translate_to_telugu": True}
    ]
    
    results = agent.fetch_rss_feeds(rss_sources)
    
    if not results:
        print("⚠️ No news articles were fetched. Please check the RSS URLs.")
        # Exit with error code to stop workflow
        exit(1)
    
    agent.print_output(results)
    agent.save_output(results, "telugu_news_output.json")

if __name__ == "__main__":
    main()
