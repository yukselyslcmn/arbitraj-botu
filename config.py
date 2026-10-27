import os
import re
from pathlib import Path
from dotenv import load_dotenv

# .env dosyasını yükle
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _derive_public_dashboard_url() -> str:
    """
    Her yerden (mobil veri dahil) erişilebilen GitHub Pages bulut paneli adresini üretir.
    Öncelik: PUBLIC_DASHBOARD_URL -> GITHUB_REPOSITORY (Actions) -> yerel .git/config origin adresi.
    """
    explicit = os.getenv("PUBLIC_DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo:
        try:
            git_cfg = (BASE_DIR / ".git" / "config").read_text(encoding="utf-8", errors="ignore")
            match = re.search(
                r"url\s*=\s*(?:https://github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?\s*$",
                git_cfg,
                re.MULTILINE,
            )
            if match:
                repo = match.group(1)
        except OSError:
            repo = ""

    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


# ==============================================================================
# 1. TELEGRAM VE DURUM HAFIZASI AYARLARI
# ==============================================================================
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DATA_DIR: Path = BASE_DIR / "data"
STATE_FILE_PATH: Path = DATA_DIR / "products_state.json"

# GitHub Pages bulut paneli (docs/ klasörü) ve yerel panel portu
PUBLIC_DASHBOARD_URL: str = _derive_public_dashboard_url()
STATIC_DASHBOARD_DIR: Path = BASE_DIR / "docs"
LOCAL_DASHBOARD_PORT: int = 8080
IS_GITHUB_ACTIONS: bool = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"

NOTIFY_ON_FIRST_RUN: bool = _get_bool("NOTIFY_ON_FIRST_RUN", False)
PRICE_DROP_RE_NOTIFY_PERCENT: float = _get_float("PRICE_DROP_RE_NOTIFY_PERCENT", 5.0)
TELEGRAM_FLOOD_SLEEP_SECONDS: float = _get_float("TELEGRAM_FLOOD_SLEEP_SECONDS", 1.5)

# ==============================================================================
# 2. HEDEF KATEGORİLER VE TAM / BUÇUKLU / KESİRLİ NUMARA KURALLARI
# ==============================================================================
# Erkek Ayakkabı Hedef Numaralar (41 - 46 arası tam ve buçuklu numaralar)
MEN_TARGET_SIZES: list[str] = [
    "41",
    "41.5",
    "42",
    "42.5",
    "43",
    "43.5",
    "44",
    "44.5",
    "45",
    "45.5",
    "46",
]

# Erkek Kesirli Numara Karşılıkları (Adidas, Salomon, New Balance vb.)
MEN_FRACTIONAL_SIZE_ALIASES: list[str] = [
    "41 1/3",
    "42 2/3",
    "43 1/3",
    "44 2/3",
    "45 1/3",
    "46 2/3",
]

# Kadın Ayakkabı Hedef Numaralar (37 - 40.5 arası tam ve buçuklu numaralar)
WOMEN_TARGET_SIZES: list[str] = [
    "37",
    "37.5",
    "38",
    "38.5",
    "39",
    "39.5",
    "40",
    "40.5",
]

# Kadın Kesirli Numara Karşılıkları
WOMEN_FRACTIONAL_SIZE_ALIASES: list[str] = [
    "37 1/3",
    "38 2/3",
    "39 1/3",
    "40 2/3",
]

ALL_TARGET_SIZES: set[str] = set(MEN_TARGET_SIZES) | set(WOMEN_TARGET_SIZES)

# ==============================================================================
# 3. ORİJİNAL HEDEF MARKALAR VE KARA LİSTE FİLTRELERİ
# ==============================================================================
ALLOWED_BRANDS: list[str] = [
    "Nike",
    "Adidas",
    "New Balance",
    "Lacoste",
    "Puma",
    "Vans",
    "Converse",
    "Salomon",
    "Asics",
    "Timberland",
    "The North Face",
    "Skechers",
    "Tommy Hilfiger",
    "Columbia",
    "Gant",
    "Nautica",
    "Helly Hansen",
    "Under Armour",
]

# Arbitraj puanlaması için marka likidite / popülerlik katsayıları (0-20 puan arası)
BRAND_POPULARITY_WEIGHTS: dict[str, int] = {
    "nike": 20,
    "adidas": 19,
    "new balance": 20,
    "salomon": 19,
    "asics": 18,
    "lacoste": 18,
    "the north face": 18,
    "timberland": 17,
    "puma": 15,
    "vans": 15,
    "converse": 15,
    "skechers": 16,
    "columbia": 17,
    "tommy hilfiger": 16,
    "helly hansen": 16,
    "under armour": 15,
    "gant": 15,
    "nautica": 14,
}

# Kara Liste Kelimeler (Başlık veya URL içinde geçerse elenir)
EXCLUDE_KEYWORDS: list[str] = [
    "terlik",
    "sandalet",
    "çorap",
    "bakım",
    "sprey",
    "bağcık",
    "tabanlık",
    "panduf",
    "deniz",
    "havuz",
    "çocuk",
    "bebek",
    "bebe",
    "kız çocuk",
    "erkek çocuk",
    " jr ",
    "-jr-",
    "junior",
    "şapka",
    "çanta",
    "tişört",
]

# ==============================================================================
# 4. TİCARİ ARBİTRAJ EŞİKLERİ VE BİRİM EKONOMİSİ SABİTLERİ (2026)
# ==============================================================================
MAX_BASKET_PRICE: float = _get_float("MAX_BASKET_PRICE", 5500.0)
MIN_TOTAL_DISCOUNT_RATE: float = _get_float("MIN_TOTAL_DISCOUNT_RATE", 40.0)
MIN_NET_PROFIT_TL: float = _get_float("MIN_NET_PROFIT_TL", 550.0)
MIN_ROI_PERCENT: float = _get_float("MIN_ROI_PERCENT", 30.0)

# Hızlı Satış Fiyat Çarpanı (Piyasa dip fiyatının %12 altı)
SUGGESTED_SELL_DISCOUNT_MULTIPLIER: float = 0.88

# Kargo ve Paketleme Sabit Gideri (TL)
SHIPPING_COST_TL: float = 95.0

# Senaryo 1: Gardrops / Sahibinden (Param Güvende) / Letgo Kesinti Oranı (%6)
GARDROPS_COMMISSION_RATE: float = 0.06

# Senaryo 2: Dolap Komisyon (%14) + E-Ticaret Stopajı (%1) = %15
DOLAP_COMMISSION_RATE: float = 0.14
DOLAP_WITHHOLDING_TAX_RATE: float = 0.01

# ==============================================================================
# 5. AĞ, KAZIYICI (SCRAPER) VE HEDEF KATEGORİ URL YAPILANDIRMASI
# ==============================================================================
REQUEST_TIMEOUT: int = _get_int("REQUEST_TIMEOUT", 20)
MAX_RETRIES: int = _get_int("MAX_RETRIES", 2)
MIN_DELAY_SECONDS: float = _get_float("MIN_DELAY_SECONDS", 0.35)
MAX_DELAY_SECONDS: float = _get_float("MAX_DELAY_SECONDS", 0.85)
MAX_PAGES_PER_CATEGORY: int = _get_int("MAX_PAGES_PER_CATEGORY", 4)
MAX_WORKERS: int = _get_int("MAX_WORKERS", 5)
LOOP_INTERVAL_SECONDS: int = _get_int("LOOP_INTERVAL_SECONDS", 900)
HTTP_PROXY: str = os.getenv("HTTP_PROXY", "").strip()

# Hedef Siteler ve Taranacak İndirim / Outlet Kategori Uç Noktaları
SITE_TARGETS: dict[str, dict] = {
    "occasion": {
        "name": "Occasion",
        "base_url": "https://www.occasion.com.tr",
        "categories": [
            {"url": "https://www.occasion.com.tr/erkek-ayakkabi/", "gender": "Erkek"},
            {"url": "https://www.occasion.com.tr/kadin-ayakkabi/", "gender": "Kadın"},
        ],
    },
    "superstep": {
        "name": "SuperStep",
        "base_url": "https://www.superstep.com.tr",
        "categories": [
            {"url": "https://www.superstep.com.tr/indirim-erkek-ayakkabi/", "gender": "Erkek"},
            {"url": "https://www.superstep.com.tr/indirim-kadin-ayakkabi/", "gender": "Kadın"},
            {"url": "https://www.superstep.com.tr/erkek-sneaker/", "gender": "Erkek"},
            {"url": "https://www.superstep.com.tr/kadin-sneaker/", "gender": "Kadın"},
        ],
    },
    "instreet": {
        "name": "InStreet",
        "base_url": "https://www.instreet.com.tr",
        "categories": [
            {
                "url": (
                    "https://www.instreet.com.tr/sneaker"
                    "?marka=nike,adidas,puma,new-balance,skechers,vans,converse,asics,lacoste,salomon"
                ),
                "gender": "Unisex",
            },
            {
                "url": (
                    "https://www.instreet.com.tr/spor-ayakkabi"
                    "?cinsiyet=erkek&marka=nike,adidas,puma,new-balance,skechers,vans,converse,asics,lacoste,salomon,timberland,under-armour,columbia,the-north-face,tommy-hilfiger"
                ),
                "gender": "Erkek",
            },
            {
                "url": (
                    "https://www.instreet.com.tr/spor-ayakkabi"
                    "?cinsiyet=kadin&marka=nike,adidas,puma,new-balance,skechers,vans,converse,asics,lacoste,salomon,timberland,under-armour,columbia,the-north-face,tommy-hilfiger"
                ),
                "gender": "Kadın",
            },
        ],
    },
    "barcin": {
        "name": "Barçın",
        "base_url": "https://www.barcin.com",
        "categories": [
            {"url": "https://www.barcin.com/outlet/", "gender": "Unisex"},
            {"url": "https://www.barcin.com/erkek-spor-ayakkabi", "gender": "Erkek"},
            {"url": "https://www.barcin.com/kadin-spor-ayakkabi", "gender": "Kadın"},
        ],
    },
    "korayspor": {
        "name": "Korayspor",
        "base_url": "https://www.korayspor.com",
        "categories": [
            {"url": "https://www.korayspor.com/erkek-indirimli-urunler/", "gender": "Erkek"},
            {"url": "https://www.korayspor.com/kadin-indirimli-urunler/", "gender": "Kadın"},
            {"url": "https://www.korayspor.com/erkek-spor-ayakkabi/", "gender": "Erkek"},
            {"url": "https://www.korayspor.com/kadin-spor-ayakkabi/", "gender": "Kadın"},
        ],
    },
    "sportive": {
        "name": "Sportive",
        "base_url": "https://www.sportive.com.tr",
        "categories": [
            {"url": "https://www.sportive.com.tr/outlet/", "gender": "Unisex"},
            {"url": "https://www.sportive.com.tr/erkek-spor-ayakkabi/", "gender": "Erkek"},
            {"url": "https://www.sportive.com.tr/kadin-spor-ayakkabi/", "gender": "Kadın"},
        ],
    },
    "fashfed": {
        "name": "FashFed",
        "base_url": "https://www.fashfed.com",
        "categories": [
            {"url": "https://www.fashfed.com/flash-indirim/", "gender": "Unisex"},
            {"url": "https://www.fashfed.com/erkek-ayakkabi/", "gender": "Erkek"},
            {"url": "https://www.fashfed.com/kadin-ayakkabi/", "gender": "Kadın"},
        ],
    },
}
