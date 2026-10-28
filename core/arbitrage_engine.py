from dataclasses import dataclass, field
import re
from typing import Optional
from urllib.parse import quote_plus

import config
from core.parsers import NormalizedSizeInfo


@dataclass(slots=True)
class ScrapedProduct:
    """Kazıyıcılardan (scrapers) dönen standartlaştırılmış ürün veri modeli."""

    site_key: str
    site_name: str
    canonical_sku: str
    display_sku: str
    brand: str
    title: str
    gender: str
    url: str
    image_url: str
    retail_price: float
    sale_price: float
    net_basket_price: float
    total_discount_rate: float
    basket_discount_rate: float
    campaign_name: str
    sizes: list[NormalizedSizeInfo] = field(default_factory=list)
    total_stock: Optional[int] = None
    verified_akakce_url: Optional[str] = None

    @property
    def akakce_search_url(self) -> str:
        if self.verified_akakce_url:
            return self.verified_akakce_url
        brand_low = (self.brand or "").lower().strip()
        # Global SKU kullanmayan veya dahili mağaza barkodu kullanan markalarda (Lacoste vb.)
        # Akakçe'de arama modeli SKU yerine doğrudan Temiz Model Adı ile yapılmalıdır:
        if brand_low not in ("nike", "adidas", "puma", "new balance", "asics", "salomon", "skechers", "under armour"):
            from scrapers.market_comparator import MarketComparator
            tokens = MarketComparator.extract_model_tokens(self.title, self.brand, self.display_sku)
            if tokens:
                model_str = " ".join(tokens[:3])
                q_text = f"{self.brand} {model_str}".strip()
                return f"https://www.akakce.com/arama/?q={quote_plus(q_text)}"
        query = f"{self.brand} {self.display_sku}".strip()
        return f"https://www.akakce.com/arama/?q={quote_plus(query)}"
    @property
    def is_low_stock(self) -> bool:
        if self.total_stock is not None and 0 < self.total_stock <= 3:
            return True
        low_sizes = [s for s in self.sizes if s.stock_count is not None and 0 < s.stock_count <= 3]
        return len(low_sizes) > 0 and len(low_sizes) == len(self.sizes)

    @property
    def min_stock_count(self) -> Optional[int]:
        if self.total_stock is not None and self.total_stock > 0:
            return self.total_stock
        counts = [s.stock_count for s in self.sizes if s.stock_count is not None and s.stock_count > 0]
        return sum(counts) if counts else None


@dataclass(slots=True)
class ArbitrageAnalysis:
    """Bir ürünün tüm birim ekonomisi, Gardrops/Dolap net kâr, ROI, trend ve puan analizi."""

    product: ScrapedProduct
    buy_cost: float
    market_floor_price: float
    market_floor_source: str
    suggested_sell_price: float

    # Senaryo 1: Gardrops / Sahibinden (Param Güvende) / Letgo
    gardrops_expense: float
    net_profit_gardrops: float
    roi_gardrops: float

    # Senaryo 2: Dolap Uygulaması
    dolap_expense: float
    net_profit_dolap: float
    roi_dolap: float

    # Puan, Fiyat Trendi, Sahte İndirim Tespiti ve Fırsat Durumu
    arbitrage_score: int
    is_profitable_opportunity: bool
    rejection_reason: str = ""
    price_trend_badge: str = "📊 Aktif Kampanya Fiyatı"
    is_inflated_retail_price: bool = False

    # Çoklu Resmi Satıcı ve Pazaryeri Kıyaslaması (Yeni)
    best_official_price: Optional[float] = None
    best_official_seller: Optional[str] = None
    best_official_platform: Optional[str] = None
    official_sellers_breakdown: list[str] = field(default_factory=list)
    is_buy_source_cheapest: bool = True
    cheaper_source_alert: Optional[str] = None


class ArbitrageEngine:
    """
    Türkiye 2. El / Sıfır Satış Platformları (Gardrops, Sahibinden, Letgo, Dolap)
    2026 Kesinti Yapısına Göre Birim Ekonomisi, Sahte İndirim Koruması ve ROI Hesaplayıcısı.
    """

    GOLDEN_SIZES = {"42", "42.5", "43", "43.5", "44", "38", "38.5", "39"}

    @staticmethod
    def is_brand_allowed(brand: str, title: str = "") -> Optional[str]:
        """
        Ürün markasının izin verilen 18 orijinal markadan biri olup olmadığını kontrol eder.
        Kelime sınırı (word boundary) kullanarak 'Gant' -> 'Elegant' gibi yanlış eşleşmeleri önler.
        Eşleşirse standart marka adını, eşleşmezse None döndürür.
        """
        brand_low = (brand or "").lower().strip()
        title_low = (title or "").lower().strip()
        combined = f"{brand_low} {title_low}".strip()
        for allowed in config.ALLOWED_BRANDS:
            allowed_low = allowed.lower()
            if brand_low == allowed_low or re.search(rf"\b{re.escape(allowed_low)}\b", combined):
                return allowed
        return None

    @staticmethod
    def has_excluded_keyword(title: str, url: str = "") -> Optional[str]:
        """Kara listedeki kelimelerden (terlik, çorap, çocuk vb.) biri geçiyorsa o kelimeyi döndürür."""
        combined = f"{title} {url}".lower()
        combined_ascii = (
            combined.replace("ç", "c")
            .replace("ğ", "g")
            .replace("ı", "i")
            .replace("ö", "o")
            .replace("ş", "s")
            .replace("ü", "u")
        )
        for kw in config.EXCLUDE_KEYWORDS:
            kw_low = kw.lower()
            kw_ascii = (
                kw_low.replace("ç", "c")
                .replace("ğ", "g")
                .replace("ı", "i")
                .replace("ö", "o")
                .replace("ş", "s")
                .replace("ü", "u")
            )
            if kw_low in combined or kw_ascii in combined_ascii:
                return kw
        return None

    @classmethod
    def is_footwear(cls, title: str, url: str = "") -> bool:
        """
        Ürünün kesin olarak ayakkabı / sneaker / bot kategorisinde olup olmadığını doğrular.
        Tekstil, jean, pantolon, mont vb. giyim ve aksesuar ürünlerini kesin olarak engeller.
        """
        comb = f"{title} {url}".lower()
        # 1. Kara listede giyim/pantolon/aksesuar kelimesi varsa elenir
        for kw in config.EXCLUDE_KEYWORDS:
            if re.search(rf"\b{re.escape(kw.lower())}\b", comb):
                return False

        # 2. Pozitif ayakkabı göstergelerinden biri başlıkta veya URL'de geçiyor mu?
        indicators = getattr(config, "FOOTWEAR_INDICATORS", [])
        if any(ind in comb for ind in indicators):
            return True

        # 3. URL doğrudan ayakkabı/sneaker kategorisiyse
        url_low = url.lower()
        if any(term in url_low for term in ("/ayakkabi", "-ayakkabi", "/sneaker", "-sneaker", "spor-ayakkabi")):
            return True

        return False

    @classmethod
    def check_pre_filters(cls, product: ScrapedProduct) -> tuple[bool, str]:
        """
        Akakçe çapraz fiyat sorgusuna gitmeden önce temel marka, ayakkabı doğrulaması,
        kara liste, beden, maksimum sepet fiyatı ve minimum indirim oranı filtrelerini kontrol eder.
        """
        matched_brand = cls.is_brand_allowed(product.brand, product.title)
        if not matched_brand:
            return False, f"Marka hedef listede değil ({product.brand})"
        product.brand = matched_brand

        excluded_kw = cls.has_excluded_keyword(product.title, product.url)
        if excluded_kw:
            return False, f"Kara liste kelime tespit edildi ('{excluded_kw}')"

        if not cls.is_footwear(product.title, product.url):
            return False, "Ürün ayakkabı kategorisinde değil (Giyim/Pantolon/Aksesuar elendi)"

        if not product.sizes:
            return False, "Hedef numaralarda (Erkek 41-46 / Kadın 37-40.5) stok yok"

        if product.net_basket_price <= 0:
            return False, "Geçersiz sepet fiyatı"

        if product.net_basket_price > config.MAX_BASKET_PRICE:
            return (
                False,
                f"Sepet fiyatı maksimum eşiği aşıyor ({product.net_basket_price:.0f} TL > {config.MAX_BASKET_PRICE:.0f} TL)",
            )

        if product.total_discount_rate < config.MIN_TOTAL_DISCOUNT_RATE:
            return (
                False,
                f"Toplam indirim oranı yetersiz (%{product.total_discount_rate:.1f} < %{config.MIN_TOTAL_DISCOUNT_RATE:.1f})",
            )

        return True, ""

    @classmethod
    def compute_arbitrage_score(
        cls,
        product: ScrapedProduct,
        roi_gardrops: float,
        net_profit_gardrops: float,
        is_inflated_retail: bool = False,
    ) -> int:
        """
        0 - 100 arasında ağırlıklı Arbitraj Fırsat Puanı üretir:
        1. Toplam İndirim Oranı (Maks 30 Puan)
        2. ROI Yüzdesi ve Net Kâr (Maks 35 Puan)
        3. Marka Popülerliği / Likiditesi (Maks 20 Puan)
        4. Stok Azlığı (Son 1-3 Adet) ve Altın Numara Çeşitliliği (Maks 15 Puan)
        5. Sahte İndirim / Şişirilmiş Liste Fiyatı Cezası (-8 Puan)
        """
        discount_pts = min(30.0, max(0.0, (product.total_discount_rate - 25.0) * 0.75))

        roi_pts = min(25.0, max(0.0, roi_gardrops * 0.45))
        profit_bonus = min(10.0, max(0.0, (net_profit_gardrops - 300.0) / 120.0))
        financial_pts = min(35.0, roi_pts + profit_bonus)

        brand_key = product.brand.lower().strip()
        brand_pts = float(config.BRAND_POPULARITY_WEIGHTS.get(brand_key, 15))

        scarcity_and_size_pts = 0.0
        min_stock = product.min_stock_count
        if min_stock is not None and 1 <= min_stock <= 3:
            scarcity_and_size_pts += 8.0
        elif product.is_low_stock:
            scarcity_and_size_pts += 7.0
        else:
            scarcity_and_size_pts += 3.0

        available_norm_sizes = {s.normalized_size for s in product.sizes}
        golden_matches = len(available_norm_sizes & cls.GOLDEN_SIZES)
        scarcity_and_size_pts += min(7.0, golden_matches * 2.0)
        scarcity_and_size_pts = min(15.0, scarcity_and_size_pts)

        penalty = 8.0 if is_inflated_retail else 0.0
        total_score = int(round(discount_pts + financial_pts + brand_pts + scarcity_and_size_pts - penalty))
        return max(1, min(100, total_score))

    @classmethod
    def evaluate(
        cls,
        product: ScrapedProduct,
        market_floor_price: Optional[float] = None,
        market_floor_source: str = "Piyasa Referansı",
        price_trend_badge: str = "📊 Aktif Kampanya Fiyatı",
        best_official_price: Optional[float] = None,
        best_official_seller: Optional[str] = None,
        best_official_platform: Optional[str] = None,
        official_sellers_breakdown: Optional[list[str]] = None,
        comparison_details: Optional[object] = None,
    ) -> ArbitrageAnalysis:
        """
        Ürün için Gardrops/Sahibinden/Letgo ve Dolap senaryolarını kuruşu kuruşuna hesaplar.
        Aynı zamanda Akakçe, Amazon, idefix vb. resmi satıcılar arasında en ucuz alış kontrolünü yapar.
        """
        # comparison_details nesnesi verilmişse alanları otomatik çek
        if comparison_details is not None:
            if market_floor_price is None or market_floor_price <= 0:
                market_floor_price = getattr(comparison_details, "floor_price", None)
            if not market_floor_source or market_floor_source == "Piyasa Referansı":
                market_floor_source = getattr(comparison_details, "floor_source", market_floor_source)
            if best_official_price is None:
                best_off = getattr(comparison_details, "best_official_offer", None)
                if best_off:
                    best_official_price = getattr(best_off, "price", None)
                    best_official_seller = getattr(best_off, "seller_name", None)
                    best_official_platform = getattr(best_off, "platform", None)
            if official_sellers_breakdown is None:
                official_sellers_breakdown = getattr(comparison_details, "official_sellers_breakdown", [])

        buy_cost = round(product.net_basket_price, 2)

        if market_floor_price is None or market_floor_price <= 0:
            market_floor_price = round(product.retail_price * 0.72, 2)
            market_floor_source = "Liste Fiyatı x 0.72 (Tahmini Piyasa Dip)"

        # Sahte indirim / şişirilmiş liste fiyatı tespiti:
        # Eğer mağazanın üstü çizili liste fiyatı, piyasadaki gerçek dip fiyatın %55'inden daha fazlaysa
        is_inflated_retail = bool(market_floor_price > 0 and product.retail_price > market_floor_price * 1.55)

        # Önerilen Hızlı Satış Fiyatı: market_floor_price * 0.88 (%12 daha ucuz)
        suggested_sell_price = round(market_floor_price * config.SUGGESTED_SELL_DISCOUNT_MULTIPLIER, 2)

        # Senaryo 1: Gardrops / Sahibinden (Param Güvende) / Letgo (%6 + 95 TL)
        gardrops_commission = round(suggested_sell_price * config.GARDROPS_COMMISSION_RATE, 2)
        gardrops_expense = round(gardrops_commission + config.SHIPPING_COST_TL, 2)
        net_profit_gardrops = round(suggested_sell_price - buy_cost - gardrops_expense, 2)
        roi_gardrops = round((net_profit_gardrops / buy_cost) * 100.0, 1) if buy_cost > 0 else 0.0

        # Senaryo 2: Dolap Uygulaması (%14 komisyon + %1 stopaj + 95 TL kargo)
        dolap_total_rate = config.DOLAP_COMMISSION_RATE + config.DOLAP_WITHHOLDING_TAX_RATE
        dolap_commission = round(suggested_sell_price * dolap_total_rate, 2)
        dolap_expense = round(dolap_commission + config.SHIPPING_COST_TL, 2)
        net_profit_dolap = round(suggested_sell_price - buy_cost - dolap_expense, 2)
        roi_dolap = round((net_profit_dolap / buy_cost) * 100.0, 1) if buy_cost > 0 else 0.0

        score = cls.compute_arbitrage_score(
            product=product,
            roi_gardrops=roi_gardrops,
            net_profit_gardrops=net_profit_gardrops,
            is_inflated_retail=is_inflated_retail,
        )

        passed_pre, pre_reason = cls.check_pre_filters(product)
        is_profitable = passed_pre
        rejection_reason = pre_reason

        # Çoklu Resmi Satıcı / Pazaryeri Fiyat Koruması:
        # Eğer bu ürün Amazon, idefix, Hepsiburada, Trendyol vb. yerlerde başka bir resmi satıcıda
        # taranan sitemizden daha ucuza veya eşit fiyata satılıyorsa, bu mağazadan almak zarar ettirir!
        is_buy_source_cheapest = True
        cheaper_source_alert: Optional[str] = None
        if best_official_price is not None and best_official_price > 0:
            # Satın alınacak kaynak mağaza zaten piyasadaki en ucuz teklif mi?
            # 1. Alış maliyeti best_official_price ile aynı/yakınsa (fark <= 20 TL) en ucuzdur.
            # 2. Veya ürünün site_name'i best_official_platform ile eşleşiyorsa kendisidir.
            is_same_deal = (
                buy_cost <= best_official_price + 20.0
                or (best_official_platform and (
                    best_official_platform.lower() in product.site_name.lower()
                    or product.site_name.lower() in best_official_platform.lower()
                ))
            )
            if not is_same_deal and buy_cost > best_official_price + 20.0:
                is_buy_source_cheapest = False
                cheaper_source_alert = (
                    f"⚠️ Piyasada daha ucuz resmi satıcı var: {best_official_platform or 'Pazaryeri'} "
                    f"({best_official_seller or 'Resmi Satıcı'}) {best_official_price:,.0f} TL < {buy_cost:,.0f} TL"
                ).replace(",", ".")
                if is_profitable:
                    is_profitable = False
                    rejection_reason = cheaper_source_alert

        # Bağımsız resmi piyasa doğrulaması kontrolü:
        # Eğer piyasada en az bir bağımsız rakip mağaza veya pazaryeri teklifi yoksa ve REQUIRE_VERIFIED_MARKET_FLOOR aktifse,
        # fırsat teyitsiz kabul edilir.
        has_independent_competitor = bool(
            (
                comparison_details is not None
                and getattr(comparison_details, "all_official_offers", None)
                and any(
                    product.site_name.lower().strip() not in off.platform.lower()
                    and product.site_name.lower().strip() not in off.seller_name.lower()
                    for off in comparison_details.all_official_offers
                )
            )
            or (
                market_floor_source
                and any(
                    kw in market_floor_source.lower()
                    for kw in (
                        "resmi satıcı", "akakçe", "akakce", "çapraz", "fashfed",
                        "superstep", "occasion", "barçın", "barcin", "korayspor",
                        "sportive", "instreet", "amazon", "idefix"
                    )
                )
            )
        )

        if getattr(config, "REQUIRE_VERIFIED_MARKET_FLOOR", False) and not has_independent_competitor:
            is_buy_source_cheapest = False
            if is_profitable:
                is_profitable = False
                rejection_reason = "Piyasada bağımsız resmi satıcı / pazaryeri fiyat doğrulaması bulunamadı (Teyitsiz fırsat bildirilmez)"

        if is_profitable:
            if net_profit_gardrops < config.MIN_NET_PROFIT_TL:
                is_profitable = False
                rejection_reason = (
                    f"Net kâr eşik altında ({net_profit_gardrops:.0f} TL < {config.MIN_NET_PROFIT_TL:.0f} TL)"
                )
            elif roi_gardrops < config.MIN_ROI_PERCENT:
                is_profitable = False
                rejection_reason = f"ROI eşik altında (%{roi_gardrops:.1f} < %{config.MIN_ROI_PERCENT:.1f})"

        return ArbitrageAnalysis(
            product=product,
            buy_cost=buy_cost,
            market_floor_price=round(market_floor_price, 2),
            market_floor_source=market_floor_source,
            suggested_sell_price=suggested_sell_price,
            gardrops_expense=gardrops_expense,
            net_profit_gardrops=net_profit_gardrops,
            roi_gardrops=roi_gardrops,
            dolap_expense=dolap_expense,
            net_profit_dolap=net_profit_dolap,
            roi_dolap=roi_dolap,
            arbitrage_score=score,
            is_profitable_opportunity=is_profitable,
            rejection_reason=rejection_reason,
            price_trend_badge=price_trend_badge,
            is_inflated_retail_price=is_inflated_retail,
            best_official_price=best_official_price,
            best_official_seller=best_official_seller,
            best_official_platform=best_official_platform,
            official_sellers_breakdown=official_sellers_breakdown or [],
            is_buy_source_cheapest=is_buy_source_cheapest,
            cheaper_source_alert=cheaper_source_alert,
        )
