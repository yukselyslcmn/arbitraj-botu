import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger("arbitraj.scraper.akinon")


class AkinonScraper(BaseScraper):
    """
    Akinon altyapısını kullanan yetkili distribütör siteleri (Occasion, SuperStep vb.)
    hem `?format=json&page=X` JSON uç noktasından hem de HTML / `__NEXT_DATA__` /
    `window.universal_variable` yapılarından ayrıştıran kazıyıcı.
    """

    def __init__(
        self,
        site_key: str = "occasion",
        http_client: Optional[StealthHttpClient] = None,
        enrich_missing_sizes: bool = True,
    ) -> None:
        super().__init__(site_key=site_key, http_client=http_client)
        self.enrich_missing_sizes = enrich_missing_sizes

    @staticmethod
    def _build_paginated_url(base_category_url: str, page: int, as_json: bool = True) -> str:
        parsed = urlparse(base_category_url)
        query_dict = dict(parse_qsl(parsed.query))
        if as_json:
            query_dict["format"] = "json"
        if page > 1:
            query_dict["page"] = str(page)
        new_query = urlencode(query_dict)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    @staticmethod
    def _extract_image_from_akinon_dict(item: dict[str, Any]) -> str:
        # 1. productimage_set listesi
        img_set = item.get("productimage_set") or item.get("images") or item.get("media")
        if isinstance(img_set, list) and img_set:
            first = img_set[0]
            if isinstance(first, dict):
                return str(first.get("image") or first.get("url") or first.get("src") or "")
            if isinstance(first, str):
                return first

        # 2. Doğrudan image alanları
        for key in ("image", "image_url", "thumbnail", "picture", "first_image"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                return str(val.get("url") or val.get("image") or "")
        return ""

    @staticmethod
    def _extract_sizes_from_akinon_dict(item: dict[str, Any]) -> tuple[list[dict[str, Any]], Optional[int]]:
        """Akinon ürün JSON nesnesinden bedenleri ve stok miktarlarını çıkarır."""
        raw_sizes: list[dict[str, Any]] = []
        total_stock = 0
        has_stock_data = False

        # 1. variants / children / simple_products / stock_list
        variants = (
            item.get("variants")
            or item.get("variant_set")
            or item.get("children")
            or item.get("simple_products")
            or item.get("options")
            or item.get("sizes")
        )
        if isinstance(variants, list):
            for var in variants:
                if isinstance(var, dict):
                    in_stock = var.get("in_stock", var.get("is_available", var.get("available", True)))
                    stock_qty = var.get("stock") or var.get("quantity") or var.get("stock_count")
                    if stock_qty is not None and str(stock_qty).isdigit():
                        qty_int = int(stock_qty)
                        has_stock_data = True
                        if qty_int <= 0:
                            continue
                        total_stock += qty_int
                    elif in_stock is False:
                        continue
                    else:
                        qty_int = None

                    # Beden bilgisini attributes veya doğrudan alanlardan çek
                    attrs = var.get("attributes") or var.get("attribute_map") or {}
                    size_val = (
                        var.get("size")
                        or var.get("value")
                        or var.get("name")
                        or var.get("label")
                        or var.get("option")
                        or (attrs.get("size") if isinstance(attrs, dict) else None)
                        or (attrs.get("beden") if isinstance(attrs, dict) else None)
                        or (attrs.get("filter_size") if isinstance(attrs, dict) else None)
                        or (attrs.get("integration_size") if isinstance(attrs, dict) else None)
                    )
                    if isinstance(size_val, dict):
                        size_val = size_val.get("value") or size_val.get("label") or size_val.get("name")

                    if size_val:
                        raw_sizes.append(
                            {
                                "size": str(size_val),
                                "stock": qty_int,
                                "available": True,
                            }
                        )
                elif isinstance(var, (str, int, float)):
                    raw_sizes.append({"size": str(var), "available": True})

        # 2. Tekil ürün nesnesinin kendi attribute'unda beden varsa (Akinon'da bazen her beden ayrı ürün/varyant olarak gelir)
        attrs = item.get("attributes") or item.get(" attributes_kwargs") or {}
        if isinstance(attrs, dict):
            single_size = (
                attrs.get("size")
                or attrs.get("beden")
                or attrs.get("integration_size")
                or attrs.get("filter_size")
                or attrs.get("erp_size")
                or attrs.get("numara")
            )
            if isinstance(single_size, dict):
                single_size = single_size.get("value") or single_size.get("label")
            if single_size and item.get("in_stock", True) is not False:
                stock_val = item.get("stock")
                qty = int(stock_val) if stock_val is not None and str(stock_val).isdigit() else None
                raw_sizes.append({"size": str(single_size), "stock": qty, "available": True})

        return raw_sizes, (total_stock if has_stock_data and total_stock > 0 else None)

    @staticmethod
    def _extract_campaigns_from_akinon_dict(item: dict[str, Any]) -> tuple[Any, list[str]]:
        """Akinon JSON nesnesinden sepetteki fiyatı ve kampanya metinlerini çıkarır."""
        api_basket_price = (
            item.get("basket_price")
            or item.get("campaign_price")
            or item.get("discounted_price")
            or item.get("extra_discounted_price")
            or item.get("cart_price")
        )
        campaign_texts: list[str] = []

        # Akinon basket_offers / promotions / campaigns / badges alanları
        for field_name in (
            "basket_offers",
            "promotions",
            "campaigns",
            "badges",
            "discount_badges",
            "basket_campaigns",
            "active_promotions",
        ):
            val = item.get(field_name)
            if isinstance(val, list):
                for entry in val:
                    if isinstance(entry, dict):
                        txt = (
                            entry.get("name")
                            or entry.get("title")
                            or entry.get("label")
                            or entry.get("description")
                            or entry.get("badge_text")
                        )
                        if txt:
                            campaign_texts.append(str(txt))
                        discounted = (
                            entry.get("discounted_price")
                            or entry.get("basket_price")
                            or entry.get("price")
                        )
                        if discounted and api_basket_price is None:
                            api_basket_price = discounted
                    elif isinstance(entry, str):
                        campaign_texts.append(entry)
            elif isinstance(val, dict):
                txt = val.get("name") or val.get("title") or val.get("label")
                if txt:
                    campaign_texts.append(str(txt))
            elif isinstance(val, str) and val.strip():
                campaign_texts.append(val.strip())

        # attributes içindeki kampanya etiketleri
        attrs = item.get("attributes") or item.get("attributes_kwargs") or {}
        if isinstance(attrs, dict):
            for k, v in attrs.items():
                if any(w in k.lower() for w in ("campaign", "kampanya", "badge", "sepet", "promo", "indirim")):
                    if isinstance(v, dict):
                        v = v.get("value") or v.get("label")
                    if v:
                        campaign_texts.append(str(v))

        return api_basket_price, campaign_texts

    def fetch_product_detail_enrichment(
        self,
        product_url: str,
    ) -> tuple[list[dict[str, Any]], Any, list[str], Optional[int]]:
        """
        Kategori listesinde beden kırılımı gelmeyen aday ürünler için
        `{product_url}?format=json` veya ürün detay HTML'ini sorgulayarak
        tam/buçuklu/kesirli stok numaralarını ve sepet kampanyasını çeker.
        """
        json_url = self._build_paginated_url(product_url, page=1, as_json=True)
        data = self.client.get_json(json_url)

        raw_sizes: list[dict[str, Any]] = []
        api_basket_price: Any = None
        campaign_texts: list[str] = []
        total_stock: Optional[int] = None

        if isinstance(data, dict):
            prod_obj = data.get("product") if isinstance(data.get("product"), dict) else data
            raw_sizes, total_stock = self._extract_sizes_from_akinon_dict(prod_obj)
            api_basket_price, campaign_texts = self._extract_campaigns_from_akinon_dict(prod_obj)

            # Akinon detay JSON'unda `variants` veya `selectable_attributes` / `options` kontrolü
            for var_key in ("variants", "variant_list", "product_variants", "RelatedProducts"):
                v_list = data.get(var_key) or prod_obj.get(var_key)
                if isinstance(v_list, list):
                    for v_item in v_list:
                        if isinstance(v_item, dict):
                            sub_sizes, _ = self._extract_sizes_from_akinon_dict(v_item)
                            raw_sizes.extend(sub_sizes)

            if raw_sizes:
                return raw_sizes, api_basket_price, campaign_texts, total_stock

        # JSON'dan beden çıkmadıysa ürün detay HTML'ini kontrol et
        resp = self.client.get(product_url)
        if resp and resp.ok and resp.text:
            html_sizes, html_basket, html_campaigns = self._parse_detail_html(resp.text)
            raw_sizes.extend(html_sizes)
            if html_basket and not api_basket_price:
                api_basket_price = html_basket
            campaign_texts.extend(html_campaigns)

        return raw_sizes, api_basket_price, campaign_texts, total_stock

    def _parse_detail_html(self, html_text: str) -> tuple[list[dict[str, Any]], Any, list[str]]:
        soup = BeautifulSoup(html_text, "lxml")
        sizes: list[dict[str, Any]] = []
        api_basket_price: Any = None
        campaigns: list[str] = []

        # 1. Beden seçici buton/input/option elemanları
        size_selectors = [
            "div.js-size-options a:not(.disabled):not(.out-of-stock)",
            "div.size-options button:not([disabled]):not(.disabled)",
            "ul.size-list li:not(.disabled):not(.out-of-stock)",
            "select[name*='size'] option:not([disabled])",
            "[data-attr='size'] [data-value]:not(.disabled)",
            "input[name*='size']:not([disabled])",
            "label.size-item:not(.disabled):not(.out-of-stock)",
            ".product-sizes__item:not(.disabled):not(.is-disabled)",
            ".variant-list__item:not(.disabled)",
        ]
        for sel in size_selectors:
            for el in soup.select(sel):
                classes = " ".join(el.get("class", [])).lower()
                if any(x in classes for x in ("disabled", "out-of-stock", "passive", "sold-out", "none")):
                    continue
                val = (
                    el.get("data-value")
                    or el.get("data-size")
                    or el.get("data-label")
                    or el.get_text(strip=True)
                )
                if val:
                    sizes.append({"size": str(val).strip(), "available": True})

        # 2. Kampanya rozetleri ve sepette fiyat alanları
        promo_selectors = [
            ".basket-discount",
            ".campaign-badge",
            ".product-badge",
            ".promotion-text",
            ".js-basket-price",
            "[class*='campaign']",
            "[class*='basket-price']",
        ]
        for sel in promo_selectors:
            for el in soup.select(sel):
                txt = el.get_text(" ", strip=True)
                if txt and len(txt) < 120:
                    campaigns.append(txt)

        # 3. Gömülü JSON içinde variants / stock kontrolü
        uv = self.extract_universal_variable(html_text)
        if isinstance(uv, dict) and isinstance(uv.get("product"), dict):
            uv_prod = uv["product"]
            uv_sizes, _ = self._extract_sizes_from_akinon_dict(uv_prod)
            sizes.extend(uv_sizes)

        next_data = self.extract_next_data(html_text)
        if isinstance(next_data, dict):
            nd_sizes = self._find_sizes_in_nested_json(next_data)
            sizes.extend(nd_sizes)

        return sizes, api_basket_price, campaigns

    def _find_sizes_in_nested_json(self, obj: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(obj, dict):
            if "variants" in obj and isinstance(obj["variants"], list):
                s_list, _ = self._extract_sizes_from_akinon_dict(obj)
                found.extend(s_list)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    found.extend(self._find_sizes_in_nested_json(v))
        elif isinstance(obj, list):
            for item in obj[:50]:
                if isinstance(item, (dict, list)):
                    found.extend(self._find_sizes_in_nested_json(item))
        return found

    def _parse_akinon_json_products(
        self,
        raw_products: list[dict[str, Any]],
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        """
        Akinon JSON ürün listesini ayrıştırır. Aynı modelin (`base_code` veya `canonical_sku`)
        farklı beden satırlarını tek bir üründe birleştirir.
        """
        grouped_raw: dict[str, dict[str, Any]] = {}

        for item in raw_products:
            if not isinstance(item, dict):
                continue
            if item.get("in_stock") is False:
                continue

            title = str(item.get("name") or item.get("title") or "").strip()
            if not title:
                continue

            attrs = item.get("attributes") or item.get("attributes_kwargs") or {}
            brand = ""
            if isinstance(attrs, dict):
                brand_field = attrs.get("brand") or attrs.get("marka") or attrs.get("integration_brand")
                if isinstance(brand_field, dict):
                    brand = str(brand_field.get("value") or brand_field.get("label") or "")
                elif brand_field:
                    brand = str(brand_field)
            if not brand:
                b_obj = item.get("brand")
                brand = str(b_obj.get("name") if isinstance(b_obj, dict) else (b_obj or ""))

            raw_sku = str(
                item.get("base_code")
                or item.get("model_code")
                or item.get("group_code")
                or (attrs.get("base_code") if isinstance(attrs, dict) else None)
                or (attrs.get("model_code") if isinstance(attrs, dict) else None)
                or item.get("sku")
                or item.get("pk")
                or ""
            )
            url = str(item.get("absolute_url") or item.get("url") or item.get("slug") or "")
            canonical_sku, _ = (
                self._quick_canonical_key(raw_sku, title, url, brand)
            )
            if not canonical_sku:
                continue

            raw_sizes, stock_total = self._extract_sizes_from_akinon_dict(item)
            api_basket_price, campaign_texts = self._extract_campaigns_from_akinon_dict(item)
            image_url = self._extract_image_from_akinon_dict(item)

            retail_price = item.get("retail_price") or item.get("old_price") or item.get("list_price") or item.get("price")
            sale_price = item.get("price") or item.get("sale_price") or retail_price

            if canonical_sku not in grouped_raw:
                grouped_raw[canonical_sku] = {
                    "raw_sku": raw_sku,
                    "brand": brand,
                    "title": title,
                    "url": url,
                    "image_url": image_url,
                    "retail_price": retail_price,
                    "sale_price": sale_price,
                    "api_basket_price": api_basket_price,
                    "campaign_texts": list(campaign_texts),
                    "raw_sizes": list(raw_sizes),
                    "total_stock": stock_total,
                }
            else:
                grp = grouped_raw[canonical_sku]
                grp["raw_sizes"].extend(raw_sizes)
                grp["campaign_texts"].extend(campaign_texts)
                if api_basket_price and not grp["api_basket_price"]:
                    grp["api_basket_price"] = api_basket_price
                if stock_total is not None:
                    grp["total_stock"] = (grp["total_stock"] or 0) + stock_total

        results: list[ScrapedProduct] = []
        for grp in grouped_raw.values():
            # Marka ve kara liste ön kontrolü (gereksiz detay isteklerini önlemek için)
            matched_brand = ArbitrageEngine.is_brand_allowed(grp["brand"], grp["title"])
            if not matched_brand:
                continue
            if ArbitrageEngine.has_excluded_keyword(grp["title"], grp["url"]):
                continue

            # Eğer kategori JSON'unda beden bilgisi gelmediyse ürün detayından zenginleştir
            if not grp["raw_sizes"] and self.enrich_missing_sizes and grp["url"]:
                full_prod_url = self.build_full_url(grp["url"])
                det_sizes, det_basket, det_campaigns, det_stock = self.fetch_product_detail_enrichment(full_prod_url)
                grp["raw_sizes"].extend(det_sizes)
                if det_basket and not grp["api_basket_price"]:
                    grp["api_basket_price"] = det_basket
                grp["campaign_texts"].extend(det_campaigns)
                if det_stock is not None:
                    grp["total_stock"] = det_stock

            prod = self.build_scraped_product(
                raw_sku=grp["raw_sku"],
                brand=matched_brand,
                title=grp["title"],
                gender=default_gender,
                url=grp["url"],
                image_url=grp["image_url"],
                retail_price=grp["retail_price"],
                sale_price=grp["sale_price"],
                api_basket_price=grp["api_basket_price"],
                campaign_texts=grp["campaign_texts"],
                raw_sizes=grp["raw_sizes"],
                total_stock=grp["total_stock"],
            )
            if prod and prod.sizes:
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results

    @staticmethod
    def _quick_canonical_key(raw_sku: str, title: str, url: str, brand: str) -> tuple[str, str]:
        from core.parsers import SKUNormalizer

        return SKUNormalizer.normalize(raw_sku=raw_sku, title=title, url=url, brand=brand)

    def _parse_html_fallback(
        self,
        html_text: str,
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        """
        `?format=json` uç noktası yerine HTML döndüğünde `__NEXT_DATA__`,
        `window.universal_variable` veya HTML ürün kartlarını ayrıştırır.
        """
        # 1. __NEXT_DATA__ kontrolü
        next_data = self.extract_next_data(html_text)
        if isinstance(next_data, dict):
            raw_list = self._find_product_list_in_json(next_data)
            if raw_list:
                parsed = self._parse_akinon_json_products(raw_list, default_gender=default_gender, limit=limit)
                if parsed:
                    return parsed

        # 2. window.universal_variable kontrolü
        uv = self.extract_universal_variable(html_text)
        if isinstance(uv, dict):
            listing = uv.get("listing", {})
            items = listing.get("items") if isinstance(listing, dict) else None
            if isinstance(items, list) and items:
                parsed = self._parse_akinon_json_products(items, default_gender=default_gender, limit=limit)
                if parsed:
                    return parsed

        # 3. HTML ürün kartları (.js-product-wrapper, [data-product], .product-item)
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select(
            ".js-product-wrapper, .product-item, .product-card, [data-product], [data-sku]"
        )
        raw_items: list[dict[str, Any]] = []

        for card in cards:
            data_prod = card.get("data-product") or card.get("data-gtm-product")
            if data_prod:
                try:
                    obj = json.loads(data_prod)
                    if isinstance(obj, dict):
                        raw_items.append(obj)
                        continue
                except Exception as exc:
                    logger.debug("data-product JSON ayrıştırılamadı: %s", exc)

            link_el = card.select_one("a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")
            title_el = card.select_one(".product-name, .product-title, .name, h3, h2") or link_el
            title = title_el.get_text(" ", strip=True) or link_el.get("title", "")
            brand_el = card.select_one(".product-brand, .brand")
            brand = brand_el.get_text(" ", strip=True) if brand_el else (card.get("data-brand") or "")
            sku = card.get("data-sku") or card.get("data-base-code") or card.get("data-id") or ""

            old_price_el = card.select_one(".old-price, .retail-price, del, s, [class*='old-price']")
            sale_price_el = card.select_one(".sale-price, .price, .current-price, [class*='sale-price']")
            basket_el = card.select_one(".basket-price, .campaign-price, [class*='basket']")

            retail_txt = old_price_el.get_text(" ", strip=True) if old_price_el else ""
            sale_txt = sale_price_el.get_text(" ", strip=True) if sale_price_el else ""
            basket_txt = basket_el.get_text(" ", strip=True) if basket_el else ""

            img_el = card.select_one("img")
            img_url = ""
            if img_el:
                img_url = img_el.get("data-src") or img_el.get("src") or ""

            if title and (sale_txt or retail_txt):
                raw_items.append(
                    {
                        "sku": sku,
                        "brand": brand,
                        "name": title,
                        "url": href,
                        "image": img_url,
                        "retail_price": retail_txt or sale_txt,
                        "price": sale_txt or retail_txt,
                        "campaigns": [basket_txt] if basket_txt else [],
                    }
                )

        if raw_items:
            return self._parse_akinon_json_products(raw_items, default_gender=default_gender, limit=limit)
        return []

    def _find_product_list_in_json(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            for key in ("products", "items", "results", "productList"):
                val = data.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    if any(k in val[0] for k in ("price", "sku", "name", "title", "retail_price")):
                        return val
            for v in data.values():
                if isinstance(v, (dict, list)):
                    res = self._find_product_list_in_json(v)
                    if res:
                        return res
        elif isinstance(data, list):
            for entry in data[:20]:
                res = self._find_product_list_in_json(entry)
                if res:
                    return res
        return []

    def scrape_category(
        self,
        category_url: str,
        gender: str = "Erkek",
        max_pages: int = config.MAX_PAGES_PER_CATEGORY,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        collected: list[ScrapedProduct] = []

        for page in range(1, max_pages + 1):
            remaining = (limit - len(collected)) if limit else None
            if remaining is not None and remaining <= 0:
                break

            json_url = self._build_paginated_url(category_url, page=page, as_json=True)
            logger.info("[%s] Sayfa %d taranıyor: %s", self.site_name, page, json_url)

            resp = self.client.get(json_url, is_json=True)
            page_products: list[ScrapedProduct] = []

            if resp and resp.ok:
                # Yanıt JSON mu kontrol et
                try:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        raw_prods = (
                            payload.get("products")
                            or payload.get("results")
                            or payload.get("items")
                            or self._find_product_list_in_json(payload)
                        )
                        if isinstance(raw_prods, list) and raw_prods:
                            page_products = self._parse_akinon_json_products(
                                raw_prods,
                                default_gender=gender,
                                limit=remaining,
                            )
                            paginator = payload.get("paginator") or payload.get("pagination") or {}
                            has_next = paginator.get("has_next", True) if isinstance(paginator, dict) else True
                            collected.extend(page_products)
                            if not has_next or (limit and len(collected) >= limit):
                                break
                            continue
                except Exception as exc:
                    logger.debug("[%s] Yanıt JSON formatında değil, HTML ayrıştırıcıya geçiliyor: %s", self.site_name, exc)

                # Yanıt HTML olarak döndüyse HTML ayrıştırıcıyı çalıştır
                if resp.text:
                    page_products = self._parse_html_fallback(
                        resp.text,
                        default_gender=gender,
                        limit=remaining,
                    )

            if not page_products:
                # Doğrudan HTML URL'sini dene
                html_url = self._build_paginated_url(category_url, page=page, as_json=False)
                html_resp = self.client.get(html_url, is_json=False)
                if html_resp and html_resp.ok and html_resp.text:
                    page_products = self._parse_html_fallback(
                        html_resp.text,
                        default_gender=gender,
                        limit=remaining,
                    )

            if not page_products:
                break

            collected.extend(page_products)
            if limit and len(collected) >= limit:
                break

        return collected
