import json
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator

# ===== PERFORMANCE OPTIMIZATION =====
MAX_WORKERS = 5  # Safe limit to avoid Google Translate rate limiting

# Thread-safe caching and request tracking
cache_lock = threading.Lock()
translation_cache = {}
request_counter_lock = threading.Lock()
translation_requests_count = 0

thread_local = threading.local()

def get_translator(target_lang):
    """Creates or retrieves a thread-local translator instance to ensure thread safety."""
    if not hasattr(thread_local, "translators"):
        thread_local.translators = {}
    if target_lang not in thread_local.translators:
        # Using source='auto' handles mixed-language text gracefully
        thread_local.translators[target_lang] = GoogleTranslator(source='auto', target=target_lang)
    return thread_local.translators[target_lang]

def get_from_cache(key):
    with cache_lock:
        return translation_cache.get(key)

def set_cache(key, value):
    with cache_lock:
        translation_cache[key] = value

def increment_requests():
    global translation_requests_count
    with request_counter_lock:
        translation_requests_count += 1
# ===== END PERFORMANCE OPTIMIZATION =====

def clean_text_for_translation(text):
    """Clean text by removing HTML entities and problematic characters while preserving meaningful content."""
    if not text or not isinstance(text, str):
        return ""
    
    # Remove HTML entities like &#8230;, &nbsp;, etc.
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    
    # Remove ONLY empty brackets to preserve meaningful content like [Exclusive] or (Updated)
    text = re.sub(r'\[\s*\]', '', text)
    text = re.sub(r'\(\s*\)', '', text)
    
    # Clean up multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def is_telugu(text):
    """Check if text contains Telugu Unicode characters."""
    return any('\u0C00' <= char <= '\u0C7F' for char in text)

def is_hindi(text):
    """Check if text contains Devanagari (Hindi) Unicode characters."""
    return any('\u0900' <= char <= '\u097F' for char in text)

def is_english(text):
    """Check if text is primarily English (ASCII alphabetic characters)."""
    if not text:
        return False
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return True  # If no alphabetic chars (e.g., just numbers/dates), skip translation
    ascii_alpha = sum(1 for c in alpha_chars if ord(c) < 128)
    return (ascii_alpha / len(alpha_chars)) > 0.8

def should_skip(text, target_lang):
    """Determine if translation can be safely skipped."""
    if not text or len(text.strip()) < 3:
        return True
    if target_lang == 'te' and is_telugu(text):
        return True
    if target_lang == 'hi' and is_hindi(text):
        return True
    if target_lang == 'en' and is_english(text):
        return True
    return False

def translate_single(text, target_lang):
    """Translates a single piece of text with exponential backoff and caching."""
    global translation_requests_count
    if not text:
        return text
        
    cleaned = clean_text_for_translation(text)
    if should_skip(cleaned, target_lang):
        return text  # Already in target language or too short
        
    key = (cleaned, target_lang)
    cached = get_from_cache(key)
    if cached:
        return cached
        
    for attempt in range(3):
        try:
            translator = get_translator(target_lang)
            increment_requests()
            result = translator.translate(cleaned)
            if result and len(result.strip()) > 0:
                set_cache(key, result.strip())
                return result.strip()
        except Exception as e:
            delay = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
            print(f"      Single translation attempt {attempt + 1} failed, retrying in {delay}s... ({str(e)[:40]})")
            time.sleep(delay)
            
    # Final fallback: Return cleaned original text
    return cleaned

def translate_combined(title, desc, target_lang):
    """Attempts to translate title and description in one request to save API calls."""
    global translation_requests_count
    
    cleaned_title = clean_text_for_translation(title)
    cleaned_desc = clean_text_for_translation(desc)
    
    # If both are empty or already in target language, skip entirely
    if should_skip(cleaned_title, target_lang) and should_skip(cleaned_desc, target_lang):
        return title, desc
        
    key_combined = (cleaned_title, cleaned_desc, target_lang)
    cached = get_from_cache(key_combined)
    if cached:
        return cached
        
    # Use a highly distinct delimiter that Google Translate typically preserves
    combined_text = f"<<<TITLE>>>\n{cleaned_title}\n<<<DESC>>>\n{cleaned_desc}"
    
    for attempt in range(3):
        try:
            translator = get_translator(target_lang)
            increment_requests()
            translated = translator.translate(combined_text)
            
            # Check if delimiter was preserved (Google Translate usually leaves <<<...>>> intact)
            if "<<<DESC>>>" in translated or "<<<desc>>>" in translated.lower():
                parts = re.split(r'<<<\s*DESC\s*>>>', translated, flags=re.IGNORECASE)
                if len(parts) >= 2:
                    t_res = parts[0].replace("<<<TITLE>>>", "").replace("<<<title>>>", "").strip()
                    d_res = parts[1].strip()
                    set_cache(key_combined, (t_res, d_res))
                    return t_res, d_res
            
            # If delimiter was mangled, force fallback to separate translations
            raise ValueError("Delimiter mangled by translation service")
            
        except Exception as e:
            delay = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
            print(f"      Combined translation attempt {attempt + 1} failed, retrying in {delay}s...")
            time.sleep(delay)
            
    # Fallback to separate translations if combined fails
    print("      Fallback: Translating title and description separately.")
    t_res = translate_single(cleaned_title, target_lang)
    d_res = translate_single(cleaned_desc, target_lang)
    set_cache(key_combined, (t_res, d_res))
    return t_res, d_res

def translate_article_job(idx, article):
    """Worker function to process a single article concurrently."""
    title_src = article[0]
    desc_src = article[1]
    image_url = article[2]
    date = article[3]
    original_link = article[4]
    
    # Translate to English and Hindi
    title_en, desc_en = translate_combined(title_src, desc_src, 'en')
    title_hi, desc_hi = translate_combined(title_src, desc_src, 'hi')
    
    # Determine success (did the text actually change from the cleaned original?)
    cleaned_title = clean_text_for_translation(title_src)
    cleaned_desc = clean_text_for_translation(desc_src)
    
    success_en = (title_en != cleaned_title) or (desc_en != cleaned_desc) or (should_skip(cleaned_title, 'en') and should_skip(cleaned_desc, 'en'))
    success_hi = (title_hi != cleaned_title) or (desc_hi != cleaned_desc) or (should_skip(cleaned_title, 'hi') and should_skip(cleaned_desc, 'hi'))
    
    return idx, {
        'en': [title_en, desc_en, image_url, date, original_link],
        'hi': [title_hi, desc_hi, image_url, date, original_link],
        'success_en': success_en,
        'success_hi': success_hi
    }

def translate_news_agent(input_file, english_output_file, hindi_output_file, telugu_output_file):
    print(f" Reading source news from: {input_file}")
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            source_news = json.load(f)
    except FileNotFoundError:
        print(f"❌ Error: File '{input_file}' not found. Please run the aggregator first.")
        raise
    except json.JSONDecodeError:
        print(f"❌ Error: '{input_file}' is not a valid JSON file.")
        raise

    total_articles = len(source_news)
    if total_articles == 0:
        print("⚠️ No articles to translate.")
        return

    print(f"🚀 Translating {total_articles} articles to English and Hindi using {MAX_WORKERS} workers...")
    print("=" * 60)

    results = {}
    start_time = time.time()

    # ===== PERFORMANCE OPTIMIZATION: Concurrent Execution =====
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all jobs
        futures = {executor.submit(translate_article_job, idx, article): idx for idx, article in enumerate(source_news)}
        
        # Process as they complete
        for future in as_completed(futures):
            idx = futures[future]
            try:
                res_idx, res_data = future.result()
                results[res_idx] = res_data
                print(f"✅ Article {res_idx + 1}/{total_articles} completed")
            except Exception as e:
                print(f"❌ Article {idx + 1}/{total_articles} failed critically: {e}")
                # Fallback to original source to preserve structure and order
                article = source_news[idx]
                results[idx] = {
                    'en': article,
                    'hi': article,
                    'success_en': False,
                    'success_hi': False
                }
    # ===== END PERFORMANCE OPTIMIZATION =====

    end_time = time.time()
    elapsed = end_time - start_time
    avg_time = elapsed / total_articles if total_articles > 0 else 0

    # Reconstruct lists in exact original order
    english_news = [results[i]['en'] for i in range(total_articles)]
    hindi_news = [results[i]['hi'] for i in range(total_articles)]
    
    successful_en = sum(1 for r in results.values() if r['success_en'])
    successful_hi = sum(1 for r in results.values() if r['success_hi'])
    failed_partial = sum(1 for r in results.values() if not r['success_en'] or not r['success_hi'])

    # Save English JSON
    with open(english_output_file, 'w', encoding='utf-8') as f:
        json.dump(english_news, f, ensure_ascii=False, indent=2)
    print(f"\n💾 English translation saved to: {english_output_file}")

    # Save Hindi JSON
    with open(hindi_output_file, 'w', encoding='utf-8') as f:
        json.dump(hindi_news, f, ensure_ascii=False, indent=2)
    print(f"💾 Hindi translation saved to: {hindi_output_file}")
    
    # Copy telugu output to newstelugu.json
    with open(telugu_output_file, 'w', encoding='utf-8') as f:
        json.dump(source_news, f, ensure_ascii=False, indent=2)
    print(f"💾 Telugu news saved to: {telugu_output_file}")
    
    print("\n" + "=" * 60)
    print("📊 Translation Summary:")
    print(f"   Total articles: {total_articles}")
    print(f"   Successful English: {successful_en}")
    print(f"   Successful Hindi: {successful_hi}")
    print(f"   Failed/partial: {failed_partial}")
    print(f"   Total translation requests: {translation_requests_count}")
    print(f"   Elapsed time: {elapsed:.2f} seconds")
    print(f"   Average time per article: {avg_time:.2f} seconds")
    print("🎉 Translation process completed!")

if __name__ == "__main__":
    # Updated filenames to match repository
    INPUT_FILE = "telugu_news_output.json"
    ENGLISH_OUTPUT = "english.json"
    HINDI_OUTPUT = "hindi.json"
    TELUGU_OUTPUT = "newstelugu.json"

    translate_news_agent(INPUT_FILE, ENGLISH_OUTPUT, HINDI_OUTPUT, TELUGU_OUTPUT)
