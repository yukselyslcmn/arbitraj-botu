import logging
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from core.parsers import BasketPromotionParser
from scrapers.akinon_scraper import AkinonScraper

logger = logging.getLogger("arbitraj.scraper.barcin_koray")


class BarcinKorayScraper(AkinonScraper):
    """
    Nike, Adidas, Puma resmi yetkili satıcıları olan `barcin.com` (Akinon JSON) ve
    `korayspor.com` (Next.js `__NEXT_DATA__` -> `response.products` & `stocksByBarcode`)
    indirim/outlet sayfalarını tarayan kazıyıcı.
    """

    KORAYSPOR_IMAGE_CDN = "https://p-korayspor.sm.mncdn.com/mnresize/640/-/"

    def __init__(
        self,
        site_key: str = "barcin",
        http_client: Optional[StealthHttpClient] = None,
        enrich_missing_sizes: bool = True,
    ) -> None:
        if site_key not in ("barcin", "korayspor"):
            raise ValueError("BarcinKorayScraper yalnızca 'barcin' veya 'korayspor' anahtarlarını kabul eder.")
        super().__init__(
            site_key=site_key,
            http_client=http_client,
            enrich_missing_sizes=enrich_missing_sizes,
        )

    def _fetch_korayspor_detail_sizes(self, product_url: str) -> tuple[list[dict[str, Any]], Optional[int]]:
        """
        Korayspor ürün detay sayfasındaki `__NEXT_DATA__` içinden `barcodes` ve
        `stocksByBarcode` eşleşmesini çözerek stoktaki numaraları ve stok adetlerini döndürür.
        """
        resp = self.client.get(product_url)
        if not resp or not resp.ok or not resp.text:
            return [], None

        next_data = self.extract_next_data(resp.text)
        if not isinstance(next_data, dict):
            return [], None

        prod = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("response", {})
            .get("product", {})
        )
        if not isinstance(prod, dict):
            return [], None

        barcodes = prod.get("barcodes") or []
        stocks_by_barcode = prod.get("stocksByBarcode") or {}
        if not isinstance(barcodes, list) or not isinstance(stocks_by_barcode, dict):
            return [], None

        raw_sizes: list[dict[str, Any]] = []
        total_stock = 0

        for bc in barcodes:
            if not isinstance(bc, dict):
                continue
            bc_id = str(bc.get("id") or "")
            qty = stocks_by_barcode.get(bc_id, 0)
            if not isinstance(qty, (int, float)) or qty <= 0:
                continue

            stock_types = bc.get("stockTypeValues") or []
            if isinstance(stock_types, list) and stock_types and isinstance(stock_types[0], dict):
                sz_name = stock_types[0].get("name")
                if sz_name:
                    qty_int = int(qty)
                    total_stock += qty_int
                    raw_sizes.append(
                        {
                            "size": str(sz_name),
                            "stock": qty_int,
                            "available": True,
                        }
                    )

        return raw_sizes, (total_stock if total_stock > 0 else None)

    def _parse_korayspor_next_data(
        self,
        next_data: dict[str, Any],
        default_gender: str,
        limit: Optional[int] = None,
    ) -> tuple[list[ScrapedProduct], bool]:
        """
        Korayspor'un `__NEXT_DATA__` -> `props.pageProps.data.response` nesnesini ayrıştırır.
        Dönüş: (ürün_listesi, sonraki_sayfa_var_mı)
        """
        resp_obj = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("response", {})
        )
        if not isinstance(resp_obj, dict):
            return [], False

        raw_products = resp_obj.get("products") or []
        if not isinstance(raw_products, list) or not raw_products:
            return [], False

        current_page = int(resp_obj.get("currentPage") or 1)
        total_pages = int(resp_obj.get("totalPages") or 1)
        has_next = current_page < total_pages

        results: list[ScrapedProduct] = []
        apply_strict_price_prefilter = limit is None

        for item in raw_products:
            if not isinstance(item, dict):
                continue

            # Sadece ayakkabı kategorisindeki ürünleri al (Giyim/Tişört/Aksesuar olanları ele)
            short_name = str(item.get("shortName") or "").strip()
            route_desc = str(item.get("routeDescription") or "").strip()
            name = str(item.get("name") or "").strip()
            full_title = f"{name} {short_name}".strip()

            cats = item.get("categories") or []
            cat_names = " ".join(str(c.get("name") or "") for c in cats if isinstance(c, dict)).lower()
            combined_type = f"{full_title} {route_desc} {cat_names}".lower()
            if not any(
                w in combined_type
                for w in ("ayakkab", "sneaker", "koşu", "kosu", "bot", "krampon", "tenis", "basketbol", "outdoor")
            ):
                continue

            brand_obj = item.get("brand")
            brand_raw = str(brand_obj.get("name") if isinstance(brand_obj, dict) else (brand_obj or "")).strip()
            matched_brand = ArbitrageEngine.is_brand_allowed(brand_raw, full_title)
            if not matched_brand:
                continue

            route_path = str(item.get("routePath") or "").strip().lstrip("/")
            if not route_path:
                continue
            if ArbitrageEngine.has_excluded_keyword(full_title, route_path):
                continue

            # Stok kontrolü (stocksByBarcode içindeki toplam stok > 0 olmalı)
            stocks_map = item.get("stocksByBarcode") or {}
            if isinstance(stocks_map, dict) and stocks_map:
                total_avail = sum(int(v) for v in stocks_map.values() if isinstance(v, (int, float)) and v > 0)
                if total_avail <= 0:
                    continue

            retail_price = item.get("basePrice") or item.get("salesPrice") or item.get("discountPrice")
            sale_price = item.get("discountPrice") or item.get("salesPrice") or retail_price

            campaign_texts: list[str] = []
            types_list = item.get("types") or []
            if isinstance(types_list, list):
                for t_entry in types_list:
                    if isinstance(t_entry, dict):
                        t_name = str(t_entry.get("name") or "").strip()
                        if t_name and any(w in t_name.lower() for w in ("indirim", "sepet", "%", "tl")):
                            campaign_texts.append(t_name)

            prelim_price = BasketPromotionParser.calculate(
                retail_price=retail_price,
                sale_price=sale_price,
                campaign_texts=campaign_texts,
            )
            if prelim_price.net_basket_price <= 0:
                continue

            enriched_count = sum(1 for r in results if r.sizes)
            min_disc_threshold = 25.0 if enriched_count < 5 else config.MIN_TOTAL_DISCOUNT_RATE
            is_enrichment_candidate = (not apply_strict_price_prefilter) or (
                prelim_price.net_basket_price <= config.MAX_BASKET_PRICE
                and prelim_price.total_discount_rate >= min_disc_threshold
            )

            raw_sku = str(item.get("sku") or item.get("code") or item.get("id") or "").strip()
            docs = item.get("documents") or []
            image_url = ""
            if isinstance(docs, list) and docs and isinstance(docs[0], dict):
                fp = str(docs[0].get("filePath") or "").lstrip("/")
                if fp:
                    image_url = f"{self.KORAYSPOR_IMAGE_CDN}{fp}"

            full_prod_url = self.build_full_url(route_path)
            raw_sizes: list[dict[str, Any]] = []
            total_stock: Optional[int] = None

            if is_enrichment_candidate and self.enrich_missing_sizes:
                raw_sizes, total_stock = self._fetch_korayspor_detail_sizes(full_prod_url)

            prod = self.build_scraped_product(
                raw_sku=raw_sku,
                brand=matched_brand,
                title=full_title,
                gender=default_gender,
                url=full_prod_url,
                image_url=image_url,
                retail_price=retail_price,
                sale_price=sale_price,
                campaign_texts=campaign_texts,
                raw_sizes=raw_sizes,
                total_stock=total_stock,
            )
            if prod and (prod.sizes or (limit is None and not is_enrichment_candidate)):
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results, has_next

    def scrape_category(
        self,
        category_url: str,
        gender: str = "Erkek",
        max_pages: int = config.MAX_PAGES_PER_CATEGORY,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        # Barçın doğrudan Akinon `?format=json` yapısını kullanır
        if self.site_key == "barcin":
            return super().scrape_category(
                category_url=category_url,
                gender=gender,
                max_pages=max_pages,
                limit=limit,
            )

        # Korayspor Next.js HTML + `__NEXT_DATA__` yapısını kullanır (?page=X)
        collected: list[ScrapedProduct] = []
        for page in range(1, max_pages + 1):
            remaining = (limit - len(collected)) if limit else None
            if remaining is not None and remaining <= 0:
                break

            parsed = urlparse(category_url)
            q = dict(parse_qsl(parsed.query))
            if page > 1:
                q["page"] = str(page)
            page_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q), parsed.fragment))

            logger.info("[%s] Sayfa %d taranıyor: %s", self.site_name, page, page_url)
            resp = self.client.get(page_url, is_json=False)
            if not resp or not resp.ok or not resp.text:
                break

            next_data = self.extract_next_data(resp.text)
            if isinstance(next_data, dict):
                page_items, has_next = self._parse_korayspor_next_data(
                    next_data=next_data,
                    default_gender=gender,
                    limit=remaining,
                )
                collected.extend(page_items)
                if not has_next or (limit and len(collected) >= limit):
                    break
            else:
                break

        return collected
