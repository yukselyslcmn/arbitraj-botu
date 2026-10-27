import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
from core.arbitrage_engine import ScrapedProduct

logger = logging.getLogger("arbitraj.storage")


class StateStorage:
    """
    `data/products_state.json` üzerinde ürün durumlarını `canonical_sku` ve `site_key`
    bazında saklayan, ilk çalışma spam korumasını, 5 yetkili orijinal mağaza (Occasion,
    SuperStep, InStreet, Barçın, Korayspor) arası çapraz fiyat kıyaslamasını ve
    %5 ekstra fiyat düşüş kuralını yöneten sınıf.
    """

    def __init__(self, state_file: Path = config.STATE_FILE_PATH) -> None:
        self.state_file = state_file
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "meta": {
                "initialized_at": None,
                "last_updated_at": None,
                "first_run_completed": False,
            },
            "products": {},
        }
        self.is_first_run: bool = True
        self._load()

    def _load(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self.is_first_run = True
            return

        try:
            raw_text = self.state_file.read_text(encoding="utf-8").strip()
            if not raw_text or raw_text == "{}":
                self.is_first_run = True
                return

            loaded = json.loads(raw_text)
            if isinstance(loaded, dict) and "products" in loaded:
                self.state = loaded
                products_dict = self.state.get("products", {})
                first_run_flag = self.state.get("meta", {}).get("first_run_completed", False)
                self.is_first_run = not (first_run_flag and len(products_dict) > 0)
            elif isinstance(loaded, dict):
                # Eski veya düz format uyumluluğu
                self.state["products"] = loaded
                self.is_first_run = len(loaded) == 0
        except Exception as exc:
            logger.warning("Durum dosyası okunamadı, yeni durum başlatılıyor (%s): %s", self.state_file, exc)
            self.is_first_run = True

    def save(self) -> None:
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            if not self.state["meta"].get("initialized_at"):
                self.state["meta"]["initialized_at"] = now_iso
            self.state["meta"]["last_updated_at"] = now_iso
            self.state["meta"]["first_run_completed"] = True

            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temp_file.replace(self.state_file)

    def upsert_scraped_product(self, product: ScrapedProduct) -> None:
        """
        Kazınan her ürünü canonical_sku -> sites -> site_key altında günceller.
        """
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            sku_map = self.state["products"].setdefault(
                product.canonical_sku,
                {
                    "canonical_sku": product.canonical_sku,
                    "display_sku": product.display_sku,
                    "brand": product.brand,
                    "title": product.title,
                    "gender": product.gender,
                    "sites": {},
                },
            )
            if not sku_map.get("gender") and product.gender:
                sku_map["gender"] = product.gender
            sites_map = sku_map.setdefault("sites", {})
            existing_site = sites_map.get(product.site_key, {})

            lowest_seen = existing_site.get("lowest_seen_price")
            if lowest_seen is None or product.net_basket_price < lowest_seen:
                lowest_seen = product.net_basket_price

            new_sizes = [s.normalized_size for s in product.sizes] if product.sizes else existing_site.get("sizes", [])
            new_labels = [s.display_label for s in product.sizes] if product.sizes else existing_site.get("size_labels", [])

            sites_map[product.site_key] = {
                "site_name": product.site_name,
                "url": product.url,
                "image_url": product.image_url,
                "retail_price": product.retail_price,
                "sale_price": product.sale_price,
                "net_basket_price": product.net_basket_price,
                "total_discount_rate": product.total_discount_rate,
                "campaign_name": product.campaign_name,
                "sizes": new_sizes,
                "size_labels": new_labels,
                "lowest_seen_price": lowest_seen,
                "lowest_notified_price": existing_site.get("lowest_notified_price"),
                "last_notified_at": existing_site.get("last_notified_at"),
                "last_seen_at": now_iso,
            }

    def should_notify(
        self,
        product: ScrapedProduct,
        notify_on_first_run: bool = config.NOTIFY_ON_FIRST_RUN,
        drop_threshold_pct: float = config.PRICE_DROP_RE_NOTIFY_PERCENT,
    ) -> tuple[bool, str]:
        """
        Ürün için bildirim atılıp atılmayacağına karar verir.
        Dönüş: (bildirim_atilsin_mi, bildirim_tipi)
        bildirim_tipi: 'NEW_OPPORTUNITY' | 'EXTRA_PRICE_CRASH' | 'SKIP_FIRST_RUN' | 'ALREADY_NOTIFIED'
        """
        with self._lock:
            sku_entry = self.state["products"].get(product.canonical_sku, {})
            site_entry = sku_entry.get("sites", {}).get(product.site_key, {})
            lowest_notified = site_entry.get("lowest_notified_price")

            # İlk çalışma spam koruması
            if self.is_first_run and not notify_on_first_run:
                # İlk çalışmada gördüğümüz ürünlerin mevcut fiyatını baz fiyat olarak kaydedelim ki
                # sonraki çalışmalarda mevcut eski ürünler bir anda "yeni fırsat" diye yağmasın,
                # sadece yeni eklenenler veya %5 ekstra düşenler bildirilsin.
                if site_entry and lowest_notified is None:
                    site_entry["lowest_notified_price"] = product.net_basket_price
                    site_entry["last_notified_at"] = datetime.now(timezone.utc).isoformat()
                return False, "SKIP_FIRST_RUN"

            # Daha önce hiç bildirilmemiş fırsat ürünü
            if lowest_notified is None or lowest_notified <= 0:
                return True, "NEW_OPPORTUNITY"

            # Daha önce bildirilmiş: Fiyat, kaydettiğimiz en düşük fiyattan en az %5 daha aşağı düştü mü?
            required_max_price = round(lowest_notified * (1.0 - (drop_threshold_pct / 100.0)), 2)
            if product.net_basket_price <= required_max_price:
                return True, "EXTRA_PRICE_CRASH"

            return False, "ALREADY_NOTIFIED"

    def mark_notified(self, product: ScrapedProduct) -> None:
        """Bildirim gönderildikten sonra lowest_notified_price ve last_notified_at alanlarını günceller."""
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            sku_entry = self.state["products"].setdefault(product.canonical_sku, {"sites": {}})
            site_entry = sku_entry.setdefault("sites", {}).setdefault(product.site_key, {})

            prev_lowest = site_entry.get("lowest_notified_price")
            if prev_lowest is None or product.net_basket_price < prev_lowest:
                site_entry["lowest_notified_price"] = product.net_basket_price
            site_entry["last_notified_at"] = now_iso

    def get_cross_site_floor_price(
        self,
        canonical_sku: str,
        exclude_site_key: str,
        brand: Optional[str] = None,
        gender: Optional[str] = None,
        model_tokens: Optional[list[str]] = None,
    ) -> Optional[tuple[float, str]]:
        """
        5 yetkili orijinal mağaza (Occasion, SuperStep, InStreet, Barçın, Korayspor) arasında:
        1. Birebir `canonical_sku` veya baz model kodu (ör. `747CFA0002` <-> `747CFA000221G`) eşleşmesi,
        2. Aynı Marka + Cinsiyet + Model Serisi (`model_tokens`) eşleşmesi
        yaparak diğer yetkili mağazalardaki en düşük satış/sepet fiyatını döndürür.
        """
        with self._lock:
            products_dict = self.state.get("products", {})
            sku_clean = (canonical_sku or "").upper().strip()
            is_internal_num = sku_clean.isdigit() and len(sku_clean) >= 8 and sku_clean.startswith("10")
            base_prefix = sku_clean[:10] if (len(sku_clean) >= 7 and not is_internal_num) else ""

            sku_matches: list[tuple[float, str]] = []
            model_matches: list[tuple[float, str]] = []

            brand_low = (brand or "").lower().strip()
            gender_low = (gender or "").lower().strip()
            sig_tokens = [t.lower() for t in (model_tokens or []) if len(t) >= 2]

            for other_sku, sku_entry in products_dict.items():
                if not isinstance(sku_entry, dict):
                    continue
                sites_map = sku_entry.get("sites", {})
                if not isinstance(sites_map, dict):
                    continue

                other_sku_clean = str(other_sku).upper().strip()
                is_sku_match = False
                if other_sku_clean == sku_clean:
                    is_sku_match = True
                elif base_prefix and len(other_sku_clean) >= 7 and not other_sku_clean.isdigit():
                    if other_sku_clean.startswith(base_prefix) or sku_clean.startswith(other_sku_clean[:10]):
                        is_sku_match = True

                is_model_match = False
                if not is_sku_match and brand_low and sig_tokens:
                    entry_brand = str(sku_entry.get("brand") or "").lower().strip()
                    if entry_brand == brand_low:
                        entry_title = str(sku_entry.get("title") or "").lower()
                        entry_gender = str(sku_entry.get("gender") or "").lower()
                        gender_ok = True
                        if "erkek" in gender_low and ("kadın" in entry_gender or "kadin" in entry_title):
                            if "erkek" not in entry_title and "unisex" not in entry_title:
                                gender_ok = False
                        elif ("kadın" in gender_low or "kadin" in gender_low) and "erkek" in entry_title:
                            if "kadın" not in entry_title and "kadin" not in entry_title and "unisex" not in entry_title:
                                gender_ok = False

                        if gender_ok:
                            has_specific_single = len(sig_tokens) == 1 and (
                                any(c.isdigit() or c == "-" for c in sig_tokens[0])
                                or sig_tokens[0]
                                in {
                                    "shox",
                                    "gazelle",
                                    "samba",
                                    "campus",
                                    "spezial",
                                    "palermo",
                                    "superstar",
                                    "forum",
                                    "cortez",
                                    "vomero",
                                    "pegasus",
                                    "dunk",
                                    "blazer",
                                }
                            )
                            if (len(sig_tokens) >= 2 and all(tok in entry_title for tok in sig_tokens[:2])) or (
                                has_specific_single and sig_tokens[0] in entry_title
                            ):
                                is_model_match = True

                if not (is_sku_match or is_model_match):
                    continue

                for s_key, s_data in sites_map.items():
                    if s_key == exclude_site_key or not isinstance(s_data, dict):
                        continue
                    price = s_data.get("net_basket_price") or s_data.get("sale_price")
                    s_name = str(s_data.get("site_name") or s_key)
                    if isinstance(price, (int, float)) and price >= 650.0:
                        if is_sku_match:
                            sku_matches.append((float(price), s_name))
                        else:
                            model_matches.append((float(price), f"{s_name} Model Eşleşmesi"))

            if sku_matches:
                sku_matches.sort(key=lambda x: x[0])
                return sku_matches[0]
            if model_matches:
                model_matches.sort(key=lambda x: x[0])
                return model_matches[0]
            return None

    def save_verified_market_floor(
        self,
        canonical_sku: str,
        floor_price: float,
        floor_source: str,
        akakce_url: Optional[str] = None,
    ) -> None:
        """
        Akakçe'den doğrulanan piyasa dip fiyatını `data/products_state.json` içine kaydeder.
        Böylece GitHub Actions (ABD/Avrupa veri merkezi IP'si) üzerinde Akakçe geo-block
        uygulasa bile doğrulanmış piyasa fiyatı ve Akakçe linki korunur.
        """
        with self._lock:
            sku_entry = self.state["products"].get(canonical_sku)
            if not sku_entry:
                return
            sku_entry["verified_market_floor_price"] = round(float(floor_price), 2)
            sku_entry["verified_market_floor_source"] = floor_source
            if akakce_url:
                sku_entry["verified_akakce_url"] = akakce_url

    def get_verified_market_floor(self, canonical_sku: str) -> Optional[tuple[float, str, Optional[str]]]:
        """Daha önce doğrulanıp kaydedilmiş Akakçe piyasa dip fiyatını döndürür."""
        with self._lock:
            sku_entry = self.state["products"].get(canonical_sku)
            if not sku_entry:
                return None
            price = sku_entry.get("verified_market_floor_price")
            source = sku_entry.get("verified_market_floor_source")
            url = sku_entry.get("verified_akakce_url")
            if isinstance(price, (int, float)) and price > 0 and source:
                return float(price), str(source), (str(url) if url else None)
            return None
