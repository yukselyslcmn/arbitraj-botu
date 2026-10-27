import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from core.arbitrage_engine import ScrapedProduct
from core.http_client import StealthHttpClient
from core.parsers import BasketPromotionParser, SKUNormalizer
from core.storage import StateStorage

logger = logging.getLogger("arbitraj.market_comparator")


@dataclass(slots=True)
class OfficialOfferInfo:
    """Tek bir resmi satıcı veya yetkili platform teklifi."""

    platform: str
    seller_name: str
    price: float
    is_official: bool = True
    url: str = ""


@dataclass(slots=True)
class MarketComparisonDetails:
    """Tüm yetkili mağazalar ve Akakçe pazaryerleri resmi satıcı kıyaslama detayları."""

    floor_price: float
    floor_source: str
    verified_url: Optional[str] = None
    best_official_offer: Optional[OfficialOfferInfo] = None
    all_official_offers: list[OfficialOfferInfo] = field(default_factory=list)
    official_sellers_breakdown: list[str] = field(default_factory=list)
    is_scraped_site_cheapest: bool = True
    cheaper_official_seller_alert: Optional[str] = None


class MarketComparator:
    """
    7 Yetkili Orijinal Mağaza (Occasion, SuperStep, InStreet, Barçın, Korayspor, Sportive, FashFed)
    ve Akakçe Canlı Pazaryeri (idefix, Amazon, Hepsiburada, Pazarama, Trendyol vb.)
    Resmi Satıcı Çapraz Fiyat Doğrulayıcısı.
    """

    AUTHORIZED_OFFICIAL_SELLERS = {
        # Pazaryerleri (Platformun kendisi veya yetkili mağazaları)
        "amazon",
        "amazon türkiye",
        "amazon.com.tr",
        "hepsiburada",
        "hepsiburada.com",
        "trendyol",
        "trendyol.com",
        "idefix",
        "idefix.com",
        "pazarama",
        "pazarama.com",
        "n11",
        "n11.com",
        "pttavm",
        "pttavm.com",
        # Yetkili Zincir ve Distribütör Mağazalar
        "korayspor",
        "koray spor",
        "sportive",
        "barcin",
        "barçın",
        "superstep",
        "super step",
        "occasion",
        "instreet",
        "in street",
        "flo",
        "fashfed",
        "fash fed",
        "boyner",
        "beymen",
        "yalı spor",
        "yalispor",
        "sneaks up",
        "sneaksup",
        "sporthink",
        "intersport",
        "finspor",
        "sportinn",
        "dalkılıç spor",
        "dalkilic",
        "şampiyon spor",
        "sampiyon spor",
        "maratonsport",
        "morhipo",
        "etiler spor",
        "sportime",
        "sporset",
        "çukurova spor",
        "cukurova spor",
        # Orijinal Marka Mağazaları
        "puma",
        "nike",
        "adidas",
        "under armour",
        "skechers",
        "asics",
        "salomon",
        "columbia",
        "lacoste",
        "the north face",
        "converse",
        "vans",
        "timberland",
        "tommy hilfiger",
        "tommy",
        "gant",
        "nautica",
        "helly hansen",
        "new balance",
    }

    GENERIC_SHOE_AND_COLOR_WORDS = re.compile(
        r"\b(?:erkek|kadın|kadin|unisex|spor|ayakkabı|ayakkabi|ayakkabısı|ayakkabisi|"
        r"sneaker|günlük|gunluk|koşu|kosu|yürüyüş|yuruyus|antrenman|basketbol|tenis|"
        r"beyaz|siyah|lacivert|mavi|kırmızı|kirmizi|yeşil|yesil|gri|bej|krem|kahverengi|"
        r"pembe|mor|turuncu|sarı|sari|haki|bordo|antrasit|renkli|çok|cok|açık|acik|koyu|"
        r"deri|süet|suet|kanvas|canvas|fileli|tekstil|hakiki|nubuk|sentetik|mesh|"
        r"low|mid|high|retro|classic|klasik|casual|lifestyle|comfort|essential|essentials)\b",
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
        self.client = http_client or StealthHttpClient(min_delay=0.4, max_delay=0.9, max_retries=2, timeout=15)
        self.storage = storage
        self._cache: dict[str, MarketComparisonDetails] = {}
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
    def is_official_seller(cls, seller_name: str, platform: str, brand: str = "") -> bool:
        s_norm = cls._normalize_tr_ascii(seller_name)
        p_norm = cls._normalize_tr_ascii(platform)
        b_norm = cls._normalize_tr_ascii(brand)

        for auth in cls.AUTHORIZED_OFFICIAL_SELLERS:
            auth_norm = cls._normalize_tr_ascii(auth)
            if auth_norm in s_norm or auth_norm in p_norm:
                return True
        if b_norm and len(b_norm) >= 3 and (b_norm in s_norm or b_norm in p_norm):
            return True
        return False

    @classmethod
    def extract_model_tokens(cls, title: str, brand: str, display_sku: str) -> list[str]:
        cleaned = title
        if brand:
            cleaned = re.sub(re.escape(brand), " ", cleaned, flags=re.IGNORECASE)
        if display_sku:
            clean_sku = re.escape(display_sku.strip())
            cleaned = re.sub(rf"{clean_sku}\d*", " ", cleaned, flags=re.IGNORECASE)

        cleaned = cls.GENERIC_SHOE_AND_COLOR_WORDS.sub(" ", cleaned)
        cleaned = re.sub(r"[^a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\.\-]", " ", cleaned)
        raw_tokens = [t.strip(".-") for t in cleaned.split() if len(t.strip(".-")) >= 2]

        model_tokens: list[str] = []
        for tok in raw_tokens:
            norm_tok = cls._normalize_tr_ascii(tok)
            if norm_tok.isdigit() and len(norm_tok) <= 3:
                continue
            if norm_tok not in model_tokens:
                model_tokens.append(norm_tok)
        return model_tokens

    @classmethod
    def build_search_queries(cls, product: ScrapedProduct) -> list[tuple[str, bool]]:
        queries: list[tuple[str, bool]] = []
        sku = (product.display_sku or "").strip()
        brand_clean = (product.brand or "").strip()
        model_tokens = cls.extract_model_tokens(product.title, product.brand, sku)

        gender_word = ""
        g_low = (product.gender or "").lower()
        if "kadın" in g_low or "kadin" in g_low:
            gender_word = "Kadın"
        elif "erkek" in g_low:
            gender_word = "Erkek"

        # 1. ÖNCELİK: Marka + Üretici SKU (Akakçe ve pazaryerlerinde %100 kesin eşleşme)
        is_internal_numeric_sku = sku.isdigit() and len(sku) >= 8 and sku.startswith("10")
        title_sku = SKUNormalizer.extract_sku_from_text(product.title)
        if title_sku and title_sku != sku:
            queries.append((f"{brand_clean} {title_sku}".strip(), True))

        if sku and len(sku) >= 5 and not is_internal_numeric_sku:
            queries.append((f"{brand_clean} {sku}".strip(), True))
            base_sku = re.sub(r"(?:[\-_]\d{1,3}|[A-Z])$", "", sku)
            if base_sku and len(base_sku) >= 6 and base_sku != sku:
                queries.append((f"{brand_clean} {base_sku}".strip(), True))

        # 2. ÖNCELİK: Temiz Model Serisi + Cinsiyet + Sneaker Sorgusu
        if model_tokens:
            core_model_str = " ".join(model_tokens[:3])
            core_q = f"{brand_clean} {core_model_str} {gender_word} Sneaker"
            core_q = re.sub(r"\s+", " ", core_q).strip()
            queries.append((core_q, False))

        return queries[:3]

    def _fetch_detail_page_offers(self, detail_url: str, brand: str) -> list[OfficialOfferInfo]:
        """Akakçe ürün detay sayfasından (ul#PL li) tüm resmi distribütör tekliflerini çeker."""
        if not detail_url:
            return []
        try:
            resp = self.client.get(detail_url, is_json=False)
            if not resp or not resp.ok or not resp.text:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            offers: list[OfficialOfferInfo] = []

            for li in soup.select("ul#PL li"):
                pt_el = li.select_one("span.pt_v9, span.pt_v8, .pt_v9, .pt_v8, .price")
                if not pt_el:
                    continue
                txt = pt_el.get_text(" ", strip=True)
                txt = re.sub(r"\s*,\s*", ",", txt)
                m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", txt, re.I)
                if not m:
                    continue
                price = BasketPromotionParser.parse_turkish_price(m.group(1))
                if price <= 0:
                    continue

                s_tag = li.select_one(".v_v8, .s_v8, .seller, .merchant")
                seller_raw = s_tag.get_text(" ", strip=True) if s_tag else ""
                seller = re.sub(r"^(?:sat[ıi]c[ıi]\s*:\s*|/|\s)+", "", seller_raw, flags=re.I).strip()

                img = li.select_one("img[alt]")
                plat = img.get("alt", "").strip() if img else ""
                if not plat:
                    a_tag = li.select_one("a[title]")
                    plat = a_tag.get("title", "").strip() if a_tag else ""
                if not plat:
                    plat = "Akakçe Mağaza"

                is_off = self.is_official_seller(seller, plat, brand)
                if is_off:
                    offers.append(
                        OfficialOfferInfo(
                            platform=plat,
                            seller_name=seller or plat,
                            price=round(price, 2),
                            is_official=True,
                            url=detail_url,
                        )
                    )
            offers.sort(key=lambda x: x.price)
            return offers
        except Exception as exc:
            logger.debug("Akakçe detay sayfası satıcı ayrıştırma hatası (%s): %s", detail_url, exc)
            return []

    def get_detailed_market_comparison(
        self,
        product: ScrapedProduct,
        all_current_run_products: Optional[list[ScrapedProduct]] = None,
        allow_live_query: bool = True,
    ) -> MarketComparisonDetails:
        """
        Ürün için 7 yetkili site + Akakçe'deki tüm resmi satıcıları (Amazon, idefix, Hepsiburada, Pazarama vb.)
        karşılaştırarak en dip fiyatı, en uygun resmi satıcıyı ve tam dökümü döndürür.
        """
        cache_key = f"{product.canonical_sku}:{product.site_key}"
        if cache_key in self._cache:
            cached_res = self._cache[cache_key]
            if cached_res.verified_url:
                product.verified_akakce_url = cached_res.verified_url
            return cached_res

        model_tokens = self.extract_model_tokens(product.title, product.brand, product.display_sku)
        all_official_offers: list[OfficialOfferInfo] = []

        # 1. Mevcut çalışma sırasındaki diğer 6 yetkili sitedeki eşleşmeler
        if all_current_run_products:
            for other in all_current_run_products:
                if other.canonical_sku == product.canonical_sku and other.net_basket_price > 0:
                    all_official_offers.append(
                        OfficialOfferInfo(
                            platform=other.site_name,
                            seller_name=f"{other.site_name} (Kendi Sitesi)",
                            price=round(other.net_basket_price, 2),
                            is_official=True,
                            url=other.url,
                        )
                    )

        # 2. StateStorage içindeki kayıtlı diğer mağazalar
        if self.storage is not None:
            sku_entry = self.storage.state.get("products", {}).get(product.canonical_sku, {})
            for s_key, s_data in (sku_entry.get("sites") or {}).items():
                p_val = s_data.get("net_basket_price") or s_data.get("sale_price")
                s_name = str(s_data.get("site_name") or s_key)
                if isinstance(p_val, (int, float)) and p_val >= 650.0:
                    all_official_offers.append(
                        OfficialOfferInfo(
                            platform=s_name,
                            seller_name=f"{s_name} (Kendi Sitesi)",
                            price=round(float(p_val), 2),
                            is_official=True,
                            url=str(s_data.get("url") or ""),
                        )
                    )

        # 3. Akakçe Canlı Sorgusu ve Pazaryerlerindeki Resmi Satıcılar
        verified_url: Optional[str] = None
        queries = self.build_search_queries(product)
        if queries and not product.verified_akakce_url:
            verified_url = f"https://www.akakce.com/arama/?q={quote_plus(queries[0][0])}"
            product.verified_akakce_url = verified_url

        if allow_live_query and time.time() >= self._akakce_blocked_until:
            min_plausible_price = max(650.0, product.net_basket_price * 0.50)
            max_plausible_price = max(product.retail_price * 1.40, product.net_basket_price * 2.8)

            for q_str, is_sku in queries:
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
                    soup = BeautifulSoup(resp.text, "html.parser")
                    cards = soup.select("ul#APL li, ul.pl_v8 li, li[data-pr]")
                    if not cards:
                        continue

                    verified_cards = self._extract_verified_cards_from_akakce_html(
                        html_text=resp.text,
                        product=product,
                        model_tokens=model_tokens,
                        min_price=min_plausible_price,
                        max_price=max_plausible_price,
                    )

                    detail_page_hit = False
                    for card_price, card_title, card_detail_url in verified_cards[:3]:
                        if card_detail_url:
                            verified_url = card_detail_url
                            product.verified_akakce_url = verified_url
                            offers = self._fetch_detail_page_offers(card_detail_url, product.brand)
                            if offers:
                                all_official_offers.extend(offers)
                                detail_page_hit = True

                    if not detail_page_hit and verified_cards:
                        card_price, card_title, card_detail_url = verified_cards[0]
                        all_official_offers.append(
                            OfficialOfferInfo(
                                platform="Akakçe En Ucuz Satıcı",
                                seller_name="Akakçe Doğrulamalı",
                                price=round(card_price, 2),
                                is_official=True,
                                url=card_detail_url or search_url,
                            )
                        )
                        verified_url = card_detail_url or search_url
                        product.verified_akakce_url = verified_url

                    if all_official_offers:
                        break
                except Exception as exc:
                    logger.debug("Akakçe arama sorgu hatası (%s): %s", search_url, exc)
                    continue

        # Kendi mağaza fiyatını da teklifler listesine dahil et
        if product.net_basket_price > 0:
            all_official_offers.append(
                OfficialOfferInfo(
                    platform=product.site_name,
                    seller_name=f"{product.site_name} (Kendi Sitesi)",
                    price=round(product.net_basket_price, 2),
                    is_official=True,
                    url=product.url,
                )
            )

        # Satıcı ve Platform bazında tekilleştir ve fiyata göre sırala
        unique_offers: dict[tuple[str, str], OfficialOfferInfo] = {}
        for off in all_official_offers:
            key = (off.platform.lower().strip(), off.seller_name.lower().strip())
            if key not in unique_offers or off.price < unique_offers[key].price:
                unique_offers[key] = off

        sorted_offers = sorted(unique_offers.values(), key=lambda x: x.price)
        best_offer = sorted_offers[0] if sorted_offers else None

        # Taranan kaynak mağaza dışındaki DİĞER resmi satıcıların teklifleri (Rakip piyasa dip fiyatı):
        current_site_lower = product.site_name.lower().strip()
        other_competitor_offers = [
            off for off in sorted_offers
            if current_site_lower not in off.platform.lower() and current_site_lower not in off.seller_name.lower()
        ]

        if other_competitor_offers:
            best_competitor = other_competitor_offers[0]
            floor_price = best_competitor.price
            floor_source = (
                f"Resmi Satıcı Kıyaslaması ({best_competitor.platform} / {best_competitor.seller_name}: {best_competitor.price:,.0f} TL)"
            )
        else:
            # Başka resmi mağazada bulunamadıysa liste fiyatı x 0.72 piyasa referansı alınır
            floor_price = round(product.retail_price * 0.72, 2)
            floor_source = "Liste Fiyatı x 0.72 (Tahmini Piyasa Değeri)"

        # Rütbeli döküm listesi hazırla (🏆, 🥈, 🥉, ▫️)
        breakdown: list[str] = []
        rank_emojis = ["🏆", "🥈", "🥉", "▫️", "▫️", "▫️"]
        for idx, off in enumerate(sorted_offers[:5]):
            emo = rank_emojis[idx] if idx < len(rank_emojis) else "▫️"
            breakdown.append(f"{emo} {off.platform} ({off.seller_name}): {off.price:,.0f} TL".replace(",", "."))

        # Taranan kaynak mağaza en ucuz mu?
        is_scraped_cheapest = True
        cheaper_alert: Optional[str] = None
        if best_offer and product.net_basket_price > best_offer.price + 20.0:
            is_scraped_cheapest = False
            cheaper_alert = (
                f"⚠️ Dikkat: Aynı ürün {best_offer.platform} ({best_offer.seller_name}) "
                f"üzerinde {best_offer.price:,.0f} TL ile daha ucuz! (Alış: {product.net_basket_price:,.0f} TL)"
            ).replace(",", ".")

        details = MarketComparisonDetails(
            floor_price=floor_price,
            floor_source=floor_source,
            verified_url=verified_url,
            best_official_offer=best_offer,
            all_official_offers=sorted_offers,
            official_sellers_breakdown=breakdown,
            is_scraped_site_cheapest=is_scraped_cheapest,
            cheaper_official_seller_alert=cheaper_alert,
        )
        self._cache[cache_key] = details

        # StateStorage içine de kaydet
        if self.storage is not None and best_offer is not None:
            self.storage.save_verified_market_floor(
                canonical_sku=product.canonical_sku,
                floor_price=floor_price,
                floor_source=floor_source,
                akakce_url=verified_url,
                best_official_seller=best_offer.seller_name,
                best_official_platform=best_offer.platform,
                official_breakdown=breakdown,
            )

        return details

    def get_market_floor_price(
        self,
        product: ScrapedProduct,
        all_current_run_products: Optional[list[ScrapedProduct]] = None,
        allow_live_query: bool = True,
    ) -> tuple[float, str]:
        """Geriye dönük uyumluluk metodu (floor_price, floor_source)."""
        details = self.get_detailed_market_comparison(
            product=product,
            all_current_run_products=all_current_run_products,
            allow_live_query=allow_live_query,
        )
        return round(details.floor_price, 2), details.floor_source

    @classmethod
    def _extract_verified_cards_from_akakce_html(
        cls,
        html_text: str,
        product: ScrapedProduct,
        model_tokens: list[str],
        min_price: float,
        max_price: float,
    ) -> list[tuple[float, str, str]]:
        soup = BeautifulSoup(html_text, "html.parser")
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
        pt_el = li_tag.select_one("span.pt_v9, span.pt_v8, .pt_v9, .pt_v8, .price")
        if not pt_el:
            return 0.0
        txt = pt_el.get_text(" ", strip=True)
        txt = re.sub(r"\s*,\s*", ",", txt)
        m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", txt, flags=re.IGNORECASE)
        if m:
            return BasketPromotionParser.parse_turkish_price(m.group(1))
        return 0.0
