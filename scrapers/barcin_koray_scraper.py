import logging
from typing import Optional

from bs4 import BeautifulSoup

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from scrapers.akinon_scraper import AkinonScraper

logger = logging.getLogger("arbitraj.scraper.barcin_koray")


class BarcinKorayScraper(AkinonScraper):
    """
    Nike, Adidas, Puma resmi yetkili satıcıları olan `barcin.com` ve `korayspor.com`
    outlet / indirim sayfalarını tarayan kazıyıcı.
    Hem Akinon/Next.js JSON uç noktalarını hem de özel HTML grid yapılarını destekler.
    """

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

    def _parse_html_fallback(
        self,
        html_text: str,
        default_gender: str,
        limit: Optional[int] = None,
    ) -> list[ScrapedProduct]:
        # Önce üst sınıfın (__NEXT_DATA__, universal_variable ve standart Akinon kartları) ayrıştırıcısını dene
        base_results = super()._parse_html_fallback(
            html_text=html_text,
            default_gender=default_gender,
            limit=limit,
        )
        if base_results:
            return base_results

        # Barçın ve Korayspor özel HTML ürün kartı seçicileri
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select(
            "div.productItem, div.product-item, div.showcase, div.col-product, article.product-card, [data-product-id]"
        )
        results: list[ScrapedProduct] = []

        for card in cards:
            link_el = card.select_one("a.product-link, a.showcase-Title, a.detailLink, a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href or href.startswith("#") or "javascript:" in href:
                continue

            title_el = card.select_one(
                ".product-title, .showcase-title, .productName, .detailLink, h3, h2"
            ) or link_el
            title = title_el.get_text(" ", strip=True) or link_el.get("title", "")
            if not title:
                continue

            brand_el = card.select_one(".product-brand, .showcase-brand, .brandName, .brand")
            brand = brand_el.get_text(" ", strip=True) if brand_el else (card.get("data-brand") or "")

            matched_brand = ArbitrageEngine.is_brand_allowed(brand, title)
            if not matched_brand:
                continue
            if ArbitrageEngine.has_excluded_keyword(title, href):
                continue

            raw_sku = (
                card.get("data-sku")
                or card.get("data-code")
                or card.get("data-product-code")
                or card.get("data-product-id")
                or ""
            )

            old_el = card.select_one(
                ".old-price, .discountedPrice, .showcase-price-old, .price-old, del, s"
            )
            sale_el = card.select_one(
                ".current-price, .currentPrice, .showcase-price-new, .price-new, .sale-price, .price"
            )
            basket_el = card.select_one(
                ".basket-price, .sepette-fiyat, .campaign-price, [class*='sepet']"
            )

            retail_txt = old_el.get_text(" ", strip=True) if old_el else ""
            sale_txt = sale_el.get_text(" ", strip=True) if sale_el else ""
            basket_txt = basket_el.get_text(" ", strip=True) if basket_el else ""

            campaigns: list[str] = []
            if basket_txt:
                campaigns.append(basket_txt)
            for b_el in card.select(".badge, .campaign-badge, .discount-badge, [class*='campaign']"):
                txt = b_el.get_text(" ", strip=True)
                if txt:
                    campaigns.append(txt)

            raw_sizes = []
            for sz_el in card.select(".size-item:not(.passive):not(.disabled), [data-size]:not(.disabled)"):
                sz_val = sz_el.get("data-size") or sz_el.get_text(" ", strip=True)
                if sz_val:
                    raw_sizes.append({"size": sz_val, "available": True})

            img_el = card.select_one("img")
            img_url = ""
            if img_el:
                img_url = img_el.get("data-src") or img_el.get("data-original") or img_el.get("src") or ""

            api_basket_price = None
            total_stock = None
            if not raw_sizes and self.enrich_missing_sizes:
                full_url = self.build_full_url(href)
                det_sizes, det_basket, det_campaigns, det_stock = self.fetch_product_detail_enrichment(full_url)
                raw_sizes.extend(det_sizes)
                if det_basket:
                    api_basket_price = det_basket
                campaigns.extend(det_campaigns)
                total_stock = det_stock

            prod = self.build_scraped_product(
                raw_sku=str(raw_sku),
                brand=matched_brand,
                title=title,
                gender=default_gender,
                url=href,
                image_url=img_url,
                retail_price=retail_txt or sale_txt,
                sale_price=sale_txt or retail_txt,
                api_basket_price=api_basket_price,
                campaign_texts=campaigns,
                raw_sizes=raw_sizes,
                total_stock=total_stock,
            )
            if prod and prod.sizes:
                results.append(prod)
                if limit and len(results) >= limit:
                    break

        return results
