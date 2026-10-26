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
    bazında saklayan, ilk çalışma spam korumasını ve %5 ekstra fiyat düşüş kuralını yöneten sınıf.
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
                    "sites": {},
                },
            )
            sites_map = sku_map.setdefault("sites", {})
            existing_site = sites_map.get(product.site_key, {})

            lowest_seen = existing_site.get("lowest_seen_price")
            if lowest_seen is None or product.net_basket_price < lowest_seen:
                lowest_seen = product.net_basket_price

            sites_map[product.site_key] = {
                "site_name": product.site_name,
                "url": product.url,
                "image_url": product.image_url,
                "retail_price": product.retail_price,
                "sale_price": product.sale_price,
                "net_basket_price": product.net_basket_price,
                "total_discount_rate": product.total_discount_rate,
                "campaign_name": product.campaign_name,
                "sizes": [s.normalized_size for s in product.sizes],
                "size_labels": [s.display_label for s in product.sizes],
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

    def get_cross_site_floor_price(self, canonical_sku: str, exclude_site_key: str) -> Optional[tuple[float, str]]:
        """
        Veritabanımızda aynı `canonical_sku` koduna sahip diğer sitelerdeki en düşük
        satış/sepet fiyatını döndürür (Akakçe yedeği ve çapraz site doğrulaması için).
        """
        with self._lock:
            sku_entry = self.state["products"].get(canonical_sku)
            if not sku_entry:
                return None

            sites_map = sku_entry.get("sites", {})
            other_prices: list[tuple[float, str]] = []
            for s_key, s_data in sites_map.items():
                if s_key == exclude_site_key:
                    continue
                price = s_data.get("net_basket_price") or s_data.get("sale_price")
                s_name = s_data.get("site_name", s_key)
                if isinstance(price, (int, float)) and price > 0:
                    other_prices.append((float(price), s_name))

            if not other_prices:
                return None

            other_prices.sort(key=lambda x: x[0])
            return other_prices[0]
