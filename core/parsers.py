import re
from dataclasses import dataclass
from typing import Any, Optional

import config


@dataclass(slots=True)
class NormalizedSizeInfo:
    """Beden çözümleme sonucunu hem orijinal metin hem de hedef numara olarak tutar."""

    original_text: str
    normalized_size: str
    is_fractional: bool = False
    stock_count: Optional[int] = None

    @property
    def display_label(self) -> str:
        base = (
            f"{self.normalized_size} ({self.original_text})"
            if self.is_fractional and self.original_text != self.normalized_size
            else self.normalized_size
        )
        if self.stock_count is not None and 0 < self.stock_count <= 3:
            return f"{base} [Son {self.stock_count}!]"
        return base


@dataclass(slots=True)
class BasketPriceResult:
    """Ürünün liste, vitrin ve sepetteki net fiyatlarını ve indirim oranını tutar."""

    retail_price: float
    sale_price: float
    net_basket_price: float
    total_discount_rate: float
    basket_discount_rate: float
    campaign_name: str


class SKUNormalizer:
    """
    Farklı e-ticaret sitelerindeki model kodlarını (SKU) standartlaştırarak
    siteler arası ve Akakçe ile birebir eşleştirme yapılmasını sağlar.
    Örn:
      - 'CW2288-111', 'CW2288.111', 'NK-CW2288 111' -> canonical: 'CW2288111', display: 'CW2288-111'
      - 'ID1488', 'AD-ID1488' -> canonical: 'ID1488', display: 'ID1488'
      - 'U9060ECA', 'NB-U9060ECA' -> canonical: 'U9060ECA', display: 'U9060ECA'
    """

    BRAND_PREFIX_PATTERN = re.compile(
        r"^(?:NK|NIKE|AD|ADIDAS|NB|NEWBALANCE|PM|PUMA|LC|LACOSTE|VN|VANS|CV|CONVERSE|"
        r"SL|SALOMON|AS|ASICS|TB|TIMBERLAND|TNF|THENORTHFACE|SK|SKECHERS|TH|TOMMY|"
        r"CL|COLUMBIA|GA|GANT|NA|NAUTICA|HH|HELLYHANSEN|UA|UNDERARMOUR)[\s\-_\.]+",
        re.IGNORECASE,
    )

    # Bilinen marka SKU kalıpları (Başlık veya URL içinden model kodu yakalamak için)
    KNOWN_SKU_PATTERNS = [
        # Nike / Jordan: CW2288-111, DV0788-001, FD0736-100, DM0121-100
        re.compile(r"\b([A-Z]{2}\d{4}[\s\-_\.]\d{3})\b", re.IGNORECASE),
        # Nike eski format: 315122-111
        re.compile(r"\b(\d{6}[\s\-_\.]\d{3})\b"),
        # Adidas: ID1488, IE0586, IG1024, HQ8708, GY0042, B75806, FX5502, IF8840
        re.compile(r"\b([A-Z]{1,2}\d{4,5})\b", re.IGNORECASE),
        # Asics: 1201A789-020, 1011B700.001, 1203A383-102
        re.compile(r"\b(\d{4}[A-Z]\d{3}[\s\-_\.]\d{3})\b", re.IGNORECASE),
        # Salomon: L47451800, L41252900
        re.compile(r"\b(L\d{8})\b", re.IGNORECASE),
        # New Balance: MR530SG, U9060ECA, M2002RDM, BB550WT1, U327WGC
        re.compile(r"\b([MUW]{1,2}\d{3,4}[A-Z0-9]{2,4})\b", re.IGNORECASE),
        re.compile(r"\b(BB\d{3}[A-Z0-9]{2,4})\b", re.IGNORECASE),
        # Puma: 392290-01, 389289.02, 374915-01, 40036701, 400367-01
        re.compile(r"\b([34]\d{5}[\s\-_\.]?\d{2})\b"),
        # Vans: VN000D3HY281, VN0A38G1P0S1
        re.compile(r"\b(VN0[A-Z0-9]{7,10})\b", re.IGNORECASE),
        # Converse: M9160C, 162050C, A06558C (En az 4 rakam içermeli, CLASSIC gibi kelimelerle karışmamalı)
        re.compile(r"\b([A-Z]?\d{4,6}C)\b", re.IGNORECASE),
        # Skechers: 232457-BBK, 149710-WHT, 52631-NVY
        re.compile(r"\b(\d{5,6}[\s\-_\.][A-Z]{3,4})\b", re.IGNORECASE),
        # Timberland: TB0A28592311, A2859
        re.compile(r"\b(TB0[A-Z0-9]{8,10})\b", re.IGNORECASE),
        # Lacoste: 745SMA0034, 746SFA0012
        re.compile(r"\b(7\d{2}[A-Z]{3}\d{4}[A-Z0-9]*)\b", re.IGNORECASE),
        # InStreet / FLO 9 Haneli Ürün Kodu: 101203973, 102266181
        re.compile(r"\b(10\d{7})\b"),
    ]

    # Sadece beden son eki olan ifadeleri temizlemek için (Örn: CW2288-111-42,5)
    TRAILING_SIZE_PATTERN = re.compile(
        r"[\-_\/](?:EU|TR)?(?:3[5-9]|4[0-8])(?:[\.,]5|[\s\-_]?[12]\/3)?$",
        re.IGNORECASE,
    )

    @classmethod
    def clean_raw_sku(cls, raw_sku: str, brand: str = "") -> str:
        if not raw_sku:
            return ""
        sku = str(raw_sku).strip().upper()
        sku = cls.BRAND_PREFIX_PATTERN.sub("", sku)
        if brand:
            brand_clean = re.sub(r"[^A-Z0-9]", "", brand.upper())
            if brand_clean and sku.startswith(brand_clean + "-"):
                sku = sku[len(brand_clean) + 1 :]
        sku = cls.TRAILING_SIZE_PATTERN.sub("", sku)
        return sku.strip(" -_./")

    @classmethod
    def extract_sku_from_text(cls, *texts: str) -> str:
        """Verilen başlık, URL veya açıklama metinlerinden bilinen SKU formatlarını yakalar."""
        for text in texts:
            if not text:
                continue
            cleaned_text = str(text).upper()
            for pattern in cls.KNOWN_SKU_PATTERNS:
                match = pattern.search(cleaned_text)
                if match:
                    candidate = match.group(1).strip()
                    # Yıl veya rastgele 4 haneli sayıları elemek için kontrol
                    if candidate not in ("2023", "2024", "2025", "2026", "1000", "2000", "3000"):
                        return candidate
        return ""

    @classmethod
    def normalize(cls, raw_sku: str = "", title: str = "", url: str = "", brand: str = "") -> tuple[str, str]:
        """
        Girdi verilerinden (canonical_sku, display_sku) ikilisini döndürür.
        canonical_sku: Sadece büyük harf ve rakamlardan oluşan birleştirilmiş benzersiz anahtar (Örn: CW2288111).
        display_sku: Okunabilir standart model kodu (Örn: CW2288-111).
        """
        extracted = cls.extract_sku_from_text(raw_sku, title, url)
        base_sku = extracted if extracted else cls.clean_raw_sku(raw_sku, brand=brand)

        if not base_sku and url:
            # URL'nin son segmentinden model kodu yakalamayı dene
            slug = url.rstrip("/").split("/")[-1].split("?")[0]
            base_sku = cls.extract_sku_from_text(slug.replace("-", " ")) or slug.upper()[-14:]

        display_sku = cls.clean_raw_sku(base_sku, brand=brand)
        display_sku = re.sub(r"[\s_\.]+", "-", display_sku)

        canonical_sku = re.sub(r"[^A-Z0-9]", "", display_sku.upper())
        if not canonical_sku and title:
            canonical_sku = re.sub(r"[^A-Z0-9]", "", title.upper())[:24]
            display_sku = canonical_sku[:12]

        return canonical_sku, display_sku


class SizeNormalizer:
    """
    Buçuklu ve Kesirli Numara Çözücü.
    - Virgülleri noktaya çevirir (42,5 -> 42.5), 'EU', 'TR' gibi ekleri siler.
    - Adidas, Salomon ve New Balance kesirli numaralarını (1/2, 1/3, 2/3, ½, ⅓, ⅔, .33, .66)
      otomatik olarak standart buçuklu hedef numaralara eşler.
    """

    # Kesirli ve ondalıklı Adidas/Salomon/New Balance numaralarının standart hedef numara haritası
    EXPLICIT_FRACTION_MAP: dict[str, str] = {
        # Kadın Kesirli -> Standart Buçuklu Eşleşmeleri
        "36 2/3": "36.5",
        "36.6": "36.5",
        "36.66": "36.5",
        "36.67": "36.5",
        "37 1/3": "37.5",
        "37.3": "37.5",
        "37.33": "37.5",
        "37 1/2": "37.5",
        "38 2/3": "38.5",
        "38.6": "38.5",
        "38.66": "38.5",
        "38.67": "38.5",
        "38 1/2": "38.5",
        "39 1/3": "39.5",
        "39.3": "39.5",
        "39.33": "39.5",
        "39 1/2": "39.5",
        "40 2/3": "40.5",
        "40.6": "40.5",
        "40.66": "40.5",
        "40.67": "40.5",
        "40 1/2": "40.5",
        # Erkek Kesirli -> Standart Buçuklu Eşleşmeleri
        "41 1/3": "41.5",
        "41.3": "41.5",
        "41.33": "41.5",
        "41 1/2": "41.5",
        "42 2/3": "42.5",
        "42.6": "42.5",
        "42.66": "42.5",
        "42.67": "42.5",
        "42 1/2": "42.5",
        "43 1/3": "43.5",
        "43.3": "43.5",
        "43.33": "43.5",
        "43 1/2": "43.5",
        "44 2/3": "44.5",
        "44.6": "44.5",
        "44.66": "44.5",
        "44.67": "44.5",
        "44 1/2": "44.5",
        "45 1/3": "45.5",
        "45.3": "45.5",
        "45.33": "45.5",
        "45 1/2": "45.5",
        "46 2/3": "46",
        "46.6": "46",
        "46.66": "46",
        "46.67": "46",
    }

    UNICODE_FRACTIONS = {
        "½": " 1/2",
        "⅓": " 1/3",
        "⅔": " 2/3",
    }

    @classmethod
    def normalize_single(cls, raw_size: Any, stock_count: Optional[int] = None) -> Optional[NormalizedSizeInfo]:
        """
        Tek bir beden metnini temizler ve NormalizedSizeInfo döndürür.
        Geçersiz bir beden metni ise None döner.
        """
        if raw_size is None:
            return None

        original = str(raw_size).strip()
        if not original:
            return None

        cleaned = original.upper()

        # Unicode kesir karakterlerini standart kesire dönüştür
        for uni_char, repl in cls.UNICODE_FRACTIONS.items():
            cleaned = cleaned.replace(uni_char, repl)

        # Gereksiz kelimeleri temizle (EU, EUR, TR, US, UK değilse)
        cleaned = re.sub(r"\b(?:EU|EUR|TR|BEDEN|NUMARA|SIZE|NO)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace(",", ".")
        # Tireli kesirleri boşluğa çevir: "42-2/3" -> "42 2/3", "42/5" -> "42.5"
        cleaned = re.sub(r"(\d{2})[\-_](\d\/\d)", r"\1 \2", cleaned)
        cleaned = re.sub(r"(\d{2})\/5\b", r"\1.5", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_./")

        # 1. Doğrudan harita eşleşmesi kontrolü
        if cleaned in cls.EXPLICIT_FRACTION_MAP:
            mapped = cls.EXPLICIT_FRACTION_MAP[cleaned]
            return NormalizedSizeInfo(
                original_text=original,
                normalized_size=mapped,
                is_fractional=True,
                stock_count=stock_count,
            )

        # 2. Genel kesirli kalıp kontrolü: "XX 1/3", "XX 2/3", "XX 1/2"
        frac_match = re.search(r"\b(3[5-9]|4[0-8])\s*([12]\/[23])\b", cleaned)
        if frac_match:
            base_num = frac_match.group(1)
            fraction = frac_match.group(2)
            key = f"{base_num} {fraction}"
            if key in cls.EXPLICIT_FRACTION_MAP:
                mapped = cls.EXPLICIT_FRACTION_MAP[key]
            else:
                mapped = f"{base_num}.5"
            return NormalizedSizeInfo(
                original_text=original,
                normalized_size=mapped,
                is_fractional=True,
                stock_count=stock_count,
            )

        # 3. Ondalıklı kesir veya buçuklu kalıp kontrolü: "42.5", "42.6", "43.33", "42.0"
        dec_match = re.search(r"\b(3[5-9]|4[0-8])(?:\.(\d{1,2}))?\b", cleaned)
        if dec_match:
            base_num = dec_match.group(1)
            dec_part = dec_match.group(2)

            if not dec_part or dec_part in ("0", "00"):
                return NormalizedSizeInfo(
                    original_text=original,
                    normalized_size=base_num,
                    is_fractional=False,
                    stock_count=stock_count,
                )

            full_dec = f"{base_num}.{dec_part}"
            if full_dec in cls.EXPLICIT_FRACTION_MAP:
                return NormalizedSizeInfo(
                    original_text=original,
                    normalized_size=cls.EXPLICIT_FRACTION_MAP[full_dec],
                    is_fractional=True,
                    stock_count=stock_count,
                )

            if dec_part in ("5", "50"):
                return NormalizedSizeInfo(
                    original_text=original,
                    normalized_size=f"{base_num}.5",
                    is_fractional=False,
                    stock_count=stock_count,
                )

            if dec_part.startswith(("3", "6", "7")):
                mapped = "46" if base_num == "46" else f"{base_num}.5"
                return NormalizedSizeInfo(
                    original_text=original,
                    normalized_size=mapped,
                    is_fractional=True,
                    stock_count=stock_count,
                )

        return None

    @classmethod
    def filter_target_sizes(
        cls,
        raw_sizes: list[Any],
        gender: str = "Unisex",
    ) -> list[NormalizedSizeInfo]:
        """
        Ürünün stoktaki beden listesini alır, normalize eder ve hedef erkek/kadın
        numaralarımızla (41-46 ve 37-40.5, buçuklu/kesirli dahil) eşleşenleri döndürür.
        """
        gender_lower = (gender or "unisex").lower()
        if "erkek" in gender_lower and "kadın" not in gender_lower and "kadin" not in gender_lower:
            allowed_targets = set(config.MEN_TARGET_SIZES)
        elif "kadın" in gender_lower or "kadin" in gender_lower:
            allowed_targets = set(config.WOMEN_TARGET_SIZES)
        else:
            allowed_targets = config.ALL_TARGET_SIZES

        matched: list[NormalizedSizeInfo] = []
        seen_normalized: set[str] = set()

        for item in raw_sizes:
            if isinstance(item, dict):
                size_str = (
                    item.get("size")
                    or item.get("name")
                    or item.get("value")
                    or item.get("label")
                    or item.get("attribute_value")
                    or ""
                )
                stock_val = item.get("stock") or item.get("quantity") or item.get("in_stock")
                in_stock = item.get("available", True)
                if in_stock is False or stock_val == 0:
                    continue
                stock_int: Optional[int] = None
                if isinstance(stock_val, int) and not isinstance(stock_val, bool):
                    stock_int = stock_val
                elif isinstance(stock_val, str) and stock_val.isdigit():
                    stock_int = int(stock_val)
                info = cls.normalize_single(size_str, stock_count=stock_int)
            else:
                info = cls.normalize_single(item)

            if info is None:
                continue

            # Eğer kategori Erkek olarak etiketlenmiş ama numara sadece Kadın hedefinde ise veya tam tersi:
            # Yine de ALL_TARGET_SIZES içinde olup olmadığını kontrol et
            if info.normalized_size in allowed_targets or (
                gender_lower == "unisex" and info.normalized_size in config.ALL_TARGET_SIZES
            ):
                dedup_key = f"{info.normalized_size}:{info.original_text}"
                if dedup_key not in seen_normalized:
                    seen_normalized.add(dedup_key)
                    matched.append(info)

        # Numaraları küçükten büyüğe sırala
        matched.sort(key=lambda x: float(x.normalized_size))
        return matched


class BasketPromotionParser:
    """
    Sepetteki kademeli ve yüzdelik indirimleri hesaplayarak en düşük net alış maliyetini
    (net_basket_price) kuruşu kuruşuna bulan motor.
    """

    # 1. "Sepette %25 İndirim", "Sepette Ekstra %20 İndirim", "%30 Sepet İndirimi"
    PERCENT_DISCOUNT_PATTERNS = [
        re.compile(
            r"sepette\s*(?:ekstra\s*|net\s*|ilave\s*)?%\s*(\d{1,2})\s*(?:indirim|fırsat)?",
            re.IGNORECASE,
        ),
        re.compile(
            r"%\s*(\d{1,2})\s*sepet(?:te)?\s*indirim",
            re.IGNORECASE,
        ),
        re.compile(
            r"sepete\s*özel\s*(?:ekstra\s*)?%\s*(\d{1,2})",
            re.IGNORECASE,
        ),
        re.compile(
            r"ekstra\s*%\s*(\d{1,2})\s*(?:sepet\s*)?indirim",
            re.IGNORECASE,
        ),
    ]

    # 2. Kademeli TL İndirimi: "3000 TL Üzerine 750 TL İndirim", "4.000 TL ve Üzeri 1.000 TL İndirim"
    TIERED_TL_PATTERNS = [
        re.compile(
            r"([\d\.]+)\s*tl\s*(?:ve\s*)?(?:üzeri(?:ne|nde)?|alışveriş(?:e|lerde)?|'ye|'e)\s*([\d\.]+)\s*tl\s*indirim",
            re.IGNORECASE,
        ),
        re.compile(
            r"([\d\.]+)\s*tl'lik\s*alışverişe\s*([\d\.]+)\s*tl\s*indirim",
            re.IGNORECASE,
        ),
    ]

    # 3. Kademeli Yüzde İndirimi: "3000 TL Üzerine %25 İndirim"
    TIERED_PERCENT_PATTERN = re.compile(
        r"([\d\.]+)\s*tl\s*(?:ve\s*)?(?:üzeri(?:ne|nde)?|alışveriş(?:e|lerde)?|'ye|'e)\s*%\s*(\d{1,2})\s*indirim",
        re.IGNORECASE,
    )

    # 4. Doğrudan Sepet Fiyatı: "Sepette 1.899 TL", "Sepette Net 2.499,90 TL", "Sepet Fiyatı: 1.750 TL"
    DIRECT_BASKET_PRICE_PATTERNS = [
        re.compile(
            r"sepet(?:te(?:ki)?|e\s*özel)?\s*(?:net\s*)?(?:fiyatı?\s*:?\s*)?([\d\.]+(?:,\d{1,2})?)\s*tl",
            re.IGNORECASE,
        ),
    ]

    # 5. Doğrudan Sabit TL İndirimi: "Sepette 500 TL İndirim", "Sepette Anında 400 TL İndirim"
    FLAT_TL_DISCOUNT_PATTERN = re.compile(
        r"sepette\s*(?:anında\s*|ekstra\s*|net\s*)?([\d\.]+)\s*tl\s*indirim",
        re.IGNORECASE,
    )

    @staticmethod
    def parse_turkish_price(raw_value: Any) -> float:
        """
        Türkçe veya uluslararası fiyat metinlerini (Örn: '4.599,90 TL', '3,299.00', 2499.9, '1.899')
        güvenli bir şekilde float değerine dönüştürür.
        """
        if raw_value is None:
            return 0.0
        if isinstance(raw_value, (int, float)):
            return round(float(raw_value), 2)

        text = str(raw_value).strip()
        if not text:
            return 0.0

        # Para birimi ve boşlukları temizle
        cleaned = re.sub(r"[^\d,\.]", "", text)
        if not cleaned:
            return 0.0

        # Hem nokta hem virgül varsa (Örn: 4.599,90 veya 4,599.90)
        if "." in cleaned and "," in cleaned:
            last_dot = cleaned.rfind(".")
            last_comma = cleaned.rfind(",")
            if last_comma > last_dot:
                # Türkçe format: 4.599,90
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                # ABD formatı: 4,599.90
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            # Sadece virgül varsa: virgülden sonra 3 hane varsa binliktir (4,500), 1-2 hane varsa kuruştur (4599,90)
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 2:
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            # Sadece nokta varsa: noktadan sonra 3 hane varsa ve tam sayıysa binlik ayırıcıdır (Örn: '1.899' veya '4.500')
            parts = cleaned.split(".")
            if len(parts) > 2:
                cleaned = "".join(parts)
            elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 2:
                # 1.899 -> 1899.0 (Ayakkabı fiyatları TL bazında 1.899 TL'dir, 1 TL 899 kuruş değildir)
                cleaned = "".join(parts)

        try:
            val = float(cleaned)
            return round(val, 2) if val > 0 else 0.0
        except ValueError:
            return 0.0

    @classmethod
    def calculate(
        cls,
        retail_price: Any,
        sale_price: Any = None,
        api_basket_price: Any = None,
        campaign_texts: Optional[list[str]] = None,
    ) -> BasketPriceResult:
        """
        Ürünün liste fiyatını, indirimli satış fiyatını, varsa API'den gelen sepet fiyatını ve
        kampanya metinlerini analiz ederek en düşük gerçek `net_basket_price` değerini hesaplar.
        """
        r_price = cls.parse_turkish_price(retail_price)
        s_price = cls.parse_turkish_price(sale_price)
        b_price = cls.parse_turkish_price(api_basket_price)

        if s_price <= 0 and r_price > 0:
            s_price = r_price
        if r_price <= 0 and s_price > 0:
            r_price = s_price
        if s_price > r_price:
            r_price = s_price

        candidates: list[tuple[float, str]] = [(s_price, "Vitrin İndirimi")]

        if 0 < b_price < s_price:
            extra_pct = round(((s_price - b_price) / s_price) * 100)
            candidates.append((b_price, f"Sepette Ekstra %{extra_pct} İndirim"))

        texts = [t.strip() for t in (campaign_texts or []) if t and str(t).strip()]

        for text in texts:
            # A) Kademeli TL İndirimi: "3000 TL Üzerine 750 TL İndirim"
            matched_tiered_tl = False
            for pattern in cls.TIERED_TL_PATTERNS:
                for match in pattern.finditer(text):
                    threshold = cls.parse_turkish_price(match.group(1))
                    discount_tl = cls.parse_turkish_price(match.group(2))
                    if threshold > 0 and 0 < discount_tl < threshold and s_price >= threshold:
                        computed = round(s_price - discount_tl, 2)
                        if computed > 0:
                            candidates.append(
                                (computed, f"{int(threshold)} TL Üzerine {int(discount_tl)} TL İndirim")
                            )
                            matched_tiered_tl = True

            # B) Kademeli Yüzde İndirimi: "3000 TL Üzerine %25 İndirim"
            for match in cls.TIERED_PERCENT_PATTERN.finditer(text):
                threshold = cls.parse_turkish_price(match.group(1))
                pct = float(match.group(2))
                if threshold > 0 and 0 < pct <= 75 and s_price >= threshold:
                    computed = round(s_price * (1.0 - pct / 100.0), 2)
                    candidates.append((computed, f"{int(threshold)} TL Üzerine %{int(pct)} İndirim"))

            # C) Yüzdelik Sepet İndirimi: "Sepette %25 İndirim" veya "Sepette Ekstra %20"
            for pattern in cls.PERCENT_DISCOUNT_PATTERNS:
                match = pattern.search(text)
                if match:
                    pct = float(match.group(1))
                    if 0 < pct <= 75:
                        computed = round(s_price * (1.0 - pct / 100.0), 2)
                        candidates.append((computed, f"Sepette %{int(pct)} İndirim"))
                        break

            # D) Sabit TL Sepet İndirimi (Eğer kademeli TL indirimi değilse): "Sepette 500 TL İndirim"
            if not matched_tiered_tl:
                flat_match = cls.FLAT_TL_DISCOUNT_PATTERN.search(text)
                if flat_match:
                    discount_tl = cls.parse_turkish_price(flat_match.group(1))
                    if 0 < discount_tl < s_price * 0.75:
                        computed = round(s_price - discount_tl, 2)
                        candidates.append((computed, f"Sepette {int(discount_tl)} TL İndirim"))

            # E) Doğrudan Sepet Fiyatı Metni: "Sepette 1.899 TL"
            if "%" not in text and not matched_tiered_tl:
                for pattern in cls.DIRECT_BASKET_PRICE_PATTERNS:
                    match = pattern.search(text)
                    if match:
                        direct_val = cls.parse_turkish_price(match.group(1))
                        if 250.0 <= direct_val < s_price:
                            candidates.append((direct_val, f"Sepette Net {direct_val:,.0f} TL".replace(",", ".")))

        # En düşük geçerli sepet fiyatını seç
        valid_candidates = [(price, label) for price, label in candidates if price > 0]
        if not valid_candidates:
            return BasketPriceResult(
                retail_price=r_price,
                sale_price=s_price,
                net_basket_price=s_price,
                total_discount_rate=0.0,
                basket_discount_rate=0.0,
                campaign_name="Standart Fiyat",
            )

        best_price, best_campaign = min(valid_candidates, key=lambda x: x[0])

        # Eğer metinde özel kampanya adı varsa ve API sepet fiyatı aynıysa metindeki açıklamayı tercih et
        if texts and best_campaign.startswith("Sepette Ekstra"):
            for price, label in valid_candidates:
                if abs(price - best_price) <= 1.0 and label != best_campaign:
                    best_campaign = label
                    break

        total_discount_rate = (
            round(((r_price - best_price) / r_price) * 100.0, 1) if r_price > best_price and r_price > 0 else 0.0
        )
        basket_discount_rate = (
            round(((s_price - best_price) / s_price) * 100.0, 1) if s_price > best_price and s_price > 0 else 0.0
        )

        if best_campaign == "Vitrin İndirimi" and texts:
            best_campaign = texts[0][:60]

        return BasketPriceResult(
            retail_price=round(r_price, 2),
            sale_price=round(s_price, 2),
            net_basket_price=round(best_price, 2),
            total_discount_rate=total_discount_rate,
            basket_discount_rate=basket_discount_rate,
            campaign_name=best_campaign,
        )
