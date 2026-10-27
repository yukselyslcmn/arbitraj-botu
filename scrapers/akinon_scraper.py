import json
import logging
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from core.parsers import BasketPromotionParser, SKUNormalizer
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger("arbitraj.scraper.akinon")


class AkinonScraper(BaseScraper):
    """
    Akinon altyapısını kullanan yetkili distribütör siteleri (Occasion, SuperStep, Barçın vb.)
    hem `?format=json&page=X` JSON uç noktasından hem de HTML / `__NEXT_DATA__` /
    `window.universal_variable` yapılarından ayrıştıran yüksek performanslı kazıyıcı.
    """

    def __init__(
        self,
        site_key: str = "occasion",
        http_client: Optional[StealthHttpClient] = None,
        enrich_missing_sizes: bool = True,
    ) -> None:
        super().__init__(site_key=site_key, http_client=http_client)
        self.enrich_missing_sizes = enrich_missing_sizes
        self._seen_canonical_skus: set[str] = set()

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
        img_set = item.get("productimage_set") or item.get("images") or item.get("media")
        if isinstance(img_set, list) and img_set:
            first = img_set[0]
            if isinstance(first, dict):
                return str(first.get("image") or first.get("url") or first.get("src") or "")
            if isinstance(first, str):
                return first

        for key in ("image", "image_url", "thumbnail", "picture", "first_image"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                return str(val.get("url") or val.get("image") or "")
        return ""

    @classmethod
    def _extract_size_from_product_attrs(cls, prod_dict: dict[str, Any]) -> Optional[str]:
        """Akinon ürün sözlüğünün attributes ve attributes_kwargs alanlarından TR/EU bedenini çeker."""
        attrs = prod_dict.get("attributes") or {}
        attrs_kw = prod_dict.get("attributes_kwargs") or {}

        for key in (
            "filterable_size",
            "integration_trBeden",
            "integration_size",
            "size",
            "beden",
            "filter_size",
            "erp_size",
            "numara",
            "integration_beden",
        ):
            val = attrs.get(key) if isinstance(attrs, dict) else None
            if isinstance(val, dict):
                val = val.get("label") or val.get("value")
            if not val and isinstance(attrs_kw, dict):
                kw_entry = attrs_kw.get(key)
                if isinstance(kw_entry, dict):
                    val = kw_entry.get("label") or kw_entry.get("value")
            if val is not None and str(val).strip():
                val_str = str(val).strip()
                # UK numarası ('4', '4,5', '9') yerine EU/TR numarası (35-48) olduğundan emin ol
                if key == "integration_beden" and not any(
                    val_str.startswith(prefix) for prefix in ("35", "36", "37", "38", "39", "40", "41", "42", "43", "44", "45", "46", "47", "48")
                ):
                    continue
                return val_str
        return None

    @classmethod
    def _extract_sizes_from_akinon_dict(
        cls,
        item: dict[str, Any],
        include_single_attr_size: bool = False,
    ) -> tuple[list[dict[str, Any]], Optional[int]]:
        """
        Akinon ürün veya detay JSON nesnesinden bedenleri ve stok miktarlarını çıkarır.
        Hem `variants -> options -> product` (Akinon standart detay yapısı) hem de düz
        varyant listelerini destekler.
        """
        raw_sizes: list[dict[str, Any]] = []
        total_stock = 0
        has_stock_data = False

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
                if not isinstance(var, dict):
                    if isinstance(var, (str, int, float)):
                        raw_sizes.append({"size": str(var), "available": True})
                    continue

                # Durum A: Akinon detay sayfası `variants` yapısı:
                # [{'attribute_key': 'integration_beden' | 'integration_size', 'attribute_name': 'Beden', 'options': [...]}]
                attr_key = str(var.get("attribute_key") or "").lower()
                attr_name = str(var.get("attribute_name") or "").lower()
                if isinstance(var.get("options"), list):
                    if any(w in attr_key or w in attr_name for w in ("beden", "size", "numara", "olcu")):
                        for opt in var["options"]:
                            if not isinstance(opt, dict):
                                continue
                            in_stock = opt.get("in_stock", True)
                            is_selectable = opt.get("is_selectable", True)
                            if in_stock is False or is_selectable is False:
                                continue

                            opt_prod = opt.get("product") if isinstance(opt.get("product"), dict) else {}
                            if opt_prod.get("in_stock") is False:
                                continue

                            stock_val = opt_prod.get("stock") if opt_prod else opt.get("stock")
                            qty_int: Optional[int] = None
                            if stock_val is not None and str(stock_val).isdigit():
                                qty_int = int(stock_val)
                                if qty_int <= 0:
                                    continue
                                has_stock_data = True
                                total_stock += qty_int

                            size_val = (
                                cls._extract_size_from_product_attrs(opt_prod)
                                if opt_prod
                                else None
                            ) or opt.get("label") or opt.get("value")

                            if size_val:
                                raw_sizes.append(
                                    {
                                        "size": str(size_val),
                                        "stock": qty_int,
                                        "available": True,
                                    }
                                )
                    continue

                # Durum B: Doğrudan varyant nesnesi
                in_stock = var.get("in_stock", var.get("is_available", var.get("available", True)))
                stock_qty = var.get("stock") or var.get("quantity") or var.get("stock_count")
                if stock_qty is not None and str(stock_qty).isdigit():
                    qty_int = int(stock_qty)
                    if qty_int <= 0:
                        continue
                    has_stock_data = True
                    total_stock += qty_int
                elif in_stock is False:
                    continue
                else:
                    qty_int = None

                size_val = (
                    cls._extract_size_from_product_attrs(var)
                    or var.get("size")
                    or var.get("value")
                    or var.get("name")
                    or var.get("label")
                    or var.get("option")
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

        # Tekil ürün nesnesinin kendi attribute'unda beden varsa
        if include_single_attr_size and not raw_sizes and item.get("in_stock", True) is not False:
            single_size = cls._extract_size_from_product_attrs(item)
            if single_size:
                stock_val = item.get("stock")
                qty = int(stock_val) if stock_val is not None and str(stock_val).isdigit() else None
                if qty is None or qty > 0:
                    raw_sizes.append({"size": single_size, "stock": qty, "available": True})

        return raw_sizes, (total_stock if has_stock_data and total_stock > 0 else None)

    @staticmethod
    def _extract_campaigns_from_akinon_dict(item: dict[str, Any]) -> tuple[Any, list[str]]:
        """
        Akinon JSON nesnesinden sepetteki net fiyatı (`listing_kwargs.discounted_total_price` vb.)
        ve kampanya metinlerini çıkarır.
        """
        api_basket_price = (
            item.get("basket_price")
            or item.get("campaign_price")
            or item.get("discounted_price")
            or item.get("extra_discounted_price")
            or item.get("cart_price")
        )
        campaign_texts: list[str] = []

        for field_name in (
            "basket_offers",
            "offers",
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
                            entry.get("label")
                            or entry.get("name")
                            or entry.get("title")
                            or entry.get("description")
                            or entry.get("badge_text")
                        )
                        if txt:
                            campaign_texts.append(str(txt))
                        listing_kw = entry.get("listing_kwargs")
                        discounted = (
                            (listing_kw.get("discounted_total_price") if isinstance(listing_kw, dict) else None)
                            or entry.get("discounted_total_price")
                            or entry.get("discounted_price")
                            or entry.get("basket_price")
                        )
                        if discounted is not None:
                            try:
                                d_val = float(discounted)
                                if d_val > 0 and (api_basket_price is None or d_val < float(api_basket_price)):
                                    api_basket_price = d_val
                            except (ValueError, TypeError):
                                if api_basket_price is None:
                                    api_basket_price = discounted
                    elif isinstance(entry, str):
                        campaign_texts.append(entry)
            elif isinstance(val, dict):
                txt = val.get("label") or val.get("name") or val.get("title")
                if txt:
                    campaign_texts.append(str(txt))
            elif isinstance(val, str) and val.strip():
                campaign_texts.append(val.strip())

        attrs = item.get("attributes") or {}
        if isinstance(attrs, dict):
            for k, v in attrs.items():
                if any(w in k.lower() for w in ("campaign", "kampanya", "badge", "sepet", "promo", "indirim")):
                    if isinstance(v, dict):
                        v = v.get("value") or v.get("label")
                    if v and str(v) not in ("campaign-slug;campaign-test", "True", "False"):
                        campaign_texts.append(str(v))

        return api_basket_price, campaign_texts

    def fetch_product_detail_enrichment(
        self,
        product_url: str,
    ) -> tuple[list[dict[str, Any]], Any, list[str], Optional[int]]:
        """
        Aday ürünler için `{product_url}?format=json` uç noktasını sorgulayarak
        tam/buçuklu/kesirli stok numaralarını (`variants -> options`) ve sepet kampanyasını çeker.
        """
        json_url = self._build_paginated_url(product_url, page=1, as_json=True)
        data = self.client.get_json(json_url)

        raw_sizes: list[dict[str, Any]] = []
        api_basket_price: Any = None
        campaign_texts: list[str] = []
        total_stock: Optional[int] = None

        if isinstance(data, dict):
            # 1. Üst seviye `variants` ve `offers` alanlarını kontrol et
            raw_sizes, total_stock = self._extract_sizes_from_akinon_dict(data, include_single_attr_size=False)
            api_basket_price, campaign_texts = self._extract_campaigns_from_akinon_dict(data)

            # 2. İçteki `product` nesnesini kontrol et
            prod_obj = data.get("product")
            if isinstance(prod_obj, dict):
                p_sizes, p_stock = self._extract_sizes_from_akinon_dict(
                    prod_obj,
                    include_single_attr_size=(len(raw_sizes) == 0),
                )
                if p_sizes:
                    raw_sizes.extend(p_sizes)
                if total_stock is None and p_stock is not None:
                    total_stock = p_stock

                p_basket, p_camps = self._extract_campaigns_from_akinon_dict(prod_obj)
                if p_basket and not api_basket_price:
                    api_basket_price = p_basket
                campaign_texts.extend(p_camps)

            return raw_sizes, api_basket_price, campaign_texts, total_stock

        # JSON uç noktası yanıt vermediyse yedek olarak HTML sayfasını kontrol et
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

        return sizes, api_basket_price, campaigns

    @staticmethod
    def _extract_brand_from_akinon_item(item: dict[str, Any]) -> str:
        attrs = item.get("attributes") or {}
        attrs_kw = item.get("attributes_kwargs") or {}

        for key in ("integration_alt_marka", "integration_brand", "brand", "marka", "integration_marka"):
            if isinstance(attrs_kw, dict) and isinstance(attrs_kw.get(key), dict):
                lbl = attrs_kw[key].get("label") or attrs_kw[key].get("value")
                if lbl and not str(lbl).isdigit():
                    return str(lbl).strip()
            if isinstance(attrs, dict) and attrs.get(key):
                val = attrs[key]
                if isinstance(val, dict):
                    val = val.get("label") or val.get("value")
                if val and not str(val).isdigit():
                    return str(val).strip()

        b_obj = item.get("brand")
        if isinstance(b_obj, dict):
            return str(b_obj.get("name") or "")
        return str(b_obj or "")

    @staticmethod
    def _extract_sku_from_akinon_item(item: dict[str, Any]) -> str:
        attrs = item.get("attributes") or {}
        if isinstance(attrs, dict):
            for k in ("integration_article_code", "integration_model_renk", "model_code", "base_code"):
                val = attrs.get(k)
                if val and str(val).strip():
                    return str(val).strip()
        for k in ("base_code", "model_code", "group_code", "sku", "pk"):
            val = item.get(k)
            if val and str(val).strip():
                return str(val).strip()
        return ""

    def _parse_akinon_json_products(
        self,
        raw_products: list[dict[str, Any]],
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        """
        Akinon JSON ürün listesini ayrıştırır.
        Performans Optimizasyonu:
        - Tam tarama modunda (`limit is None`) ürün detay sayfasına (`?format=json`) YALNIZCA
          marka, kara liste, maksimum sepet fiyatı (`MAX_BASKET_PRICE`) ve minimum indirim oranı
          (`MIN_TOTAL_DISCOUNT_RATE`) ön filtrelerini geçen fırsat adayları için istek atar.
        - Böylece indirim oranı düşük binlerce ürün için gereksiz detay isteği atılmaz ve tarama
          dakikalar yerine saniyeler içinde tamamlanır.
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

            brand = self._extract_brand_from_akinon_item(item)
            raw_sku = self._extract_sku_from_akinon_item(item)
            url = str(item.get("absolute_url") or item.get("url") or item.get("slug") or "")
            canonical_sku, _ = SKUNormalizer.normalize(raw_sku=raw_sku, title=title, url=url, brand=brand)
            if not canonical_sku:
                continue

            raw_sizes, stock_total = self._extract_sizes_from_akinon_dict(item, include_single_attr_size=False)
            api_basket_price, campaign_texts = self._extract_campaigns_from_akinon_dict(item)
            image_url = self._extract_image_from_akinon_dict(item)

            retail_price = (
                item.get("retail_price")
                or (item.get("price_extra_field", {}) or {}).get("previous_price")
                or item.get("old_price")
                or item.get("list_price")
                or item.get("price")
            )
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
                    "fallback_single_size": self._extract_size_from_product_attrs(item),
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
        apply_strict_price_prefilter = limit is None
        enriched_candidate_count = 0

        for canonical_sku, grp in grouped_raw.items():
            if canonical_sku in self._seen_canonical_skus:
                continue
            matched_brand = ArbitrageEngine.is_brand_allowed(grp["brand"], grp["title"])
            if not matched_brand:
                continue
            if ArbitrageEngine.has_excluded_keyword(grp["title"], grp["url"]):
                continue

            # Ön Fiyat & İndirim Kontrolü (Detay isteği atmadan önce!)
            prelim_price = BasketPromotionParser.calculate(
                retail_price=grp["retail_price"],
                sale_price=grp["sale_price"],
                api_basket_price=grp["api_basket_price"],
                campaign_texts=grp["campaign_texts"],
            )
            if prelim_price.net_basket_price <= 0:
                continue

            min_disc_threshold = (
                25.0 if (self.site_key == "barcin" and enriched_candidate_count < 5) else config.MIN_TOTAL_DISCOUNT_RATE
            )
            is_enrichment_candidate = (not apply_strict_price_prefilter) or (
                prelim_price.net_basket_price <= config.MAX_BASKET_PRICE
                and prelim_price.total_discount_rate >= min_disc_threshold
            )

            self._seen_canonical_skus.add(canonical_sku)

            # Yalnızca filtreyi geçen aday ürünler için detay JSON'undan beden kırılımını çek
            if is_enrichment_candidate and not grp["raw_sizes"] and self.enrich_missing_sizes and grp["url"]:
                full_prod_url = self.build_full_url(grp["url"])
                det_sizes, det_basket, det_campaigns, det_stock = self.fetch_product_detail_enrichment(full_prod_url)
                grp["raw_sizes"].extend(det_sizes)
                if det_basket and not grp["api_basket_price"]:
                    grp["api_basket_price"] = det_basket
                grp["campaign_texts"].extend(det_campaigns)
                if det_stock is not None:
                    grp["total_stock"] = det_stock

            if not grp["raw_sizes"] and grp.get("fallback_single_size"):
                grp["raw_sizes"].append({"size": grp["fallback_single_size"], "available": True})

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
            if prod and (prod.sizes or (limit is None and not is_enrichment_candidate)):
                if prod.sizes:
                    enriched_candidate_count += 1
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results

    def _parse_html_fallback(
        self,
        html_text: str,
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        next_data = self.extract_next_data(html_text)
        if isinstance(next_data, dict):
            raw_list = self._find_product_list_in_json(next_data)
            if raw_list:
                parsed = self._parse_akinon_json_products(raw_list, default_gender=default_gender, limit=limit)
                if parsed:
                    return parsed

        uv = self.extract_universal_variable(html_text)
        if isinstance(uv, dict):
            listing = uv.get("listing", {})
            items = listing.get("items") if isinstance(listing, dict) else None
            if isinstance(items, list) and items:
                parsed = self._parse_akinon_json_products(items, default_gender=default_gender, limit=limit)
                if parsed:
                    return parsed

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select(".js-product-wrapper, .product-item, .product-card, [data-product], [data-sku]")
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
            img_url = (img_el.get("data-src") or img_el.get("src") or "") if img_el else ""

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
                            has_next = True
                            if isinstance(paginator, dict):
                                if "has_next" in paginator:
                                    has_next = bool(paginator.get("has_next"))
                                elif "num_pages" in paginator and "current_page" in paginator:
                                    has_next = int(paginator["current_page"]) < int(paginator["num_pages"])
                            collected.extend(page_products)
                            if not has_next or (limit and len(collected) >= limit):
                                break
                            continue
                except Exception as exc:
                    logger.debug(
                        "[%s] Yanıt JSON formatında değil, HTML ayrıştırıcıya geçiliyor: %s",
                        self.site_name,
                        exc,
                    )

                if resp.text:
                    page_products = self._parse_html_fallback(
                        resp.text,
                        default_gender=gender,
                        limit=remaining,
                    )

            if not page_products:
                break

            collected.extend(page_products)
            if limit and len(collected) >= limit:
                break

        return collected
