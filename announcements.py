import requests
import json
import time
import os
import math
from datetime import datetime, timezone
import logging

LOG_FILE = "announcements.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

BASE_API_URL = "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
ARTICLE_DETAILS_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
ANNOUNCEMENTS_DIRECTORY = "announcements"
MAX_ARTICLES_PER_REQUEST = 10 
DELAY_TIME = 3
INTERVAL = 60
BASE_ARTICLE_URL = "https://www.binance.com/en/support/announcement/"

#these details were provided to spoof the binance server
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.binance.com/en/support/announcement/",
    "Origin": "https://www.binance.com",
}

#reusable HTTP session for connection pooling and TCP keep alive
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

#these are the catalogId values for Binance announcements
CATEGORIES = [
    {"id": 48,  "name": "New Cryptocurrency Listing"},
    {"id": 49,  "name": "Latest Binance News"},
    {"id": 51,  "name": "API Updates"},
    {"id": 93,  "name": "Latest Activities"},
    {"id": 128, "name": "Crypto Airdrop"},
    {"id": 157, "name": "Maintenance Updates"},
    {"id": 161, "name": "Delisting"},
]

#helper function to get current UTC time
def now_utc() -> str:
    """Returns current UTC time in readable string format."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

#helper function to convert timestamp to readable string
def ts_to_str(ts_ms) -> str:
    """Converts timestamp in milliseconds to a readable UTC string."""
    if not ts_ms:
        return "N/A"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

#helper function to get the folder path for a category
def get_category_directory(category_name: str) -> str:
    """Gets the folder path for a category."""
    return os.path.join(ANNOUNCEMENTS_DIRECTORY, category_name)

#helper function to create a file path for a category
def get_category_filepath(category_name: str) -> str:
    """Gets the specific JSON file path for a category inside its dedicated folder."""
    cat_dir = get_category_directory(category_name)
    safe_name = category_name.lower().replace(" ", "_")
    return os.path.join(cat_dir, f"{safe_name}.json")
    
#helper function to load category data
def load_category_data(filepath: str) -> dict | None:
    """Safely loads existing JSON file for a category."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Failed to read {filepath}: {e}")
        return None

#helper function to save category data atomically
def save_category_data(filepath: str, data: dict):
    """Saves category data to JSON file atomically to prevent data corruption."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    temp_filepath = f"{filepath}.tmp"
    try:
        with open(temp_filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
    except Exception as e:
        logging.error(f"Failed to save {filepath} atomically: {e}")
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)

#helper function to merge and deduplicate articles
def merge_and_deduplicate(existing_articles: list, new_articles: list) -> list:
    """Merges two lists of articles and returns a new list with no duplicates."""
    seen_ids = set()
    deduped = []

    for article in new_articles + existing_articles:
        art_id = article.get("id")
        
        if art_id:
            if art_id not in seen_ids:
                seen_ids.add(art_id)
                deduped.append(article)
        else:
            deduped.append(article)

    deduped.sort(key=lambda x: -x.get("release_date", 0))
    return deduped

#helper function to fetch a page from the Binance API
def fetch_page(category_id: int, page_no: int = 1) -> dict | None:
    """Fetches a specific page from Binance API with error handling."""
    params = {
        "type":      1,
        "pageNo":    page_no,
        "pageSize":  MAX_ARTICLES_PER_REQUEST,
        "catalogId": category_id,
    }
    
    try:
        resp = SESSION.get(BASE_API_URL, params=params, timeout=10)
        if resp.status_code == 429:
            logging.warning(f"Hit Binance rate limit (429) on page {page_no}. Pausing for 5 seconds...")
            time.sleep(5)
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        logging.warning(f"API request failed (Cat: {category_id}, Page: {page_no}): {e}")
        return None

#parse the article list and total count from Binance API response
def parse_articles(data: dict, category_name: str, category_id: int) -> tuple[list, int]:
    """Parses the article list and total count from Binance API response""" 
    if not data:
        return [], 0

    catalogs = data.get("data", {}).get("catalogs", [])

    if not catalogs:
        return [], 0

    catalog = catalogs[0]
    raw_articles = catalog.get("articles", [])
    total = catalog.get("total", 0)

    articles_out = []
    for article in raw_articles:
        article_id = article.get("id", "")
        title = article.get("title", "No Title")
        release_date = article.get("releaseDate", 0)
        code = article.get("code", "")
        link = f"{BASE_ARTICLE_URL}{code}"

        articles_out.append({
            "id":                    article_id,
            "title":                 title,
            "code":                  code,
            "link":                  link,
            "category":              category_name,
            "category_id":           category_id,
            "release_date":          release_date,
            "release_date_readable": ts_to_str(release_date),
        })

    return articles_out, total

#helper function to parse Binance rich text AST into clean text
def ast_to_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("node") == "text":
        return node.get("text", "")
    res = "".join(ast_to_text(c) for c in node.get("child", []))
    tag = node.get("tag", "")
    if tag in ["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]:
        return "\n" + res + "\n"
    return res

#helper function to parse Binance rich text AST into clean HTML
def ast_to_html(node) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("node") == "text":
        return node.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    tag = node.get("tag", "")
    inner = "".join(ast_to_html(c) for c in node.get("child", []))
    if not tag or tag == "root":
        return inner
    attrs = []
    for k, v in node.get("attr", {}).items():
        if k in ["href", "target", "src", "alt"]:
            attrs.append(f'{k}="{v}"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    return f"<{tag}{attr_str}>{inner}</{tag}>"

#helper function to fetch full announcement body from Binance API
def fetch_article_details(article_code: str) -> dict | None:
    """Fetches full content of an announcement by article code."""
    if not article_code:
        return None
    try:
        resp = SESSION.get(ARTICLE_DETAILS_URL, params={"articleCode": article_code}, timeout=10)
        if resp.status_code == 429:
            logging.warning("Hit Binance rate limit (429). Pausing for 5 seconds...")
            time.sleep(5)
            return None
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if not data:
            return None

        body_val = data.get("body", "{}")
        body_ast = json.loads(body_val) if isinstance(body_val, str) else body_val

        content_text = ast_to_text(body_ast).strip()
        content_html = ast_to_html(body_ast).strip()

        return {
            "id": data.get("id"),
            "code": article_code,
            "title": data.get("title", ""),
            "content_text": content_text,
            "content_html": content_html,
            "publish_date": data.get("publishDate", 0),
            "publish_date_readable": ts_to_str(data.get("publishDate", 0)),
            "seo_desc": data.get("seoDesc", ""),
            "fetched_at": now_utc(),
        }
    except Exception as e:
        logging.warning(f"Failed to fetch article details for '{article_code}': {e}")
        return None

#helper function to save full article details to the file
def save_article_details(category_name: str, article_code: str, detail_data: dict):
    """Saves full article details inside its category folder atomically."""
    cat_dir = get_category_directory(category_name)
    os.makedirs(cat_dir, exist_ok=True)
    filepath = os.path.join(cat_dir, f"{article_code}.json")
    temp_filepath = f"{filepath}.tmp"
    try:
        with open(temp_filepath, "w", encoding="utf-8") as f:
            json.dump(detail_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
    except Exception as e:
        logging.error(f"Failed to save details for {article_code} in {category_name}: {e}")
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)

#helper function to enrich and save full article details
def enrich_and_save_article_details(category_name: str, articles: list):
    """Fetches and saves full article details for each article if missing from disk."""
    cat_dir = get_category_directory(category_name)
    for art in articles:
        art_code = art.get("code")
        if not art_code:
            continue
        detail_path = os.path.join(cat_dir, f"{art_code}.json")
        if not os.path.exists(detail_path):
            detail = fetch_article_details(art_code)
            if detail:
                save_article_details(category_name, art_code, detail)
                art["content_text"] = detail.get("content_text", "")
                art["has_full_content"] = True
            time.sleep(DELAY_TIME)

CATEGORY_STATES = {} #stores the state of each category

#initialize category state
def get_or_init_category_state(cat: dict) -> dict:
    """Get or initialize state for a category"""

    cat_id = cat["id"]
    if cat_id in CATEGORY_STATES:
        return CATEGORY_STATES[cat_id]

    cat_name = cat["name"]
    file_path = get_category_filepath(cat_name)
    existing_data = load_category_data(file_path)

    if existing_data and existing_data.get("articles"):
        existing_articles = existing_data.get("articles", [])
        last_ts = existing_data.get("last_saved_timestamp", 0)
        history_completed = existing_data.get("history_completed", False)
        history_page = existing_data.get("history_page", 2)
        total_pages = existing_data.get("total_pages", 0)
        seen_ids = {a["id"] for a in existing_articles if a.get("id")}

        state = {
            "initialized":          True,
            "file_path":            file_path,
            "last_saved_timestamp": last_ts,
            "history_completed":    history_completed,
            "history_page":         history_page,
            "total_pages":          total_pages,
            "seen_ids":             seen_ids,
            "total_saved":          len(seen_ids),
        }
    else:
        state = {
            "initialized":          False,
            "file_path":            file_path,
            "last_saved_timestamp": 0,
            "history_completed":    False,
            "history_page":         2,
            "total_pages":          0,
            "seen_ids":             set(),
            "total_saved":          0,
        }

    CATEGORY_STATES[cat_id] = state
    return state

#fetches new announcements for a category
def fetch_new_announcements(cat_id: int, cat_name: str, state: dict) -> tuple[list, bool]:
    """Fetches all newly released announcements using timestamp bounded paging"""

    last_saved_ts = state["last_saved_timestamp"]
    seen_ids = state["seen_ids"]
    newly_found = []
    page = 1
    is_complete = True

    while True:
        data = fetch_page(cat_id, page_no=page)
        if data is None:
            logging.warning(f"Catchup aborted on page {page} for '{cat_name}'")
            is_complete = False
            break

        articles, _ = parse_articles(data, cat_name, cat_id)
        if not articles:
            break

        reached_known_history = False
        page_new_count = 0

        for a in articles:
            a_id = a.get("id")
            a_ts = a.get("release_date", 0)

            if a_id in seen_ids or (last_saved_ts > 0 and a_ts <= last_saved_ts):
                reached_known_history = True
                break
            else:
                newly_found.append(a)
                page_new_count += 1

        if reached_known_history or len(articles) < MAX_ARTICLES_PER_REQUEST:
            break

        if page_new_count == MAX_ARTICLES_PER_REQUEST:
            page += 1
            time.sleep(DELAY_TIME)
        else:
            break

    return newly_found, is_complete

#processes a single category
def process_category(cat: dict):
    """Processes a single category"""

    cat_id = cat["id"]
    cat_name = cat["name"]
    state = get_or_init_category_state(cat)
    file_path = state["file_path"]

    if not state["initialized"]:
        init_data = fetch_page(cat_id, page_no=1)
        if init_data is None:
            return

        first_articles, total = parse_articles(init_data, cat_name, cat_id)

        if total == 0 or not first_articles:
            return

        enrich_and_save_article_details(cat_name, first_articles)

        total_pages = max(1, math.ceil(total / MAX_ARTICLES_PER_REQUEST))
        last_ts = max((a["release_date"] for a in first_articles), default=0)
        is_history_done = (total_pages <= 1)
        next_hist_page = 2 if total_pages > 1 else 0

        save_payload = {
            "category":             cat_name,
            "category_id":          cat_id,
            "last_updated":         now_utc(),
            "last_saved_timestamp": last_ts,
            "last_saved_readable":  ts_to_str(last_ts),
            "history_completed":    is_history_done,
            "history_page":         next_hist_page,
            "total_pages":          total_pages,
            "total_saved":          len(first_articles),
            "articles":             first_articles,
        }
        save_category_data(file_path, save_payload)

        #update in memory state
        state["initialized"] = True
        state["last_saved_timestamp"] = last_ts
        state["history_completed"] = is_history_done
        state["history_page"] = next_hist_page
        state["total_pages"] = total_pages
        state["seen_ids"] = {a["id"] for a in first_articles if a.get("id")}
        state["total_saved"] = len(state["seen_ids"])

        if not is_history_done:
            return

    last_saved_ts = state["last_saved_timestamp"]

    brand_new_articles, catchup_complete = fetch_new_announcements(cat_id, cat_name, state)

    if not catchup_complete:
        return

    #load existing category data once into memory to avoid repeated disk reads
    existing_data = load_category_data(file_path) or {}

    if brand_new_articles:
        #fetch and save full article details for each newly announced article
        enrich_and_save_article_details(cat_name, brand_new_articles)

        existing_articles = existing_data.get("articles", [])

        merged_articles = merge_and_deduplicate(existing_articles, brand_new_articles)
        highest_ts = max(a["release_date"] for a in brand_new_articles)

        existing_data["last_updated"] = now_utc()
        existing_data["last_saved_timestamp"] = max(last_saved_ts, highest_ts)
        existing_data["last_saved_readable"] = ts_to_str(existing_data["last_saved_timestamp"])
        existing_data["total_saved"] = len(merged_articles)
        existing_data["articles"] = merged_articles

        save_category_data(file_path, existing_data)

        #update in memory state
        state["last_saved_timestamp"] = existing_data["last_saved_timestamp"]
        state["seen_ids"].update(a["id"] for a in brand_new_articles if a.get("id"))
        state["total_saved"] = len(merged_articles)

        logging.info(f"[{cat_name}] Caught up {len(brand_new_articles)} new announcements with full content")

    if not state["history_completed"] and state.get("history_page", 0) >= 2:
        h_page = state["history_page"]
        hist_data = fetch_page(cat_id, page_no=h_page)

        if hist_data is None:
            return

        raw_hist, _ = parse_articles(hist_data, cat_name, cat_id)

        new_hist_items = [a for a in raw_hist if a.get("id") not in state["seen_ids"]]

        if new_hist_items:
            enrich_and_save_article_details(cat_name, new_hist_items)

            existing_articles = existing_data.get("articles", [])
            merged_articles = merge_and_deduplicate(existing_articles, new_hist_items)

            existing_data["last_updated"] = now_utc()
            existing_data["history_page"] = h_page + 1
            existing_data["total_saved"] = len(merged_articles)
            existing_data["articles"] = merged_articles

            if len(raw_hist) < MAX_ARTICLES_PER_REQUEST or (state.get("total_pages") and h_page >= state["total_pages"]):
                existing_data["history_completed"] = True
                state["history_completed"] = True
                logging.info(f"[{cat_name}] Historical backfill completed")

            save_category_data(file_path, existing_data)

            state["history_page"] = h_page + 1
            state["seen_ids"].update(a["id"] for a in new_hist_items if a.get("id"))
            state["total_saved"] = len(merged_articles)
        else:
            existing_data["history_completed"] = True
            save_category_data(file_path, existing_data)
            state["history_completed"] = True
            logging.info(f"[{cat_name}] Historical backfill completed")

    #backfill details for previously saved articles if any are missing on the folder
    if existing_data.get("articles"):
        missing_articles = [
            a for a in existing_data["articles"]
            if a.get("code") and not os.path.exists(os.path.join(get_category_directory(cat_name), f"{a['code']}.json"))
        ]
        if missing_articles:
            enrich_and_save_article_details(cat_name, missing_articles[:MAX_ARTICLES_PER_REQUEST])


def main():
    os.makedirs(ANNOUNCEMENTS_DIRECTORY, exist_ok=True)
    logging.info("Announcements fetcher started")

    try:
        while True:
            for i, cat in enumerate(CATEGORIES):
                if i > 0:
                    time.sleep(DELAY_TIME)
                try:
                    process_category(cat)
                except Exception as e:
                    logging.error(f"Error processing category '{cat.get('name', 'Unknown')}': {e}", exc_info=True)

            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        logging.info("Announcements fetcher stopped")
    finally:
        SESSION.close()


if __name__ == "__main__":
    main()