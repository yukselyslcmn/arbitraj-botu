import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from core.parsers import BasketPromotionParser, NormalizedSizeInfo, SizeNormalizer, SKUNormalizer

logger = logging.getLogger("arbitraj.scraper.base")


class BaseScraper(ABC):
    """
    Tüm yetkili distribütör e-ticaret kazıyıcıları için soyut (abstract) temel sınıf.
    """

    def __init__(
        self,
        site_key: str,
        http_client: Optional[StealthHttpClient] = None,
    ) -> None:
        if site_key not in config.SITE_TARGETS:
            raise ValueError(f"Tanımsız site anahtarı: {site_key}")

        self.site_key = site_key
        self.site_config = config.SITE_TARGETS[site_key]
        self.site_name: str = self.site_config["name"]
        self.base_url: str = self.site_config["base_url"]
        self.categories: list[dict[str, str]] = self.site_config["categories"]
        self.client = http_client or StealthHttpClient()

    def build_full_url(self, path_or_url: str) -> str:
        if not path_or_url:
            return self.base_url
        cleaned = str(path_or_url).strip()
        if cleaned.startswith("//"):
            return f"https:{cleaned}"
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned
        return urljoin(self.base_url + "/", cleaned.lstrip("/"))

    @staticmethod
    def infer_gender(title: str, url: str = "", default_gender: str = "Erkek") -> str:
        combined = f"{title} {url}".lower()
        if any(w in combined for w in ("kadın", "kadin", "women", "wmns", "wmn", "/kadin")):
            return "Kadın"
        if any(w in combined for w in ("erkek", "men", "/erkek")):
            return "Erkek"
        if "unisex" in combined:
            return "Unisex"
        return default_gender

    @staticmethod
    def infer_brand(raw_brand: str, title: str, url: str = "") -> str:
        if raw_brand and str(raw_brand).strip():
            matched = ArbitrageEngine.is_brand_allowed(str(raw_brand).strip(), title)
            if matched:
                return matched
            return str(raw_brand).strip()

        matched = ArbitrageEngine.is_brand_allowed("", f"{title} {url}")
        if matched:
            return matched

        # Başlığın ilk kelimesini marka adayı olarak al
        words = [w for w in re.split(r"\s+", title.strip()) if w]
        return words[0] if words else "Bilinmeyen"

    @staticmethod
    def extract_next_data(html_text: str) -> Optional[dict[str, Any]]:
        """HTML içindeki <script id="__NEXT_DATA__"> JSON verisini ayrıştırır."""
        if "__NEXT_DATA__" not in html_text:
            return None
        soup = BeautifulSoup(html_text, "lxml")
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            try:
                return json.loads(script_tag.string.strip())
            except Exception as exc:
                logger.debug("__NEXT_DATA__ BS4 JSON ayrıştırma hatası: %s", exc)
        match = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                return json.loads(match.group(1).strip())
            except Exception as exc:
                logger.debug("__NEXT_DATA__ Regex JSON ayrıştırma hatası: %s", exc)
        return None

    @staticmethod
    def extract_json_ld_products(html_text: str) -> list[dict[str, Any]]:
        """HTML içindeki application/ld+json bloklarından Product nesnelerini çıkarır."""
        soup = BeautifulSoup(html_text, "lxml")
        products: list[dict[str, Any]] = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw.strip())
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("@type", "")).lower()
                if item_type == "product":
                    products.append(item)
                elif item_type == "itemlist" and isinstance(item.get("itemListElement"), list):
                    for elem in item["itemListElement"]:
                        if isinstance(elem, dict):
                            inner = elem.get("item") if isinstance(elem.get("item"), dict) else elem
                            if isinstance(inner, dict) and str(inner.get("@type", "")).lower() == "product":
                                products.append(inner)
        return products

    @staticmethod
    def extract_universal_variable(html_text: str) -> Optional[dict[str, Any]]:
        """HTML içindeki window.universal_variable JSON yapısını çıkarır."""
        if "universal_variable" not in html_text:
            return None
        patterns = [
            r"window\.universal_variable\s*=\s*(\{.*?\});\s*</script>",
            r"window\.universal_variable\s*=\s*(\{.*?\});\s*\n",
        ]
        for pat in patterns:
            match = re.search(pat, html_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except Exception:
                    continue
        return None

    def build_scraped_product(
        self,
        raw_sku: str,
        brand: str,
        title: str,
        gender: str,
        url: str,
        image_url: str,
        retail_price: Any,
        sale_price: Any,
        api_basket_price: Any = None,
        campaign_texts: Optional[list[str]] = None,
        raw_sizes: Optional[list[Any]] = None,
        total_stock: Optional[int] = None,
    ) -> Optional[ScrapedProduct]:
        """Ham ürün verilerini normalize edip `ScrapedProduct` nesnesine dönüştürür."""
        full_url = self.build_full_url(url)
        full_image = self.build_full_url(image_url) if image_url else ""
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not clean_title:
            return None

        resolved_brand = self.infer_brand(brand, clean_title, full_url)
        resolved_gender = self.infer_gender(clean_title, full_url, default_gender=gender)

        canonical_sku, display_sku = SKUNormalizer.normalize(
            raw_sku=raw_sku,
            title=clean_title,
            url=full_url,
            brand=resolved_brand,
        )
        if not canonical_sku:
            return None

        price_res = BasketPromotionParser.calculate(
            retail_price=retail_price,
            sale_price=sale_price,
            api_basket_price=api_basket_price,
            campaign_texts=campaign_texts,
        )
        if price_res.net_basket_price <= 0:
            return None

        normalized_sizes: list[NormalizedSizeInfo] = SizeNormalizer.filter_target_sizes(
            raw_sizes=raw_sizes or [],
            gender=resolved_gender,
        )

        return ScrapedProduct(
            site_key=self.site_key,
            site_name=self.site_name,
            canonical_sku=canonical_sku,
            display_sku=display_sku,
            brand=resolved_brand,
            title=clean_title,
            gender=resolved_gender,
            url=full_url,
            image_url=full_image,
            retail_price=price_res.retail_price,
            sale_price=price_res.sale_price,
            net_basket_price=price_res.net_basket_price,
            total_discount_rate=price_res.total_discount_rate,
            basket_discount_rate=price_res.basket_discount_rate,
            campaign_name=price_res.campaign_name,
            sizes=normalized_sizes,
            total_stock=total_stock,
        )

    @abstractmethod
    def scrape_category(
        self,
        category_url: str,
        gender: str = "Erkek",
        max_pages: int = config.MAX_PAGES_PER_CATEGORY,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        """Belirtilen kategori URL'sini sayfalama (pagination) ile tarar."""
        raise NotImplementedError

    def scrape_all(
        self,
        max_pages_per_category: int = config.MAX_PAGES_PER_CATEGORY,
        max_products: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        """Sitenin tanımlı tüm kategorilerini tarar ve tekilleştirilmiş ürün listesi döndürür."""
        all_products: dict[str, ScrapedProduct] = {}

        for cat in self.categories:
            cat_url = cat["url"]
            cat_gender = cat.get("gender", "Erkek")
            remaining = (max_products - len(all_products)) if max_products else None
            if remaining is not None and remaining <= 0:
                break

            try:
                cat_items = self.scrape_category(
                    category_url=cat_url,
                    gender=cat_gender,
                    max_pages=max_pages_per_category,
                    limit=remaining,
                )
                for item in cat_items:
                    existing = all_products.get(item.canonical_sku)
                    if existing is None:
                        all_products[item.canonical_sku] = item
                    else:
                        # Aynı ürün farklı kategoride geldiyse bedenleri birleştir ve en düşük fiyatı tut
                        merged_sizes = {f"{s.normalized_size}:{s.original_text}": s for s in existing.sizes}
                        for s in item.sizes:
                            merged_sizes[f"{s.normalized_size}:{s.original_text}"] = s
                        existing.sizes = sorted(
                            merged_sizes.values(),
                            key=lambda x: float(x.normalized_size),
                        )
                        if item.net_basket_price < existing.net_basket_price:
                            existing.net_basket_price = item.net_basket_price
                            existing.total_discount_rate = item.total_discount_rate
                            existing.campaign_name = item.campaign_name

                    if max_products and len(all_products) >= max_products:
                        break
            except Exception as exc:
                logger.error("[%s] Kategori tarama hatası (%s): %s", self.site_name, cat_url, exc)

        logger.info("[%s] Toplam %d benzersiz ürün toplandı.", self.site_name, len(all_products))
        return list(all_products.values())
