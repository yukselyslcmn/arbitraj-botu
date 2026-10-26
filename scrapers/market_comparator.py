import logging
import re
import time
from typing import Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from core.arbitrage_engine import ScrapedProduct
from core.http_client import StealthHttpClient
from core.parsers import BasketPromotionParser
from core.storage import StateStorage

logger = logging.getLogger("arbitraj.market_comparator")


class MarketComparator:
    """
    Çapraz Piyasa & Akakçe Fiyat Doğrulayıcı (`market_comparator.py`).
    Bir ürün yüksek indirim filtresini geçtiğinde:
    1. Ürünün normalize edilmiş Model Kodu (SKU) ve marka-model adıyla Akakçe (`akakce.com/arama/?q=...`)
       üzerinden Türkiye'deki güncel Piyasa Dip Fiyatını (`market_floor_price`) sorgular.
    2. Eğer Akakçe'de eşleşme bulunamazsa veya WAF/429 engeli olursa kendi çoklu site veritabanımızdaki
       (`StateStorage`) diğer sitelerin en düşük fiyatını kontrol eder.
    3. Orada da yoksa yedek olarak `retail_price * 0.72` değerini referans alır.
    """

    def __init__(
        self,
        http_client: Optional[StealthHttpClient] = None,
        storage: Optional[StateStorage] = None,
    ) -> None:
        self.client = http_client or StealthHttpClient(min_delay=0.6, max_delay=1.2, max_retries=1, timeout=12)
        self.storage = storage
        self._cache: dict[str, tuple[float, str]] = {}
        self._akakce_blocked_until: float = 0.0
        self._akakce_429_streak: int = 0

    def get_market_floor_price(
        self,
        product: ScrapedProduct,
        all_current_run_products: Optional[list[ScrapedProduct]] = None,
        allow_live_query: bool = True,
    ) -> tuple[float, str]:
        """
        Ürün için `(market_floor_price, kaynak_açıklaması)` döndürür.
        """
        cache_key = f"{product.canonical_sku}:{product.site_key}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 1. Mevcut çalışma sırasındaki diğer sitelerde aynı canonical_sku var mı kontrol et
        cross_site_candidates: list[tuple[float, str]] = []
        if all_current_run_products:
            for other in all_current_run_products:
                if other.canonical_sku == product.canonical_sku and other.site_key != product.site_key:
                    if other.net_basket_price > 0:
                        cross_site_candidates.append((other.net_basket_price, other.site_name))

        # 2. StateStorage veritabanında diğer sitelerdeki fiyatları kontrol et
        if self.storage is not None:
            stored_cross = self.storage.get_cross_site_floor_price(
                canonical_sku=product.canonical_sku,
                exclude_site_key=product.site_key,
            )
            if stored_cross is not None:
                cross_site_candidates.append(stored_cross)

        # 3. Akakçe Canlı Arama Sorgusu (`https://www.akakce.com/arama/?q=...`)
        if allow_live_query and time.time() >= self._akakce_blocked_until:
            akakce_price = self._query_akakce_floor_price(product)
            if akakce_price is not None and akakce_price > 0:
                result = (round(akakce_price, 2), "Akakçe Canlı Piyasa Dip Fiyatı")
                self._cache[cache_key] = result
                return result

        # 4. Yedek 1: Çoklu site veritabanımızdaki en düşük 2. fiyat (diğer sitedeki en ucuz fiyat)
        if cross_site_candidates:
            cross_site_candidates.sort(key=lambda x: x[0])
            best_cross_price, best_cross_site = cross_site_candidates[0]
            result = (round(best_cross_price, 2), f"Çapraz Site ({best_cross_site})")
            self._cache[cache_key] = result
            return result

        # 5. Yedek 2: `retail_price * 0.72` referans formülü
        fallback_price = round(product.retail_price * 0.72, 2)
        if product.basket_discount_rate > 0 and product.sale_price * 0.92 > fallback_price:
            fallback_price = round(product.sale_price * 0.92, 2)
            result = (fallback_price, "Piyasa Referans (Vitrin x 0.92)")
        else:
            result = (fallback_price, "Piyasa Referans (Liste x 0.72)")
        self._cache[cache_key] = result
        return result

    def _query_akakce_floor_price(self, product: ScrapedProduct) -> Optional[float]:
        """
        Akakçe arama sayfasından (`https://www.akakce.com/arama/?q=...`) ürünün fiyatlarını ayrıştırır.
        WAF veya HTTP 429 durumunda devre kesiciyi (circuit-breaker) devreye sokarak taramanın
        takılmasını önler.
        """
        queries = []
        if product.display_sku and len(product.display_sku) >= 5:
            queries.append(f"{product.brand} {product.display_sku}")
        clean_model = re.sub(
            r"\b(?:erkek|kadın|kadin|unisex|spor|ayakkabı|ayakkabi|sneaker)\b",
            "",
            product.title,
            flags=re.IGNORECASE,
        )
        clean_model = re.sub(r"\s+", " ", clean_model).strip()
        if clean_model and clean_model not in queries:
            queries.append(clean_model[:55])

        min_plausible_price = max(650.0, product.net_basket_price * 0.65)
        max_plausible_price = max(product.retail_price * 1.35, product.net_basket_price * 2.5)

        for q in queries[:1]:
            search_url = f"https://www.akakce.com/arama/?q={quote_plus(q)}"
            try:
                resp = self.client.get(search_url, is_json=False)
                if not resp:
                    self._akakce_429_streak += 1
                    if self._akakce_429_streak >= 2:
                        self._akakce_blocked_until = time.time() + 180.0
                    continue
                if resp.status_code in (429, 403):
                    self._akakce_429_streak += 1
                    if self._akakce_429_streak >= 2:
                        self._akakce_blocked_until = time.time() + 180.0
                    continue
                if not resp.ok or not resp.text:
                    continue

                self._akakce_429_streak = 0
                prices = self._extract_prices_from_akakce_html(
                    html_text=resp.text,
                    min_price=min_plausible_price,
                    max_price=max_plausible_price,
                )
                if prices:
                    prices.sort()
                    if len(prices) >= 2 and prices[0] <= product.sale_price * 1.03:
                        return prices[1]
                    return prices[0]
            except Exception as exc:
                logger.debug("Akakçe sorgu hatası (%s): %s", search_url, exc)
                continue

        return None

    @staticmethod
    def _extract_prices_from_akakce_html(
        html_text: str,
        min_price: float,
        max_price: float,
    ) -> list[float]:
        soup = BeautifulSoup(html_text, "lxml")
        extracted: list[float] = []

        # Akakçe ürün listesi fiyat seçicileri: span.pt_v8, span.pb_v8, .p_v8, [itemprop='price'], [itemprop='lowPrice']
        price_elements = soup.select(
            "span.pt_v8, span.pb_v8, .p_v8, [itemprop='price'], [itemprop='lowPrice'], ul#APL li .pt_v8"
        )
        for el in price_elements:
            content_attr = el.get("content")
            raw_txt = content_attr if content_attr else el.get_text(" ", strip=True)
            val = BasketPromotionParser.parse_turkish_price(raw_txt)
            if min_price <= val <= max_price:
                extracted.append(val)

        # Seçicilerden çıkmadıysa metin içindeki TL fiyat kalıplarını kontrol et
        if not extracted:
            for match in re.finditer(r"(\d{1,2}\.\d{3}(?:,\d{2})?)\s*TL", html_text):
                val = BasketPromotionParser.parse_turkish_price(match.group(1))
                if min_price <= val <= max_price:
                    extracted.append(val)

        # Tekilleştir
        unique_prices = sorted(set(round(p, 2) for p in extracted))
        return unique_prices
