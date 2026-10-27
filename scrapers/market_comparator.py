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
    Çapraz Piyasa & Akakçe Kesin Fiyat Doğrulayıcı (`market_comparator.py`).

    Çalışma Prensibi (%100 Doğrulanmış Piyasa Analizi + GitHub Actions US IP Koruması):
    1. Öncelikle ürünün spesifik Model Serisi + Cinsiyet (Örn: 'Lacoste T-Clip Kadın Sneaker',
       'Converse Run Star Trainer Sneaker', 'Puma RX 737 Mesh Kadın') ve Marka + SKU ('Puma 401392')
       sorgularıyla Akakçe'de canlı arama yapar.
    2. Akakçe'de Trendyol Plus, Hepsiburada, Amazon, Pazarama, Boyner gibi tüm satıcıların
       listelendiği kartları (`span.pt_v9`, `span.pt_v8`) ayrıştırır; kart başlığında modelin
       veya SKU'nun birebir geçtiğini doğrular ve **en ucuz satıcı fiyatını** (`lowest_price`) baz alır.
    3. Eşleşen gerçek Akakçe arama/ürün sayfasının adresini `product.verified_akakce_url` ve
       `StateStorage` (`data/products_state.json`) içine kaydeder.
    4. Eğer GitHub Actions (ABD veri merkezi IP'si) üzerinde Akakçe geo-block / 403 uygularsa:
       a) Önce diğer distribütör sitelerdeki çapraz fiyatı (`StateStorage`) kontrol eder.
       b) Ardından `StateStorage` içinde daha önce kaydedilmiş doğrulanmış Akakçe fiyatını kontrol eder.
       c) Son olarak, yalnızca vitrin fiyatına (`sale_price`) göre sepette ekstra en az %25 indirimi
          (`basket_discount_rate >= 25%`) ve spesifik model adı olan ürünlerde şişirilmiş liste
          fiyatlarına aldanmayan `min(retail_price * 0.72, sale_price * 0.88)` formülünü uygular.
    """

    GENERIC_SHOE_AND_COLOR_WORDS = re.compile(
        r"\b(?:erkek|kadın|kadin|unisex|spor|ayakkabı|ayakkabi|ayakkabısı|ayakkabisi|"
        r"sneaker|günlük|gunluk|koşu|kosu|yürüyüş|yuruyus|antrenman|basketbol|tenis|"
        r"beyaz|siyah|lacivert|mavi|kırmızı|kirmizi|yeşil|yesil|gri|bej|krem|kahverengi|"
        r"pembe|mor|turuncu|sarı|sari|haki|bordo|antrasit|renkli|çok|cok|açık|acik|koyu|"
        r"deri|süet|suet|kanvas|canvas|fileli|tekstil|hakiki|nubuk|sentetik|mesh)\b",
        re.IGNORECASE,
    )

    SKU_INDEXED_BRANDS = {
        "nike",
        "adidas",
        "puma",
        "new balance",
        "asics",
        "salomon",
        "skechers",
        "under armour",
    }

    EXCLUDED_CARD_WORDS = (
        "çocuk",
        "cocuk",
        "bebek",
        "bebe",
        "junior",
        "kız çocuk",
        "erkek çocuk",
        "parfüm",
        "parfum",
        "edt",
        "edp",
        "deodorant",
        "kol saati",
        "saat",
        "çanta",
        "canta",
        "cüzdan",
        "cuzdan",
        "kemer",
        "tişört",
        "tisort",
        "t-shirt",
        "sweatshirt",
        "eşofman",
        "esofman",
        "şort",
        "sort",
        "çorap",
        "corap",
        "terlik",
        "sandalet",
        "panduf",
        "şapka",
        "sapka",
        "bere",
    )

    def __init__(
        self,
        http_client: Optional[StealthHttpClient] = None,
        storage: Optional[StateStorage] = None,
    ) -> None:
        self.client = http_client or StealthHttpClient(min_delay=0.75, max_delay=1.35, max_retries=1, timeout=12)
        self.storage = storage
        self._cache: dict[str, tuple[float, str, Optional[str]]] = {}
        self._akakce_blocked_until: float = 0.0
        self._akakce_429_streak: int = 0

    @staticmethod
    def _normalize_tr_ascii(text: str) -> str:
        return (
            text.lower()
            .replace("ç", "c")
            .replace("ğ", "g")
            .replace("ı", "i")
            .replace("İ", "i")
            .replace("ö", "o")
            .replace("ş", "s")
            .replace("ü", "u")
        )

    @classmethod
    def extract_model_tokens(cls, title: str, brand: str, display_sku: str) -> list[str]:
        """
        Ürün başlığından marka, renk, cinsiyet ve genel ayakkabı kelimelerini çıkararak
        gerçek model serisi kelimelerini (Örn: ['t-clip'], ['chuck', '70'], ['rx', '737'],
        ['run', 'star', 'trainer'], ['handball', 'spezial'], ['l002', 'evo']) döndürür.
        """
        cleaned = title
        if brand:
            cleaned = re.sub(re.escape(brand), " ", cleaned, flags=re.IGNORECASE)
        if display_sku:
            cleaned = re.sub(re.escape(display_sku), " ", cleaned, flags=re.IGNORECASE)

        cleaned = cls.GENERIC_SHOE_AND_COLOR_WORDS.sub(" ", cleaned)
        cleaned = re.sub(r"[^a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\.\-]", " ", cleaned)
        raw_tokens = [t.strip(".-") for t in cleaned.split() if len(t.strip(".-")) >= 2]

        model_tokens: list[str] = []
        for tok in raw_tokens:
            norm_tok = cls._normalize_tr_ascii(tok)
            if norm_tok not in model_tokens:
                model_tokens.append(norm_tok)
        return model_tokens

    @classmethod
    def build_search_queries(cls, product: ScrapedProduct) -> list[tuple[str, bool]]:
        queries: list[tuple[str, bool]] = []
        sku = (product.display_sku or "").strip()
        brand_low = (product.brand or "").lower().strip()
        model_tokens = cls.extract_model_tokens(product.title, product.brand, sku)

        gender_word = ""
        g_low = (product.gender or "").lower()
        if "kadın" in g_low or "kadin" in g_low:
            gender_word = "Kadın"
        elif "erkek" in g_low:
            gender_word = "Erkek"

        # 1. Saf Model Serisi + Cinsiyet + Sneaker Sorgusu
        if model_tokens:
            core_model_str = " ".join(model_tokens[:4])
            core_q = f"{product.brand} {core_model_str} {gender_word} Sneaker"
            core_q = re.sub(r"\s+", " ", core_q).strip()
            queries.append((core_q, False))

        # 2. Marka + SKU Sorgusu
        is_internal_numeric_sku = sku.isdigit() and len(sku) >= 8 and sku.startswith("10")
        if sku and len(sku) >= 5 and not is_internal_numeric_sku:
            base_sku = re.sub(r"(?:[\-_]\d{1,3}|[A-Z])$", "", sku)
            if brand_low in cls.SKU_INDEXED_BRANDS or not model_tokens:
                if base_sku and len(base_sku) >= 6 and base_sku != sku:
                    queries.append((f"{product.brand} {base_sku}".strip(), True))
                queries.append((f"{product.brand} {sku}".strip(), True))

        return queries[:2]

    def get_market_floor_price(
        self,
        product: ScrapedProduct,
        all_current_run_products: Optional[list[ScrapedProduct]] = None,
        allow_live_query: bool = True,
    ) -> tuple[float, str]:
        """
        Ürün için `(market_floor_price, kaynak_açıklaması)` döndürür ve
        varsa doğrulanmış Akakçe URL'sini `product.verified_akakce_url` alanına atar.
        """
        cache_key = f"{product.canonical_sku}:{product.site_key}"
        if cache_key in self._cache:
            cached_price, cached_src, cached_url = self._cache[cache_key]
            if cached_url:
                product.verified_akakce_url = cached_url
            return cached_price, cached_src

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
            akakce_res = self._query_akakce_floor_price(product)
            if akakce_res is not None:
                akakce_price, matched_url, match_label = akakce_res
                if matched_url:
                    product.verified_akakce_url = matched_url
                if self.storage is not None:
                    self.storage.save_verified_market_floor(
                        canonical_sku=product.canonical_sku,
                        floor_price=akakce_price,
                        floor_source=match_label,
                        akakce_url=matched_url,
                    )

                if cross_site_candidates:
                    cross_site_candidates.sort(key=lambda x: x[0])
                    if cross_site_candidates[0][0] < akakce_price:
                        best_cp, best_cs = cross_site_candidates[0]
                        res_tuple = (round(best_cp, 2), f"Çapraz Site ({best_cs})")
                        self._cache[cache_key] = (res_tuple[0], res_tuple[1], matched_url)
                        return res_tuple

                res_tuple = (round(akakce_price, 2), match_label)
                self._cache[cache_key] = (res_tuple[0], res_tuple[1], matched_url)
                return res_tuple

        # 4. Yedek 1: Çoklu site veritabanımızdaki en düşük fiyat (diğer sitedeki en ucuz fiyat)
        if cross_site_candidates:
            cross_site_candidates.sort(key=lambda x: x[0])
            best_cross_price, best_cross_site = cross_site_candidates[0]
            result = (round(best_cross_price, 2), f"Çapraz Site ({best_cross_site})")
            self._cache[cache_key] = (result[0], result[1], product.verified_akakce_url)
            return result

        # 5. Yedek 2: StateStorage içinde daha önce doğrulanmış Akakçe fiyatı (GitHub Actions US IP koruması)
        if self.storage is not None:
            saved_verified = self.storage.get_verified_market_floor(product.canonical_sku)
            if saved_verified is not None:
                sv_price, sv_source, sv_url = saved_verified
                if sv_url:
                    product.verified_akakce_url = sv_url
                result = (round(sv_price, 2), sv_source)
                self._cache[cache_key] = (result[0], result[1], product.verified_akakce_url)
                return result

        # 6. Yedek 3: GitHub Actions (Yurt Dışı IP) Sepet Kampanyası Korumalı Referans Formülü
        model_tokens = self.extract_model_tokens(product.title, product.brand, product.display_sku)
        if model_tokens and product.basket_discount_rate >= 25.0 and product.sale_price > product.net_basket_price:
            queries = self.build_search_queries(product)
            if queries and not product.verified_akakce_url:
                product.verified_akakce_url = f"https://www.akakce.com/arama/?q={quote_plus(queries[0][0])}"
            protected_floor = round(min(product.retail_price * 0.72, product.sale_price * 0.88), 2)
            result = (protected_floor, "Vitrin & Sepet Korumalı Dip Fiyat (Min Liste x0.72 / Vitrin x0.88)")
            self._cache[cache_key] = (result[0], result[1], product.verified_akakce_url)
            return result

        # 7. Doğrulanamayan / Sepet İndirimsiz Standart Ürün Koruması (Sahte Sinyal Önleyici)
        unverified_floor = round(min(product.retail_price * 0.72, product.net_basket_price), 2)
        result = (unverified_floor, "Doğrulanamadı (Akakçe/Çapraz Eşleşme Yok)")
        self._cache[cache_key] = (result[0], result[1], product.verified_akakce_url)
        return result

    def _query_akakce_floor_price(self, product: ScrapedProduct) -> Optional[tuple[float, str, str]]:
        model_tokens = self.extract_model_tokens(product.title, product.brand, product.display_sku)
        brand_low = (product.brand or "").lower().strip()

        if not model_tokens and brand_low not in self.SKU_INDEXED_BRANDS:
            return None

        queries = self.build_search_queries(product)
        if not queries:
            return None

        product.verified_akakce_url = f"https://www.akakce.com/arama/?q={quote_plus(queries[0][0])}"

        min_plausible_price = max(650.0, product.net_basket_price * 0.55)
        max_plausible_price = max(product.retail_price * 1.40, product.net_basket_price * 2.8)

        for q_str, is_sku_query in queries:
            search_url = f"https://www.akakce.com/arama/?q={quote_plus(q_str)}"
            try:
                resp = self.client.get(search_url, is_json=False)
                if not resp:
                    self._akakce_429_streak += 1
                    if self._akakce_429_streak >= 3:
                        self._akakce_blocked_until = time.time() + 120.0
                    continue
                if resp.status_code in (429, 403):
                    self._akakce_429_streak += 1
                    if self._akakce_429_streak >= 3:
                        self._akakce_blocked_until = time.time() + 120.0
                    continue
                if not resp.ok or not resp.text:
                    continue

                self._akakce_429_streak = 0
                html_lower = resp.text.lower()

                if "herhangi bir ürün bulunamadı" in html_lower or "ürün bulunamadı" in html_lower:
                    continue

                matched_cards = self._extract_verified_cards_from_akakce_html(
                    html_text=resp.text,
                    product=product,
                    model_tokens=model_tokens,
                    min_price=min_plausible_price,
                    max_price=max_plausible_price,
                )
                if matched_cards:
                    matched_cards.sort(key=lambda x: x[0])
                    lowest_price, best_card_title, best_card_url = matched_cards[0]
                    product.verified_akakce_url = search_url
                    source_label = (
                        "Akakçe SKU Doğrulamalı En Ucuz"
                        if is_sku_query
                        else f"Akakçe En Ucuz ({best_card_title[:34]})"
                    )
                    return lowest_price, search_url, source_label
            except Exception as exc:
                logger.debug("Akakçe sorgu hatası (%s): %s", search_url, exc)
                continue

        return None

    @classmethod
    def _extract_verified_cards_from_akakce_html(
        cls,
        html_text: str,
        product: ScrapedProduct,
        model_tokens: list[str],
        min_price: float,
        max_price: float,
    ) -> list[tuple[float, str, str]]:
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("ul#APL li, ul.pl_v8 li, li[data-pr]")
        verified: list[tuple[float, str, str]] = []

        sku_norm = cls._normalize_tr_ascii(product.display_sku or "")
        base_sku_norm = re.sub(r"(?:[\-_]\d{1,3}|[a-z])$", "", sku_norm)
        brand_norm = cls._normalize_tr_ascii(product.brand or "")
        prod_gender_norm = cls._normalize_tr_ascii(product.gender or "unisex")

        for li in cards:
            title_el = li.select_one("h3.pn_v8, .pn_v8, a[title]")
            card_title = title_el.get_text(" ", strip=True) if title_el else ""
            if not card_title:
                continue

            card_norm = cls._normalize_tr_ascii(card_title)

            if any(bad in card_norm for bad in cls.EXCLUDED_CARD_WORDS):
                continue

            if brand_norm and brand_norm not in card_norm:
                data_mk = cls._normalize_tr_ascii(str(li.get("data-mk") or ""))
                if brand_norm not in data_mk:
                    continue

            if "erkek" in prod_gender_norm and "kadin" not in prod_gender_norm:
                if "kadin" in card_norm and "erkek" not in card_norm and "unisex" not in card_norm:
                    continue
            elif "kadin" in prod_gender_norm and "erkek" not in prod_gender_norm:
                if "erkek" in card_norm and "kadin" not in card_norm and "unisex" not in card_norm:
                    continue

            link_el = li.select_one("a.iC[href], a[href]")
            raw_href = str(link_el.get("href") or "").strip() if link_el else ""
            full_href = f"https://www.akakce.com{raw_href}" if raw_href.startswith("/") else raw_href
            href_norm = cls._normalize_tr_ascii(raw_href)

            sku_matched = False
            if len(sku_norm) >= 5 and not (sku_norm.isdigit() and sku_norm.startswith("10")):
                if sku_norm in card_norm or sku_norm in href_norm:
                    sku_matched = True
                elif len(base_sku_norm) >= 6 and (base_sku_norm in card_norm or base_sku_norm in href_norm):
                    sku_matched = True

            model_matched = False
            if model_tokens:
                significant_tokens = [t for t in model_tokens if len(t) >= 2]
                if significant_tokens:
                    matched_tok_count = sum(1 for t in significant_tokens[:3] if t in card_norm or t in href_norm)
                    required_count = 1 if len(significant_tokens) == 1 else min(2, len(significant_tokens))
                    if matched_tok_count >= required_count:
                        model_matched = True

            if not (sku_matched or model_matched):
                continue

            price_val = cls._parse_card_price(li)
            if min_price <= price_val <= max_price:
                verified.append((round(price_val, 2), card_title, full_href))

        return verified

    @staticmethod
    def _parse_card_price(li_tag) -> float:
        pt_el = li_tag.select_one("span.pt_v9, span.pt_v8, .pt_v9, .pt_v8")
        if not pt_el:
            return 0.0
        txt = pt_el.get_text(" ", strip=True)
        txt = re.sub(r"\s*,\s*", ",", txt)
        m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", txt, flags=re.IGNORECASE)
        if m:
            return BasketPromotionParser.parse_turkish_price(m.group(1))
        return 0.0
