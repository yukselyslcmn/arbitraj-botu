import json
import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger("arbitraj.scraper.instreet")


class InStreetScraper(BaseScraper):
    """
    InStreet (FLO Mağazacılık Grubu - instreet.com.tr) indirim ve kategori sayfalarını tarayan,
    Next.js App Router (`self.__next_f.push`), `__NEXT_DATA__`, `application/ld+json` ve
    ürün grid HTML'ini ayrıştırarak ürün, tam/buçuklu beden, stok adedi ve sepet kampanyası
    (`ruleInfo` / `animatedBadgeList`) bilgilerini çeken kazıyıcı.
    """

    def __init__(
        self,
        site_key: str = "instreet",
        http_client: Optional[StealthHttpClient] = None,
        enrich_details: bool = False,
    ) -> None:
        super().__init__(site_key=site_key, http_client=http_client)
        self.enrich_details = enrich_details

    @staticmethod
    def _build_page_url(category_url: str, page: int) -> str:
        if page <= 1:
            return category_url
        parsed = urlparse(category_url)
        q = dict(parse_qsl(parsed.query))
        q["page"] = str(page)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q), parsed.fragment))

    @staticmethod
    def _extract_next_f_products(html_text: str) -> list[dict[str, Any]]:
        """
        InStreet Next.js App Router (`self.__next_f.push([1, "..."])`) içindeki
        gömülü `"products":[{...}]` dizisini çıkarır.
        """
        products: list[dict[str, Any]] = []
        decoder = json.JSONDecoder()

        for match in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html_text, re.DOTALL):
            raw_chunk = match.group(1)
            if "products" not in raw_chunk and "brandName" not in raw_chunk:
                continue
            try:
                unescaped = json.loads(f'"{raw_chunk}"')
            except Exception:
                continue

            search_pos = 0
            marker = '"products":['
            while True:
                idx = unescaped.find(marker, search_pos)
                if idx == -1:
                    break
                arr_start = idx + len('"products":')
                try:
                    arr, end_offset = decoder.raw_decode(unescaped[arr_start:])
                    if isinstance(arr, list):
                        for item in arr:
                            if isinstance(item, dict) and ("sku" in item or "name" in item or "brandName" in item):
                                products.append(item)
                    search_pos = arr_start + end_offset
                except Exception:
                    search_pos = arr_start + 1

        return products

    def _parse_next_f_product_items(
        self,
        raw_items: list[dict[str, Any]],
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        results: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for item in raw_items:
            if item.get("isInStock") is False:
                continue

            brand_raw = str(item.get("brandName") or item.get("brand") or "").strip()
            title = str(item.get("name") or item.get("title") or "").strip()
            url = str(item.get("relativeUrl") or item.get("url") or "").strip()
            if not title or not url:
                continue

            matched_brand = ArbitrageEngine.is_brand_allowed(brand_raw, title)
            if not matched_brand:
                continue
            if ArbitrageEngine.has_excluded_keyword(title, url):
                continue

            raw_sku = str(item.get("modelCode") or item.get("sku") or item.get("productId") or "").strip()
            if raw_sku in seen_skus:
                continue

            # Beden ve stok seçenekleri (options: [{"option_value": "42.5", "is_in_stock": True, "qty": 2}])
            options = item.get("options") or []
            raw_sizes: list[dict[str, Any]] = []
            total_stock = 0
            has_qty = False

            if isinstance(options, list):
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    in_stock = opt.get("is_in_stock", True)
                    qty = opt.get("qty")
                    if in_stock is False or (isinstance(qty, (int, float)) and qty <= 0):
                        continue
                    size_str = opt.get("option_value") or opt.get("size") or opt.get("label")
                    if size_str:
                        qty_int = int(qty) if isinstance(qty, (int, float)) else None
                        if qty_int is not None and qty_int > 0:
                            total_stock += qty_int
                            has_qty = True
                        raw_sizes.append(
                            {
                                "size": str(size_str),
                                "stock": qty_int,
                                "available": True,
                            }
                        )

            # Fiyat ve Sepet Kampanyası (ruleInfo & animatedBadgeList)
            sale_price = item.get("specialPrice") or item.get("thirdPrice") or item.get("price")
            retail_price = (
                item.get("oldPrice")
                or item.get("firstPrice")
                or item.get("listPrice")
                or item.get("price")
                or sale_price
            )

            # Eğer discountRate > 0 ise ve oldPrice ayrı verilmemişse liste fiyatını hesapla
            discount_rate_raw = item.get("discountRate")
            try:
                disc_pct = float(str(discount_rate_raw or "0").replace("%", "").strip())
            except ValueError:
                disc_pct = 0.0

            if disc_pct > 0 and retail_price == sale_price and isinstance(sale_price, (int, float)):
                retail_price = round(float(sale_price) / (1.0 - disc_pct / 100.0), 2)

            api_basket_price: Any = None
            campaign_texts: list[str] = []

            rule_info = item.get("ruleInfo")
            if isinstance(rule_info, list):
                for rule in rule_info:
                    if isinstance(rule, dict):
                        lbl = rule.get("label") or rule.get("name") or rule.get("title")
                        if lbl:
                            campaign_texts.append(str(lbl))
                        b_price = rule.get("basketPrice") or rule.get("price")
                        if b_price and (api_basket_price is None or float(b_price) < float(api_basket_price)):
                            api_basket_price = b_price
            elif isinstance(rule_info, dict):
                if rule_info.get("label"):
                    campaign_texts.append(str(rule_info["label"]))
                if rule_info.get("basketPrice"):
                    api_basket_price = rule_info["basketPrice"]

            badges = item.get("animatedBadgeList") or []
            if isinstance(badges, list):
                for badge in badges:
                    if isinstance(badge, dict):
                        b_txt = str(badge.get("badgeText") or "").strip()
                        if b_txt and any(w in b_txt.lower() for w in ("indirim", "sepet", "tl", "%")):
                            campaign_texts.append(b_txt)

            image_url = str(item.get("imageUrl") or item.get("imageUrlVertical") or "")

            prod = self.build_scraped_product(
                raw_sku=raw_sku,
                brand=matched_brand,
                title=title,
                gender=default_gender,
                url=url,
                image_url=image_url,
                retail_price=retail_price,
                sale_price=sale_price,
                api_basket_price=api_basket_price,
                campaign_texts=campaign_texts,
                raw_sizes=raw_sizes,
                total_stock=total_stock if has_qty else None,
            )
            if prod and prod.sizes:
                seen_skus.add(raw_sku)
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results

    def _enrich_from_product_detail(
        self,
        product_url: str,
    ) -> tuple[str, list[dict[str, Any]], Any, list[str], Any, Any]:
        """
        Ürün detay sayfasından model kodu (SKU), stoktaki bedenler (tam/buçuklu/kesirli),
        sepetteki net fiyat ve kampanya rozetlerini çeker.
        """
        resp = self.client.get(product_url)
        if not resp or not resp.ok or not resp.text:
            return "", [], None, [], None, None

        html_text = resp.text
        soup = BeautifulSoup(html_text, "lxml")

        raw_sku = ""
        raw_sizes: list[dict[str, Any]] = []
        api_basket_price: Any = None
        campaign_texts: list[str] = []
        retail_price: Any = None
        sale_price: Any = None

        ld_products = self.extract_json_ld_products(html_text)
        for ld in ld_products:
            if not raw_sku:
                raw_sku = str(ld.get("mpn") or ld.get("model") or ld.get("sku") or "")
            offers = ld.get("offers")
            if isinstance(offers, dict) and not sale_price:
                sale_price = offers.get("price") or offers.get("lowPrice")

        size_selectors = [
            ".product-sizes a:not(.disabled):not(.passive):not(.out-of-stock)",
            ".product-sizes button:not([disabled]):not(.disabled):not(.passive)",
            "[data-size]:not(.disabled):not(.passive):not([disabled])",
        ]
        for sel in size_selectors:
            for el in soup.select(sel):
                sz_val = el.get("data-size") or el.get("data-value") or el.get_text(" ", strip=True)
                if sz_val and len(str(sz_val).strip()) <= 12:
                    raw_sizes.append({"size": str(sz_val).strip(), "available": True})

        return raw_sku, raw_sizes, api_basket_price, campaign_texts, retail_price, sale_price

    def _parse_listing_html(
        self,
        html_text: str,
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        # 1. Öncelikle Next.js App Router self.__next_f.push JSON state'inden ürünleri çek
        next_f_items = self._extract_next_f_products(html_text)
        if next_f_items:
            parsed_next_f = self._parse_next_f_product_items(
                raw_items=next_f_items,
                default_gender=default_gender,
                limit=limit,
            )
            if parsed_next_f:
                return parsed_next_f

        # 2. __NEXT_DATA__ kontrolü
        next_data = self.extract_next_data(html_text)
        if isinstance(next_data, dict):
            nd_items = self._extract_items_from_state_json(next_data)
            if nd_items:
                parsed_nd = self._parse_next_f_product_items(
                    raw_items=nd_items,
                    default_gender=default_gender,
                    limit=limit,
                )
                if parsed_nd:
                    return parsed_nd

        # 3. HTML ürün linkleri (/urun/...) yedek ayrıştırıcısı
        soup = BeautifulSoup(html_text, "lxml")
        results: list[ScrapedProduct] = []
        seen_hrefs: set[str] = set()

        for a_tag in soup.select("a[href^='/urun/']"):
            href = a_tag.get("href", "").strip()
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            parts = [p.strip() for p in a_tag.get_text("|", strip=True).split("|") if p.strip()]
            if len(parts) < 2:
                continue

            brand_cand = parts[0]
            title_cand = parts[1]
            matched_brand = ArbitrageEngine.is_brand_allowed(brand_cand, title_cand)
            if not matched_brand:
                continue
            if ArbitrageEngine.has_excluded_keyword(title_cand, href):
                continue

            price_texts = [p for p in parts[2:] if "TL" in p]
            campaign_texts = [p for p in parts[2:] if "İndirim" in p or "%" in p or "Sepette" in p]
            retail_txt = price_texts[0] if price_texts else ""
            sale_txt = price_texts[-1] if len(price_texts) > 1 else retail_txt

            sku_match = re.search(r"-(\d{7,10})$", href.split("?")[0])
            raw_sku = sku_match.group(1) if sku_match else ""

            raw_sizes: list[dict[str, Any]] = []
            if self.enrich_details:
                _, det_sizes, _, det_camps, _, _ = self._enrich_from_product_detail(self.build_full_url(href))
                raw_sizes.extend(det_sizes)
                campaign_texts.extend(det_camps)

            img_el = a_tag.select_one("img")
            img_url = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""

            prod = self.build_scraped_product(
                raw_sku=raw_sku,
                brand=matched_brand,
                title=title_cand,
                gender=default_gender,
                url=href,
                image_url=img_url,
                retail_price=retail_txt,
                sale_price=sale_txt,
                campaign_texts=campaign_texts,
                raw_sizes=raw_sizes,
            )
            if prod and prod.sizes:
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results

    def _extract_items_from_state_json(self, state_obj: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(state_obj, dict):
            for key in ("products", "items", "data", "hits"):
                val = state_obj.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    if any(k in val[0] for k in ("name", "title", "price", "sku", "brandName")):
                        return val
            for v in state_obj.values():
                if isinstance(v, (dict, list)):
                    sub = self._extract_items_from_state_json(v)
                    if sub:
                        found.extend(sub)
        elif isinstance(state_obj, list):
            for elem in state_obj[:15]:
                sub = self._extract_items_from_state_json(elem)
                if sub:
                    found.extend(sub)
        return found

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

            page_url = self._build_page_url(category_url, page=page)
            logger.info("[%s] Sayfa %d taranıyor: %s", self.site_name, page, page_url)

            resp = self.client.get(page_url, is_json=False)
            if not resp or not resp.ok or not resp.text:
                break

            page_items = self._parse_listing_html(
                html_text=resp.text,
                default_gender=gender,
                limit=remaining,
            )
            if not page_items:
                break

            collected.extend(page_items)
            if limit and len(collected) >= limit:
                break

        return collected
