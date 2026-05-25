import os
import re
import csv
import time
import random
import signal
from datetime import datetime
from urllib.parse import urlparse, quote

from playwright.sync_api import sync_playwright


# ==================== НАСТРОЙКИ ====================

ACCOUNTS_FILE = "accounts.txt"
DONE_DIR = "Done parsing"

# Chrome должен быть заранее запущен так:
# chrome.exe --remote-debugging-port=9222
# и открыта вкладка opensea.io/collections (с применённым фильтром floor 0.05–0.75 ETH)
OPENSEA_CDP_URL = "http://127.0.0.1:9222"

OUTPUT_PROFILES_PREFIX = "opensea_x_profiles"
OUTPUT_DEBUG_PREFIX = "opensea_x_debug"

PROCESSED_OWNERS_FILE = "processed_owners.txt"
PROCESSED_COLLECTIONS_FILE = "processed_collections.txt"
PROCESSED_TWITTER_FILE = "processed_twitter.txt"
STOP_FILE = "STOP"

# Floor-фильтр коллекций (re-check на странице коллекции)
FLOOR_MIN_ETH = 0.05
FLOOR_MAX_ETH = 0.75

# Сколько раз скроллить грид items внутри коллекции,
# чтобы собрать все NFT (0 = до конца)
COLLECTION_GRID_MAX_SCROLLS = 0
COLLECTION_GRID_EMPTY_SCROLL_LIMIT = 8

# Сколько раз скроллить /collections в поисках следующей коллекции
COLLECTIONS_LIST_MAX_SCROLLS = 200
COLLECTIONS_LIST_EMPTY_SCROLL_LIMIT = 6

HEADLESS = False

# Proxy только для X/Twitter (как в dex-парсере)
PROXY_ENABLED = True
PROXY_SERVER = "http://127.0.0.1:8899"

# Задержки
OPENSEA_DELAY_MIN = 1.8
OPENSEA_DELAY_MAX = 3.2

OPENSEA_SCROLL_DELAY_MIN = 1.2
OPENSEA_SCROLL_DELAY_MAX = 2.4

X_PROFILE_DELAY_MIN = 4.0
X_PROFILE_DELAY_MAX = 7.0

X_SEARCH_DELAY_MIN = 4.0
X_SEARCH_DELAY_MAX = 7.0

# Multi-account ротация
ACCOUNT_REST_TIME = 600  # 10 минут
PAGE_LOAD_WAIT_BEFORE_ROTATE = 30
MAX_RETRIES_PER_URL = 3

# Жёсткий лимит поисковых запросов на один X аккаунт за одну "смену".
# По достижении — аккаунт уходит на отдых, переключаемся на следующий.
# Счётчик сбрасывается, когда аккаунт возвращается с отдыха.
MAX_SEARCHES_PER_ACCOUNT = 25

# Сохраняем outputs каждые N обработанных owners
SAVE_OUTPUTS_EVERY = 10


BAD_X_HANDLES = {
    "x", "twitter", "premium", "verified", "verifiedorgs", "ads", "business",
    "i", "intent", "share", "search", "home", "explore", "notifications",
    "messages", "settings", "compose", "download", "privacy", "tos", "login",
    "signup", "account", "flow", "logout", "display", "keyboard_shortcuts",
    "terms", "rules", "lists", "bookmarks", "status", "events", "hashtag",
    "communities", "help", "about", "music", "opensea",
}

# Пути / страницы OpenSea, которые НЕ являются профилем owner'а
BAD_OPENSEA_PATHS = {
    "collection", "collections", "item", "items", "rankings", "ranking",
    "drops", "drop", "activity", "explore", "trending", "category",
    "categories", "studio", "blog", "blogs", "stats", "membership", "learn",
    "help", "terms", "privacy", "press", "login", "register", "account",
    "settings", "watchlist", "portfolio", "create", "assets", "rewards",
    "search", "swap", "buy", "sell", "marketplace", "profile", "home",
    "tos", "about", "trade", "trades", "leaderboard", "leaderboards",
    "notifications", "messages", "wallet", "wallets", "connect", "discover",
    "favorites", "studios", "drops-calendar", "drops_calendar",
}


# ==================== GLOBAL ====================

saved_profiles = {}
debug_rows = []
visited_x_urls = set()
visited_owners_session = set()

stop_requested = False

# Установится в main() после создания AccountManager.
# Используется search-функциями для трекинга квоты 25 поисков на аккаунт.
_account_manager = None


def request_stop(signum=None, frame=None):
    global stop_requested
    if not stop_requested:
        print("\n🛑 STOP получен. Заканчиваю текущее, потом выхожу...")
        stop_requested = True


def check_stop_file():
    global stop_requested
    if os.path.exists(STOP_FILE):
        if not stop_requested:
            print(f"\n🛑 Найден файл {STOP_FILE} — останавливаюсь после текущего шага.")
        stop_requested = True


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


# ==================== HELPERS ====================

def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sleep_random(a, b):
    end = time.time() + random.uniform(a, b)
    while time.time() < end:
        check_stop_file()
        if stop_requested:
            return
        time.sleep(0.2)


def normalize_handle(handle):
    handle = clean_text(handle).replace("@", "").strip()
    handle = handle.split("?")[0].split("#")[0]
    handle = handle.strip("/")
    return handle


def normalize_x_url(url):
    url = clean_text(url)
    if not url:
        return ""
    url = url.replace("https://twitter.com/", "https://x.com/")
    url = url.replace("http://twitter.com/", "https://x.com/")
    url = url.replace("http://x.com/", "https://x.com/")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://x.com" + url
    url = url.split("?")[0].split("#")[0].rstrip("/")
    return url


def is_bad_handle(handle):
    h = normalize_handle(handle).lower()
    if not h:
        return True
    if h in BAD_X_HANDLES:
        return True
    if len(h) < 2 or len(h) > 30:
        return True
    if not re.match(r"^[A-Za-z0-9_]+$", h):
        return True
    return False


def extract_handle_from_x_url(url):
    url = normalize_x_url(url)
    try:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        first = path.split("/")[0]
        handle = normalize_handle(first)
        if is_bad_handle(handle):
            return ""
        return handle
    except Exception:
        return ""


def is_wallet(value):
    v = clean_text(value).lower()
    return bool(re.match(r"^0x[a-f0-9]{40}$", v))


def normalize_owner_id(value):
    v = clean_text(value).strip("/").split("?")[0].split("#")[0]
    if is_wallet(v):
        return v.lower()
    return v.lower()


def safe_page_text(page, timeout=3000):
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return ""


def page_has_x_challenge_or_login(page):
    try:
        url = page.url.lower()
        if (
            "login" in url
            or "account/access" in url
            or "challenge" in url
            or "flow/login" in url
        ):
            return True
        text = safe_page_text(page, timeout=3000).lower()
        challenge_words = [
            "sign in to x",
            "log in to x",
            "suspicious activity",
            "verify your identity",
            "confirm your identity",
            "something went wrong",
            "rate limit",
            "temporarily restricted",
            "your account has been locked",
        ]
        return any(w in text for w in challenge_words)
    except Exception:
        return False


def page_is_empty_or_not_loaded(page):
    try:
        text = safe_page_text(page, timeout=15000).strip()
        if len(text) < 100:
            return True
        not_loaded = [
            "too many requests",
            "slow down",
        ]
        low = text.lower()
        return any(p in low for p in not_loaded)
    except Exception:
        return True


def page_is_dead_x_profile(page):
    try:
        text = safe_page_text(page, timeout=3000).lower()
        dead_phrases = [
            "account suspended",
            "this account doesn't exist",
            "this account has been suspended",
            "user not found",
            "sorry, that page doesn't exist",
            "this profile doesn't exist",
            "account no longer active",
            "this account owner limits who can view their tweets",
            "your account is suspended",
        ]
        return any(p in text for p in dead_phrases)
    except Exception:
        return False


def add_debug(
    collection_url="",
    opensea_owner_url="",
    owner_id="",
    x_source_url="",
    result_type="",
    saved_profile="",
    reason="",
):
    debug_rows.append({
        "collection_url": collection_url,
        "opensea_owner_url": opensea_owner_url,
        "owner_id": owner_id,
        "x_source_url": x_source_url,
        "result_type": result_type,
        "saved_profile": saved_profile,
        "reason": reason,
        "parsed_at": now_str(),
    })


def save_profile_link(handle):
    handle = normalize_handle(handle)
    if not handle or is_bad_handle(handle):
        return ""
    final_url = f"https://x.com/{handle}"
    saved_profiles[final_url.lower()] = final_url
    return final_url


# ==================== PROCESSED FILES ====================

def load_set_from_file(path):
    result = set()
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            v = clean_text(line).lower()
            if v:
                result.add(v)
    return result


def append_to_file(path, value):
    value = clean_text(value).lower()
    if not value:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(value + "\n")


# ==================== ACCOUNTS ====================

def load_all_accounts():
    accounts = []
    if not os.path.exists(ACCOUNTS_FILE):
        raise FileNotFoundError(
            f"Создай файл {ACCOUNTS_FILE} с auth_token:ct0 (по одной паре на строку)"
        )
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            auth_token, ct0 = line.split(":", 1)
            auth_token = auth_token.strip()
            ct0 = ct0.strip()
            if auth_token and ct0:
                accounts.append({"auth_token": auth_token, "ct0": ct0})
    if not accounts:
        raise RuntimeError("В accounts.txt нет нормальных auth_token:ct0")
    print(f"📋 Загружено аккаунтов: {len(accounts)}")
    return accounts


class AccountManager:
    def __init__(self, accounts):
        self.accounts = accounts
        self.rest_until = [0.0] * len(accounts)
        self.rate_limit_count = [0] * len(accounts)
        self.search_counts = [0] * len(accounts)
        self.current_index = 0

    def get_current(self):
        return self.accounts[self.current_index]

    def mark_rate_limited(self, index=None):
        if index is None:
            index = self.current_index
        rest_until = time.time() + ACCOUNT_REST_TIME
        self.rest_until[index] = rest_until
        self.rate_limit_count[index] += 1
        # Сбрасываем счётчик поисков — после отдыха аккаунт начинает свежий цикл
        self.search_counts[index] = 0
        print(
            f"😴 Аккаунт #{index + 1} отдыхает до "
            f"{time.strftime('%H:%M:%S', time.localtime(rest_until))} "
            f"(~{ACCOUNT_REST_TIME // 60} мин) rate_limit #{self.rate_limit_count[index]}"
        )

    def note_search(self, index=None):
        if index is None:
            index = self.current_index
        self.search_counts[index] += 1
        return self.search_counts[index]

    def current_search_count(self):
        return self.search_counts[self.current_index]

    def quota_exhausted(self):
        return self.search_counts[self.current_index] >= MAX_SEARCHES_PER_ACCOUNT

    def next_available(self):
        n = len(self.accounts)
        now = time.time()
        for offset in range(1, n + 1):
            idx = (self.current_index + offset) % n
            if self.rest_until[idx] <= now:
                self.current_index = idx
                print(f"🔄 Переключаюсь на аккаунт #{idx + 1}")
                return idx
        min_rest = min(self.rest_until)
        wait_sec = max(0.0, min_rest - time.time()) + 2
        print(f"⏳ Все аккаунты отдыхают. Жду {wait_sec:.0f} сек...")
        end = time.time() + wait_sec
        while time.time() < end:
            check_stop_file()
            if stop_requested:
                break
            time.sleep(1)
        best_idx = min(range(n), key=lambda i: self.rest_until[i])
        self.current_index = best_idx
        print(f"🔄 Переключаюсь на аккаунт #{best_idx + 1} (после ожидания)")
        return best_idx

    def total(self):
        return len(self.accounts)


# ==================== FILES ====================

def get_output_paths():
    os.makedirs(DONE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    txt_path = os.path.join(DONE_DIR, f"{OUTPUT_PROFILES_PREFIX}_{stamp}.txt")
    csv_path = os.path.join(DONE_DIR, f"{OUTPUT_DEBUG_PREFIX}_{stamp}.csv")
    return txt_path, csv_path


def save_outputs(txt_path, csv_path):
    links = sorted(saved_profiles.values(), key=lambda x: x.lower())
    with open(txt_path, "w", encoding="utf-8") as f:
        for link in links:
            f.write(link + "\n")

    fieldnames = [
        "collection_url",
        "opensea_owner_url",
        "owner_id",
        "x_source_url",
        "result_type",
        "saved_profile",
        "reason",
        "parsed_at",
    ]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in debug_rows:
            writer.writerow(row)

    print("=" * 80)
    print("💾 SAVED")
    print(f"TXT: {txt_path}")
    print(f"CSV: {csv_path}")
    print(f"UNIQUE PROFILES: {len(links)}")
    print("=" * 80)


# ==================== PLAYWRIGHT ====================

def connect_opensea_context(p):
    print("🌐 OpenSea: подключаюсь к твоему Chrome через CDP")
    print(f"🔌 CDP: {OPENSEA_CDP_URL}")
    browser = p.chromium.connect_over_cdp(OPENSEA_CDP_URL)
    if not browser.contexts:
        raise RuntimeError(
            "Не найден Chrome context. Запусти Chrome с --remote-debugging-port=9222"
        )
    context = browser.contexts[0]
    return browser, context


def launch_x_browser(p):
    launch_kwargs = {
        "headless": HEADLESS,
        "args": [
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    if PROXY_ENABLED:
        launch_kwargs["proxy"] = {"server": PROXY_SERVER}
        print(f"🌐 X/Twitter proxy ON: {PROXY_SERVER}")
    browser = p.chromium.launch(**launch_kwargs)
    return browser


def create_x_context(browser, auth_token, ct0):
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="UTC",
    )
    cookies = [
        {"name": "auth_token", "value": auth_token, "domain": ".x.com",
         "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": ct0, "domain": ".x.com",
         "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"},
        {"name": "auth_token", "value": auth_token, "domain": ".twitter.com",
         "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "ct0", "value": ct0, "domain": ".twitter.com",
         "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"},
    ]
    context.add_cookies(cookies)
    context.set_extra_http_headers({
        "x-csrf-token": ct0,
        "accept-language": "en-US,en;q=0.9",
    })
    return context


def get_opensea_page(opensea_context):
    pages = opensea_context.pages
    opensea_pages = []
    for page in pages:
        try:
            url = page.url.lower()
        except Exception:
            continue
        if "opensea.io" in url:
            opensea_pages.append(page)
    if opensea_pages:
        return opensea_pages[-1]
    page = opensea_context.new_page()
    print("⚠️ OpenSea вкладка не найдена. Открываю новую.")
    page.goto(
        "https://opensea.io/collections",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    return page


def close_x_session(x_context, x_browser):
    try:
        x_context.close()
    except Exception:
        pass
    try:
        x_browser.close()
    except Exception:
        pass
    print("🔴 X браузер закрыт")


# ==================== OPENSEA: COLLECTIONS LIST ====================

def opensea_goto_collections(opensea_page):
    if "/collections" not in opensea_page.url.lower():
        opensea_page.goto(
            "https://opensea.io/collections",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        sleep_random(2.0, 3.5)


def parse_eth_floor(value):
    """
    "0.05 ETH" / "0.05 WETH" / "1,234 ETH" -> 0.05 / 1234.0
    "< 0.001 ETH" -> 0.001
    """
    if not value:
        return None
    s = clean_text(value)
    s = s.replace(",", "")
    m = re.search(r"([\d.]+)\s*([KkMmBb]?)\s*(?:ETH|WETH|Ξ)?", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    suffix = m.group(2).lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    elif suffix == "b":
        num *= 1_000_000_000
    return num


def collect_visible_opensea_collections(opensea_page):
    """
    Возвращает [{url, floor}] для коллекций на странице opensea.io/collections.
    Каждая строка в трендах содержит ссылку /collection/<slug> и floor price.
    """
    try:
        rows = opensea_page.evaluate(
            """
            () => {
                const result = [];
                const anchors = Array.from(document.querySelectorAll('a[href]'));

                const collMap = new Map();
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/^\\/collection\\/([A-Za-z0-9_\\-]+)/);
                    if (!m) continue;
                    const slug = m[1];
                    if (!collMap.has(slug)) {
                        collMap.set(slug, a);
                    }
                }

                for (const [slug, a] of collMap.entries()) {
                    // Идём вверх до контейнера-строки (содержит floor)
                    let row = a;
                    let floorText = '';
                    for (let i = 0; i < 8 && row; i++) {
                        const txt = (row.innerText || '').replace(/\\s+/g, ' ');
                        // Floor обычно в районе "0.05 ETH" / "0.05 WETH"
                        const fm = txt.match(/([\\d.,]+)\\s*(?:ETH|WETH|Ξ)\\b/i);
                        if (fm) {
                            // Берём первое — обычно это floor price
                            floorText = fm[0];
                            break;
                        }
                        row = row.parentElement;
                    }
                    result.push({
                        slug,
                        url: 'https://opensea.io/collection/' + slug,
                        floor: floorText,
                    });
                }
                return result;
            }
            """
        )
    except Exception as e:
        print(f"⚠️ Ошибка чтения списка коллекций: {e}")
        return []

    out = []
    seen = set()
    for r in rows or []:
        url = clean_text(r.get("url", ""))
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        floor_eth = parse_eth_floor(r.get("floor", ""))
        out.append({"url": url, "floor": floor_eth})
    return out


def scroll_opensea_list(opensea_page, strong=False):
    try:
        opensea_page.bring_to_front()
    except Exception:
        pass
    amount = random.randint(2500, 4000) if strong else random.randint(1000, 1800)
    try:
        opensea_page.evaluate(
            """
            (amount) => {
                const els = Array.from(document.querySelectorAll('*'));
                let best = null;
                let bestDelta = 0;
                for (const el of els) {
                    const delta = el.scrollHeight - el.clientHeight;
                    if (delta > bestDelta && delta > 300) {
                        best = el;
                        bestDelta = delta;
                    }
                }
                if (best) best.scrollTop += amount;
                window.scrollBy(0, amount);
            }
            """,
            amount,
        )
    except Exception:
        try:
            opensea_page.mouse.wheel(0, amount)
        except Exception:
            pass
    sleep_random(OPENSEA_SCROLL_DELAY_MIN, OPENSEA_SCROLL_DELAY_MAX)


def find_next_collection(opensea_page, processed_collections):
    """
    Скроллит /collections, пока не найдёт коллекцию:
      - не в processed_collections
      - floor в [FLOOR_MIN_ETH, FLOOR_MAX_ETH]
    """
    opensea_goto_collections(opensea_page)
    sleep_random(2.0, 4.0)

    seen = set()
    empty = 0
    for scroll_round in range(COLLECTIONS_LIST_MAX_SCROLLS):
        check_stop_file()
        if stop_requested:
            return None

        cards = collect_visible_opensea_collections(opensea_page)
        new_cards = [c for c in cards if c["url"].lower() not in seen]
        for c in new_cards:
            seen.add(c["url"].lower())

        if not new_cards:
            empty += 1
        else:
            empty = 0

        print(
            f"📋 Collections scroll {scroll_round + 1} | visible: {len(cards)} | "
            f"new: {len(new_cards)} | seen total: {len(seen)}"
        )

        for c in new_cards:
            if c["url"].lower() in processed_collections:
                continue
            if c["floor"] is None:
                continue
            if FLOOR_MIN_ETH <= c["floor"] <= FLOOR_MAX_ETH:
                print(f"🎯 Найдена коллекция: {c['url']} (floor {c['floor']} ETH)")
                return c["url"]

        if empty >= COLLECTIONS_LIST_EMPTY_SCROLL_LIMIT:
            print("✅ Список коллекций исчерпан")
            return None

        scroll_opensea_list(opensea_page, strong=(empty >= 2))

    return None


# ==================== OPENSEA: COLLECTION ITEMS ====================

def read_collection_floor(opensea_page):
    """Читает текущий floor коллекции со страницы /collection/<slug>."""
    try:
        floor_text = opensea_page.evaluate(
            """
            () => {
                const body = document.body.innerText || '';
                // Ищем "Floor 0.05 ETH" / "Floor price 0.05 ETH"
                const patterns = [
                    /floor\\s*price[^\\d]*([\\d.,]+)\\s*(ETH|WETH|Ξ)/i,
                    /floor[^\\d]*([\\d.,]+)\\s*(ETH|WETH|Ξ)/i,
                ];
                for (const p of patterns) {
                    const m = body.match(p);
                    if (m) return m[1] + ' ' + m[2];
                }
                return '';
            }
            """
        )
    except Exception:
        floor_text = ""
    return parse_eth_floor(floor_text)


def collect_item_urls(opensea_page):
    """
    Собирает все ссылки на /item/... из текущей страницы коллекции.
    """
    try:
        hrefs = opensea_page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => a.getAttribute('href')).filter(Boolean)",
        )
    except Exception:
        return []
    items = []
    seen = set()
    for href in hrefs:
        href = clean_text(href)
        if not href:
            continue
        m = re.match(r"^(?:https://opensea\.io)?(/item/[^?#]+)$", href)
        if not m:
            continue
        path = m.group(1)
        parts = path.strip("/").split("/")
        # /item/<chain>/<contract>/<token_id>
        if len(parts) < 4:
            continue
        full = "https://opensea.io" + path
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        items.append(full)
    return items


def scroll_collection_grid(opensea_page, strong=False):
    try:
        opensea_page.bring_to_front()
    except Exception:
        pass
    amount = random.randint(3000, 4500) if strong else random.randint(1400, 2200)
    try:
        opensea_page.evaluate(
            """
            (amount) => {
                const els = Array.from(document.querySelectorAll('*'));
                let best = null;
                let bestDelta = 0;
                for (const el of els) {
                    const delta = el.scrollHeight - el.clientHeight;
                    if (delta > bestDelta && delta > 300) {
                        best = el;
                        bestDelta = delta;
                    }
                }
                if (best) best.scrollTop += amount;
                window.scrollBy(0, amount);
            }
            """,
            amount,
        )
    except Exception:
        try:
            opensea_page.mouse.wheel(0, amount)
        except Exception:
            pass
    sleep_random(OPENSEA_SCROLL_DELAY_MIN, OPENSEA_SCROLL_DELAY_MAX)


# ==================== OPENSEA: ITEM → OWNER ====================

def open_item_and_get_owner_url(opensea_page, item_url):
    """
    Открывает страницу NFT. Возвращает URL owner'а (https://opensea.io/<id>) или "".
    Ищем элемент с прямым текстом "Owned by" и берём ближайшую СЛЕДУЮЩУЮ
    ссылку на профиль /<id>, не путая со swap/collection/прочими ссылками в шапке.
    """
    try:
        opensea_page.goto(item_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"⚠️ Не могу открыть NFT {item_url}: {e}")
        return ""
    sleep_random(OPENSEA_DELAY_MIN, OPENSEA_DELAY_MAX)

    bad_paths_js = sorted(BAD_OPENSEA_PATHS)

    try:
        owner_href = opensea_page.evaluate(
            """
            (badList) => {
                const bad = new Set(badList);

                function isOwnerHref(href) {
                    if (!href) return false;
                    const m = String(href).match(/^\\/([A-Za-z0-9_\\-\\.]+)\\/?$/);
                    if (!m) return false;
                    if (bad.has(m[1].toLowerCase())) return false;
                    return true;
                }

                // 1) Найти элементы, чей СОБСТВЕННЫЙ (не от детей) текст содержит "Owned by"
                const all = Array.from(document.body.querySelectorAll('*'));
                const ownedByEls = [];
                for (const el of all) {
                    let direct = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === 3) direct += node.textContent;
                    }
                    if (/owned\\s+by/i.test(direct)) {
                        ownedByEls.push(el);
                    }
                }

                // 2) Для каждого: пробуем найти ссылку профиля среди:
                //    a) собственных потомков,
                //    b) следующих sibling'ов,
                //    c) детей parentElement (но только тех, что идут ПОСЛЕ нашего el).
                function findOwnerLinkInScope(scope) {
                    const links = scope.querySelectorAll('a[href]');
                    for (const a of links) {
                        const href = a.getAttribute('href');
                        if (isOwnerHref(href)) return href;
                    }
                    return '';
                }

                for (const el of ownedByEls) {
                    // a) внутри самого el
                    let r = findOwnerLinkInScope(el);
                    if (r) return r;

                    // b) среди следующих sibling'ов
                    let sib = el.nextElementSibling;
                    for (let i = 0; i < 6 && sib; i++) {
                        if (sib.matches && sib.matches('a[href]')) {
                            const href = sib.getAttribute('href');
                            if (isOwnerHref(href)) return href;
                        }
                        const r2 = findOwnerLinkInScope(sib);
                        if (r2) return r2;
                        sib = sib.nextElementSibling;
                    }

                    // c) дети parentElement, идущие после el
                    if (el.parentElement) {
                        const kids = Array.from(el.parentElement.children);
                        const idx = kids.indexOf(el);
                        for (let i = idx + 1; i < kids.length && i < idx + 8; i++) {
                            if (kids[i].matches && kids[i].matches('a[href]')) {
                                const href = kids[i].getAttribute('href');
                                if (isOwnerHref(href)) return href;
                            }
                            const r3 = findOwnerLinkInScope(kids[i]);
                            if (r3) return r3;
                        }
                    }
                }

                return '';
            }
            """,
            bad_paths_js,
        )
    except Exception as e:
        print(f"⚠️ Ошибка извлечения owner на {item_url}: {e}")
        return ""

    if not owner_href:
        return ""
    if owner_href.startswith("/"):
        owner_href = "https://opensea.io" + owner_href

    # Защита: проверяем что в финальном URL первый path-сегмент не bad
    parsed = urlparse(owner_href)
    first_seg = parsed.path.strip("/").split("/")[0].lower() if parsed.path else ""
    if not first_seg or first_seg in BAD_OPENSEA_PATHS:
        return ""

    return owner_href


def open_owner_and_get_details(opensea_page, owner_url):
    """
    Открывает профиль owner'а. Возвращает (twitter_handle, opensea_id, wallet).
      twitter_handle = "" если ссылки нет на opensea профиле.
      opensea_id     = username или 0x-кошелёк из URL.
      wallet         = 0x... (40 hex), пустая строка если на странице не нашли.
                       Если opensea_id уже кошелёк — wallet = opensea_id.
    """
    try:
        opensea_page.goto(owner_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"⚠️ Не могу открыть owner {owner_url}: {e}")
        return "", "", ""
    sleep_random(OPENSEA_DELAY_MIN, OPENSEA_DELAY_MAX)

    # opensea_id из URL
    parsed = urlparse(opensea_page.url)
    opensea_id = parsed.path.strip("/").split("/")[0].lower()
    if not opensea_id or opensea_id in BAD_OPENSEA_PATHS:
        print(f"⚠️ После goto URL не похож на профиль: {opensea_page.url}")
        return "", "", ""

    # twitter link
    twitter_handle = ""
    try:
        x_links = opensea_page.eval_on_selector_all(
            "a[href]",
            """
            els => els.map(a => a.getAttribute('href'))
              .filter(h => h && /(?:x\\.com|twitter\\.com)\\//i.test(h))
            """
        )
    except Exception:
        x_links = []

    for link in x_links or []:
        handle = extract_handle_from_x_url(link)
        if handle:
            twitter_handle = handle
            break

    # wallet: если opensea_id это кошелёк — уже есть, иначе ищем на странице
    wallet = opensea_id if is_wallet(opensea_id) else ""

    if not wallet:
        try:
            # Источники: hrefs (etherscan/blockscan), aria-label, текст body
            page_data = opensea_page.evaluate(
                """
                () => {
                    const parts = [];
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) parts.push(a.getAttribute('href') || '');
                    const ariaEls = Array.from(document.querySelectorAll('[aria-label]'));
                    for (const e of ariaEls) parts.push(e.getAttribute('aria-label') || '');
                    parts.push(document.body.innerText || '');
                    return parts.join(' ');
                }
                """
            )
        except Exception:
            page_data = ""
        m = re.search(r"0x[a-fA-F0-9]{40}", page_data or "")
        if m:
            wallet = m.group(0).lower()

    return twitter_handle, opensea_id, wallet


# ==================== X PAGE LOAD WITH RETRY ====================

class RateLimitError(Exception):
    pass


def goto_x_page_with_retry(x_page_ref, url, rotate_fn, label="страница"):
    for attempt in range(1, MAX_RETRIES_PER_URL + 1):
        check_stop_file()
        if stop_requested:
            raise RateLimitError("stop_requested")

        x_page = x_page_ref[0]
        try:
            x_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            sleep_random(3.0, 5.0)
        except Exception as e:
            print(f"⚠️ goto timeout/error на {label} (попытка {attempt}/{MAX_RETRIES_PER_URL}): {e}")
            if attempt < MAX_RETRIES_PER_URL:
                print(f"⏳ Жду {PAGE_LOAD_WAIT_BEFORE_ROTATE} сек перед сменой аккаунта...")
                end = time.time() + PAGE_LOAD_WAIT_BEFORE_ROTATE
                while time.time() < end:
                    check_stop_file()
                    if stop_requested:
                        raise RateLimitError("stop_requested")
                    time.sleep(1)
                rotate_fn()
                continue
            raise RateLimitError(f"Не удалось загрузить {label} после {attempt} попыток")

        if page_has_x_challenge_or_login(x_page):
            print(f"🚨 Rate limit / challenge на {label} (попытка {attempt}/{MAX_RETRIES_PER_URL})")
            if attempt < MAX_RETRIES_PER_URL:
                rotate_fn()
                continue
            raise RateLimitError(f"Rate limit на {label} после {attempt} попыток")

        if page_is_empty_or_not_loaded(x_page):
            print(f"⚠️ Страница не загрузилась: {label} (попытка {attempt}/{MAX_RETRIES_PER_URL})")
            if attempt < MAX_RETRIES_PER_URL:
                print(f"⏳ Жду {PAGE_LOAD_WAIT_BEFORE_ROTATE} сек перед сменой аккаунта...")
                end = time.time() + PAGE_LOAD_WAIT_BEFORE_ROTATE
                while time.time() < end:
                    check_stop_file()
                    if stop_requested:
                        raise RateLimitError("stop_requested")
                    time.sleep(1)
                rotate_fn()
                continue
            raise RateLimitError(f"Страница не загрузилась после {attempt} попыток: {label}")
        return True

    raise RateLimitError(f"Не удалось загрузить {label}")


# ==================== X: PROFILE ALIVE CHECK ====================

def verify_x_profile_alive(x_page_ref, handle, rotate_fn):
    handle = normalize_handle(handle)
    if not handle or is_bad_handle(handle):
        return False
    url = f"https://x.com/{handle}"
    if url.lower() in visited_x_urls:
        # Кешируем результат как живой — мы его уже сохраняли
        return url.lower() in saved_profiles
    visited_x_urls.add(url.lower())

    print(f"🔎 Проверяю что @{handle} жив...")
    goto_x_page_with_retry(x_page_ref, url, rotate_fn, label=f"профиль @{handle}")
    x_page = x_page_ref[0]
    sleep_random(X_PROFILE_DELAY_MIN, X_PROFILE_DELAY_MAX)

    if page_is_dead_x_profile(x_page):
        print(f"💀 @{handle} suspended/deleted")
        return False
    return True


# ==================== X: SEARCH ====================

def ensure_search_quota(rotate_fn):
    """
    Перед каждым search-запросом проверяем квоту 25 поисков на текущий аккаунт.
    Если квота исчерпана — ротация (текущий уходит на отдых,
    его счётчик сбросится при mark_rate_limited).
    Цикл потому что новый аккаунт тоже может быть с уже использованной квотой
    в теории (например после возврата с отдыха — должен быть 0, но проверим).
    """
    if _account_manager is None:
        return
    while _account_manager.quota_exhausted():
        idx = _account_manager.current_index
        used = _account_manager.search_counts[idx]
        print(
            f"📉 Аккаунт #{idx + 1} исчерпал квоту поисков "
            f"({used}/{MAX_SEARCHES_PER_ACCOUNT}). Ротация."
        )
        rotate_fn()


def x_search_people(x_page_ref, query, rotate_fn):
    """
    Открывает x.com/search?q=<query>&f=user, возвращает список handle'ов
    в порядке появления (только видимые People-карточки).
    """
    ensure_search_quota(rotate_fn)
    q = quote(query)
    url = f"https://x.com/search?q={q}&src=typed_query&f=user"

    if _account_manager is not None:
        used_before = _account_manager.note_search()
        acc_idx = _account_manager.current_index
        print(
            f"🔎 X поиск (people) [acc #{acc_idx + 1} "
            f"{used_before}/{MAX_SEARCHES_PER_ACCOUNT}]: {query}"
        )
    else:
        print(f"🔎 X поиск (people): {query}")

    goto_x_page_with_retry(x_page_ref, url, rotate_fn, label=f"search:{query}")
    x_page = x_page_ref[0]
    sleep_random(X_SEARCH_DELAY_MIN, X_SEARCH_DELAY_MAX)

    handles = []
    try:
        handles = x_page.evaluate(
            """
            () => {
                const cells = Array.from(document.querySelectorAll('[data-testid="UserCell"]'));
                const out = [];
                for (const cell of cells) {
                    const anchors = Array.from(cell.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/^\\/([A-Za-z0-9_]{2,30})(?:\\?|\\/|#|$)/);
                        if (m) {
                            out.push(m[1]);
                            break;
                        }
                    }
                }
                return out;
            }
            """
        )
    except Exception as e:
        print(f"⚠️ Не удалось прочитать результаты поиска: {e}")

    result = []
    seen = set()
    for h in handles or []:
        h = normalize_handle(h)
        if not h or is_bad_handle(h):
            continue
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        result.append(h)
    return result


def x_search_top_latest(x_page_ref, query, rotate_fn):
    """
    Открывает x.com/search?q=<query>&f=top — берёт автора первого видимого твита.
    Используется как fallback для поиска по кошельку.
    """
    ensure_search_quota(rotate_fn)
    q = quote(query)
    url = f"https://x.com/search?q={q}&src=typed_query"

    if _account_manager is not None:
        used_before = _account_manager.note_search()
        acc_idx = _account_manager.current_index
        print(
            f"🔎 X поиск (top) [acc #{acc_idx + 1} "
            f"{used_before}/{MAX_SEARCHES_PER_ACCOUNT}]: {query}"
        )
    else:
        print(f"🔎 X поиск (top): {query}")

    goto_x_page_with_retry(x_page_ref, url, rotate_fn, label=f"search_top:{query}")
    x_page = x_page_ref[0]
    sleep_random(X_SEARCH_DELAY_MIN, X_SEARCH_DELAY_MAX)

    handle = ""
    try:
        handle = x_page.evaluate(
            """
            () => {
                const tweets = Array.from(document.querySelectorAll('article'));
                for (const t of tweets) {
                    const anchors = Array.from(t.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/^\\/([A-Za-z0-9_]{2,30})(?:\\/status\\/|$)/);
                        if (!m) continue;
                        const first = m[1];
                        // Пропускаем сервисные пути
                        const bad = ['i','intent','search','home','explore','notifications','messages','settings'];
                        if (bad.includes(first.toLowerCase())) continue;
                        return first;
                    }
                }
                return '';
            }
            """
        )
    except Exception:
        handle = ""
    return normalize_handle(handle or "")


# ==================== FALLBACK SEARCH LOGIC ====================

def search_twitter_by_wallet(x_page_ref, wallet, rotate_fn,
                             collection_url, opensea_owner_url, owner_id_for_debug):
    """
    Ищет твиттер ТОЛЬКО по 0x-кошельку.
      1) People-таб по wallet -> первый кандидат.
      2) Если пусто -> Top-таб, автор первого твита.
    Перед возвратом верифицирует что профиль жив.
    Возвращает handle или "".
    """
    if not is_wallet(wallet):
        return ""

    people = x_search_people(x_page_ref, wallet, rotate_fn)
    candidate = ""
    if people:
        candidate = people[0]
        print(f"   People-таб → @{candidate}")
    else:
        print("   People-таб пуст, пробую Top-таб...")
        candidate = x_search_top_latest(x_page_ref, wallet, rotate_fn)
        if candidate:
            print(f"   Top-таб → @{candidate}")

    if not candidate or is_bad_handle(candidate):
        add_debug(
            collection_url=collection_url,
            opensea_owner_url=opensea_owner_url,
            owner_id=owner_id_for_debug,
            x_source_url=f"https://x.com/search?q={wallet}",
            result_type="wallet_search_no_match",
            saved_profile="",
            reason=f"wallet {wallet} search returned no usable handle",
        )
        return ""

    if not verify_x_profile_alive(x_page_ref, candidate, rotate_fn):
        add_debug(
            collection_url=collection_url,
            opensea_owner_url=opensea_owner_url,
            owner_id=owner_id_for_debug,
            x_source_url=f"https://x.com/{candidate}",
            result_type="wallet_search_candidate_dead",
            saved_profile="",
            reason=f"candidate @{candidate} suspended/dead",
        )
        return ""
    return candidate


# ==================== OWNER PROCESSING ====================

def process_owner(
    x_page_ref,
    rotate_fn,
    collection_url,
    opensea_owner_url,
    opensea_id,
    opensea_wallet,
    twitter_handle_from_opensea,
    processed_owners,
    processed_twitter,
):
    """
    Принимает owner_url, opensea_id, opensea_wallet, twitter_handle (может быть пустым).
    Если twitter не указан в OpenSea — ищет ТОЛЬКО по 0x-кошельку.
    Возвращает True если сохранили новый профиль.
    """
    if opensea_id in processed_owners:
        print(f"⏭️ Owner {opensea_id} уже обрабатывался — skip")
        return False

    saved_handle = ""

    if twitter_handle_from_opensea:
        print(f"🐦 OpenSea → twitter: @{twitter_handle_from_opensea}")
        try:
            alive = verify_x_profile_alive(
                x_page_ref, twitter_handle_from_opensea, rotate_fn
            )
        except RateLimitError as e:
            raise
        if alive:
            saved_handle = twitter_handle_from_opensea
        else:
            print(f"💀 @{twitter_handle_from_opensea} suspended/dead.")
            if is_wallet(opensea_wallet):
                print(f"   → fallback поиск по кошельку {opensea_wallet}")
                try:
                    saved_handle = search_twitter_by_wallet(
                        x_page_ref, opensea_wallet, rotate_fn,
                        collection_url, opensea_owner_url, opensea_id,
                    )
                except RateLimitError:
                    raise
            else:
                print(f"   ⏭️ кошелька нет — пропускаю owner {opensea_id}")
                add_debug(
                    collection_url=collection_url,
                    opensea_owner_url=opensea_owner_url,
                    owner_id=opensea_id,
                    x_source_url="",
                    result_type="no_wallet_for_search",
                    saved_profile="",
                    reason="opensea twitter dead and no 0x wallet available",
                )
                return False
    else:
        if is_wallet(opensea_wallet):
            print(f"🐦 У OpenSea профиля {opensea_id} нет твиттера → поиск по кошельку {opensea_wallet}")
            try:
                saved_handle = search_twitter_by_wallet(
                    x_page_ref, opensea_wallet, rotate_fn,
                    collection_url, opensea_owner_url, opensea_id,
                )
            except RateLimitError:
                raise
        else:
            print(f"⏭️ Owner {opensea_id}: нет twitter и нет 0x-кошелька — skip")
            add_debug(
                collection_url=collection_url,
                opensea_owner_url=opensea_owner_url,
                owner_id=opensea_id,
                x_source_url="",
                result_type="no_wallet_for_search",
                saved_profile="",
                reason="no twitter on opensea and no 0x wallet available",
            )
            return False

    if not saved_handle:
        add_debug(
            collection_url=collection_url,
            opensea_owner_url=opensea_owner_url,
            owner_id=opensea_id,
            x_source_url="",
            result_type="no_twitter_found",
            saved_profile="",
            reason="wallet search returned nothing",
        )
        return False

    final_url = f"https://x.com/{saved_handle}".lower()
    if final_url in processed_twitter or final_url in saved_profiles:
        print(f"⏭️ @{saved_handle} уже сохранён — skip dup")
        add_debug(
            collection_url=collection_url,
            opensea_owner_url=opensea_owner_url,
            owner_id=opensea_id,
            x_source_url=final_url,
            result_type="duplicate_twitter_handle",
            saved_profile=final_url,
            reason="handle already in processed_twitter or saved_profiles",
        )
        return False

    saved = save_profile_link(saved_handle)
    if saved:
        print(f"✅ Saved: {saved}")
        processed_twitter.add(final_url)
        append_to_file(PROCESSED_TWITTER_FILE, final_url)
        add_debug(
            collection_url=collection_url,
            opensea_owner_url=opensea_owner_url,
            owner_id=opensea_id,
            x_source_url=f"https://x.com/{saved_handle}",
            result_type=(
                "opensea_linked_twitter_alive"
                if twitter_handle_from_opensea and saved_handle.lower() == twitter_handle_from_opensea.lower()
                else "twitter_found_via_search"
            ),
            saved_profile=saved,
            reason="ok",
        )
        return True

    return False


# ==================== COLLECTION PROCESSING ====================

def process_collection(
    opensea_page,
    x_page_ref,
    rotate_fn,
    collection_url,
    processed_owners,
    processed_twitter,
    txt_path,
    csv_path,
):
    """
    Обрабатывает одну коллекцию: проходит по всем NFT, для каждого NFT
    извлекает owner'а и пытается найти его twitter.
    Возвращает True если коллекция обработана до конца (можно записать в processed).
    """
    print("=" * 100)
    print(f"📦 Collection: {collection_url}")

    try:
        opensea_page.goto(collection_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"⚠️ Не могу открыть коллекцию: {e}")
        return False
    sleep_random(2.5, 4.5)

    floor = read_collection_floor(opensea_page)
    if floor is None:
        print("⚠️ Не удалось прочитать floor. Пробую дальше всё равно.")
    else:
        print(f"📊 Floor коллекции: {floor} ETH")
        if not (FLOOR_MIN_ETH <= floor <= FLOOR_MAX_ETH):
            print(
                f"⏭️ Floor {floor} ETH вне [{FLOOR_MIN_ETH}; {FLOOR_MAX_ETH}] — skip"
            )
            add_debug(
                collection_url=collection_url,
                opensea_owner_url="",
                owner_id="",
                x_source_url="",
                result_type="collection_floor_out_of_range",
                saved_profile="",
                reason=f"floor {floor} not in [{FLOOR_MIN_ETH};{FLOOR_MAX_ETH}]",
            )
            return True  # mark processed чтобы не возвращаться

    seen_items = set()
    visited_items = set()
    empty_scrolls = 0
    scroll_round = 0
    owners_processed_in_collection = 0

    while True:
        check_stop_file()
        if stop_requested:
            return False

        if (
            COLLECTION_GRID_MAX_SCROLLS > 0
            and scroll_round >= COLLECTION_GRID_MAX_SCROLLS
        ):
            print("🟡 COLLECTION_GRID_MAX_SCROLLS достигнут")
            break

        items = collect_item_urls(opensea_page)
        new_items = [it for it in items if it.lower() not in seen_items]
        for it in items:
            seen_items.add(it.lower())

        print(
            f"   grid scroll {scroll_round + 1} | visible_items: {len(items)} | "
            f"new: {len(new_items)} | seen: {len(seen_items)} | "
            f"owners done: {owners_processed_in_collection}"
        )

        if not new_items:
            empty_scrolls += 1
        else:
            empty_scrolls = 0

        # обрабатываем new_items
        for item_url in new_items:
            check_stop_file()
            if stop_requested:
                return False
            if item_url.lower() in visited_items:
                continue
            visited_items.add(item_url.lower())

            try:
                owner_href = open_item_and_get_owner_url(opensea_page, item_url)
            except Exception as e:
                print(f"⚠️ Ошибка обработки NFT {item_url}: {e}")
                continue

            if not owner_href:
                add_debug(
                    collection_url=collection_url,
                    opensea_owner_url="",
                    owner_id="",
                    x_source_url="",
                    result_type="no_owner_link_on_nft",
                    saved_profile="",
                    reason=f"item={item_url}",
                )
                continue

            opensea_id_raw = urlparse(owner_href).path.strip("/").split("/")[0]
            opensea_id_norm = normalize_owner_id(opensea_id_raw)

            if not opensea_id_norm or opensea_id_norm in BAD_OPENSEA_PATHS:
                continue

            if opensea_id_norm in processed_owners or opensea_id_norm in visited_owners_session:
                print(f"⏭️ Owner {opensea_id_norm} уже видели — skip")
                continue

            try:
                twitter_handle, opensea_id_from_profile, opensea_wallet = open_owner_and_get_details(
                    opensea_page, owner_href
                )
            except Exception as e:
                print(f"⚠️ Ошибка открытия owner {owner_href}: {e}")
                continue

            opensea_id_final = (
                opensea_id_from_profile
                if opensea_id_from_profile
                else opensea_id_norm
            )

            if not opensea_wallet and is_wallet(opensea_id_final):
                opensea_wallet = opensea_id_final.lower()

            if (
                opensea_id_final in processed_owners
                or opensea_id_final in visited_owners_session
            ):
                print(f"⏭️ Owner {opensea_id_final} уже обработан — skip")
                continue

            visited_owners_session.add(opensea_id_final)

            try:
                saved_new = process_owner(
                    x_page_ref,
                    rotate_fn,
                    collection_url,
                    owner_href,
                    opensea_id_final,
                    opensea_wallet,
                    twitter_handle,
                    processed_owners,
                    processed_twitter,
                )
            except RateLimitError as e:
                print(f"🚨 RateLimitError: {e}. Owner {opensea_id_final} не помечаю processed.")
                # сохраняем то что есть и продолжаем (rotate уже произошёл)
                save_outputs(txt_path, csv_path)
                if stop_requested:
                    return False
                continue
            except Exception as e:
                print(f"⚠️ Ошибка обработки owner {opensea_id_final}: {e}")
                add_debug(
                    collection_url=collection_url,
                    opensea_owner_url=owner_href,
                    owner_id=opensea_id_final,
                    x_source_url="",
                    result_type="owner_process_error",
                    saved_profile="",
                    reason=str(e),
                )

            processed_owners.add(opensea_id_final)
            append_to_file(PROCESSED_OWNERS_FILE, opensea_id_final)
            owners_processed_in_collection += 1

            if owners_processed_in_collection % SAVE_OUTPUTS_EVERY == 0:
                save_outputs(txt_path, csv_path)

            check_stop_file()
            if stop_requested:
                return False

        # вернёмся в коллекцию для следующего скролла
        try:
            current_url = opensea_page.url
        except Exception:
            current_url = ""
        if "/collection/" not in current_url:
            try:
                opensea_page.goto(
                    collection_url, wait_until="domcontentloaded", timeout=60000
                )
                sleep_random(2.0, 3.5)
            except Exception:
                pass

        if empty_scrolls >= COLLECTION_GRID_EMPTY_SCROLL_LIMIT:
            print("✅ Все NFT коллекции обработаны (новых нет)")
            break

        scroll_collection_grid(opensea_page, strong=(empty_scrolls >= 2))
        scroll_round += 1

    save_outputs(txt_path, csv_path)
    return True


# ==================== MAIN ====================

def main():
    global _account_manager
    txt_path, csv_path = get_output_paths()
    accounts = load_all_accounts()
    account_manager = AccountManager(accounts)
    _account_manager = account_manager

    processed_owners = load_set_from_file(PROCESSED_OWNERS_FILE)
    processed_collections = load_set_from_file(PROCESSED_COLLECTIONS_FILE)
    processed_twitter = load_set_from_file(PROCESSED_TWITTER_FILE)

    print("=" * 100)
    print("OPENSEA → X/TWITTER OWNER PARSER")
    print(f"Floor filter: {FLOOR_MIN_ETH} - {FLOOR_MAX_ETH} ETH")
    print(f"OpenSea via CDP: {OPENSEA_CDP_URL}")
    print(f"X proxy: {PROXY_SERVER} (enabled={PROXY_ENABLED})")
    print(f"Аккаунтов X: {account_manager.total()}")
    print(f"Лимит поисков на аккаунт: {MAX_SEARCHES_PER_ACCOUNT}")
    print(f"Already processed owners: {len(processed_owners)}")
    print(f"Already processed collections: {len(processed_collections)}")
    print(f"Already saved twitter handles: {len(processed_twitter)}")
    print(f"STOP file: {STOP_FILE} (создай чтобы остановиться)")
    print(f"OUTPUT TXT: {txt_path}")
    print(f"OUTPUT CSV: {csv_path}")
    print("=" * 100)

    # Удаляем старый STOP если есть
    if os.path.exists(STOP_FILE):
        try:
            os.remove(STOP_FILE)
            print(f"🗑️ Удалил старый {STOP_FILE} перед запуском")
        except Exception:
            pass

    with sync_playwright() as p:
        opensea_browser, opensea_context = connect_opensea_context(p)

        x_browser = launch_x_browser(p)
        current_account = account_manager.get_current()
        x_context = create_x_context(
            x_browser,
            current_account["auth_token"],
            current_account["ct0"],
        )
        _x_page = x_context.new_page()
        x_page_ref = [_x_page]

        opensea_page = get_opensea_page(opensea_context)

        def rotate_account_on_rate_limit():
            nonlocal x_browser, x_context
            current_idx = account_manager.current_index
            print(f"🚨 Rate limit на аккаунте #{current_idx + 1}! Закрываю X браузер...")
            close_x_session(x_context, x_browser)
            account_manager.mark_rate_limited(current_idx)
            account_manager.next_available()

            new_account = account_manager.get_current()
            new_idx = account_manager.current_index
            print(f"🆕 Открываю X браузер для аккаунта #{new_idx + 1}...")

            x_browser = launch_x_browser(p)
            x_context = create_x_context(
                x_browser,
                new_account["auth_token"],
                new_account["ct0"],
            )
            new_page = x_context.new_page()
            x_page_ref[0] = new_page

            print("🔎 Проверяю новый X аккаунт...")
            try:
                new_page.goto(
                    "https://x.com/home",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                sleep_random(5, 8)
                if page_has_x_challenge_or_login(new_page):
                    print(f"⚠️ Аккаунт #{new_idx + 1} тоже не работает — ротирую дальше...")
                    rotate_account_on_rate_limit()
                else:
                    print(f"✅ Аккаунт #{new_idx + 1} готов к работе")
            except Exception as e:
                print(f"⚠️ Ошибка при проверке нового аккаунта: {e}")

        try:
            print("🔎 Проверяю X login для аккаунта #1...")
            x_page_ref[0].goto(
                "https://x.com/home",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            sleep_random(5, 8)
            if page_has_x_challenge_or_login(x_page_ref[0]):
                print("⚠️ Первый X аккаунт не готов — пробую следующий...")
                rotate_account_on_rate_limit()
            else:
                print("✅ X открыт")

            print("=" * 100)
            print("В Chrome открой opensea.io/collections с фильтром по floor.")
            print(f"Скрипт сам пройдётся по коллекциям с floor в [{FLOOR_MIN_ETH}; {FLOOR_MAX_ETH}] ETH.")
            print("Для каждой NFT → owner → twitter (или поиск по wallet/username).")
            print(f"Чтобы остановить — создай файл {STOP_FILE} в этой папке.")
            print("=" * 100)
            input("Нажми ENTER, когда страница opensea.io/collections видна...")

            while True:
                check_stop_file()
                if stop_requested:
                    break

                next_url = find_next_collection(opensea_page, processed_collections)
                if not next_url:
                    print("✅ Коллекций больше не нашёл, заканчиваю")
                    break

                done = process_collection(
                    opensea_page,
                    x_page_ref,
                    rotate_account_on_rate_limit,
                    next_url,
                    processed_owners,
                    processed_twitter,
                    txt_path,
                    csv_path,
                )

                if done:
                    processed_collections.add(next_url.lower())
                    append_to_file(PROCESSED_COLLECTIONS_FILE, next_url.lower())
                    print(f"✅ Collection done: {next_url}")
                else:
                    # Остановка по STOP — коллекцию НЕ помечаем processed
                    print(f"⏸ Collection не помечена processed (остановка): {next_url}")

                save_outputs(txt_path, csv_path)

                if stop_requested:
                    break

            save_outputs(txt_path, csv_path)

        finally:
            close_x_session(x_context, x_browser)
            # opensea_browser/context принадлежит юзерскому Chrome — НЕ закрываем

    print("=" * 100)
    print("✅ ГОТОВО / ОСТАНОВЛЕНО")
    print(f"Уникальных X профилей сохранено за сессию: {len(saved_profiles)}")
    print(f"TXT: {txt_path}")
    print(f"CSV debug: {csv_path}")
    if os.path.exists(STOP_FILE):
        try:
            os.remove(STOP_FILE)
            print(f"🗑️ Удалил {STOP_FILE}")
        except Exception:
            pass
    print("=" * 100)


if __name__ == "__main__":
    main()
