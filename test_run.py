import io
import logging
import sys
import time

# Windows konsolunda Türkçe karakter ve emoji (UTF-8) desteği
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient
from core.notifier import TelegramNotifier
from core.parsers import BasketPromotionParser, SizeNormalizer, SKUNormalizer
from core.storage import StateStorage
from scrapers.akinon_scraper import AkinonScraper
from scrapers.barcin_koray_scraper import BarcinKorayScraper
from scrapers.instreet_scraper import InStreetScraper
from scrapers.market_comparator import MarketComparator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("arbitraj.test_run")


def verify_core_parsers() -> None:
    print("=" * 95)
    print("1. ÇEKİRDEK MOTOR DOĞRULAMASI (SKU, BUÇUKLU/KESİRLİ NUMARA VE SEPET KAMPANYA MOTORU)")
    print("=" * 95)

    # A) SKUNormalizer Testi
    sku_samples = [
        ("CW2288-111", "Nike Air Force 1 '07", "Nike"),
        ("NK-CW2288.111", "Nike Air Force 1", "Nike"),
        ("AD-ID1488-42.5", "Adidas Spezial", "Adidas"),
        ("U9060ECA", "New Balance 9060", "New Balance"),
        ("L47451800", "Salomon XT-6", "Salomon"),
    ]
    print("\n[A] SKUNormalizer Çıktıları:")
    for raw_sku, title, brand in sku_samples:
        canonical, display = SKUNormalizer.normalize(raw_sku=raw_sku, title=title, brand=brand)
        print(f"  • Ham Girdi: {raw_sku:<16} -> Display SKU: {display:<12} | Canonical SKU: {canonical}")

    # B) SizeNormalizer Testi (Kesirli ve Buçuklu Bedenler)
    raw_size_samples = [
        "37 1/3",
        "38,5",
        "38 2/3",
        "39 1/3",
        "40 2/3",
        "EU 41 1/3",
        "42",
        "42,5",
        "42 2/3",
        "42.6",
        "43 1/3",
        "44 2/3",
        "45 1/3",
        "46",
    ]
    print("\n[B] SizeNormalizer (Buçuklu & Kesirli Numara Çözücü) Çıktıları:")
    normalized_list = SizeNormalizer.filter_target_sizes(raw_size_samples, gender="Unisex")
    for s_info in normalized_list:
        frac_tag = "Kesirli -> Buçuklu Eşleşme" if s_info.is_fractional else "Standart Tam/Buçuklu"
        print(
            f"  • Ham Beden: {s_info.original_text:<10} -> Hedef Numara: {s_info.normalized_size:<6} "
            f"| Etiket: {s_info.display_label:<16} ({frac_tag})"
        )

    # C) BasketPromotionParser Testi
    promo_samples = [
        ("5.499,90 TL", "4.200 TL", None, ["Sepette %25 İndirim"]),
        ("4.999 TL", "3.500 TL", None, ["3000 TL Üzerine 750 TL İndirim"]),
        ("3.999 TL", "2.499 TL", None, ["Sepette 1.899 TL"]),
        ("6.000 TL", "4.500 TL", "3.600 TL", ["Sepette Ekstra %20"]),
    ]
    print("\n[C] BasketPromotionParser (Sepette Net Fiyat Hesaplayıcı) Çıktıları:")
    for retail, sale, api_basket, texts in promo_samples:
        res = BasketPromotionParser.calculate(
            retail_price=retail,
            sale_price=sale,
            api_basket_price=api_basket,
            campaign_texts=texts,
        )
        print(
            f"  • Liste: {res.retail_price:>7.0f} TL | Vitrin: {res.sale_price:>7.0f} TL | "
            f"Kampanya: {texts[0]:<30} -> Sepette Net: {res.net_basket_price:>7.2f} TL "
            f"(Toplam İndirim: %{res.total_discount_rate:.1f})"
        )
    print()


def print_site_products_report(
    site_title: str,
    products: list[ScrapedProduct],
    comparator: MarketComparator,
    all_collected: list[ScrapedProduct],
) -> None:
    print("=" * 95)
    print(f"🏪 SİTE: {site_title} (İlk {len(products)} Gerçek Ürün, Numara ve Arbitraj Kâr Analizi)")
    print("=" * 95)

    if not products:
        print("  [!] Bu siteden ürün alınamadı veya WAF engeli oluştu.")
        return

    for idx, prod in enumerate(products[:5], start=1):
        market_floor, floor_src = comparator.get_market_floor_price(
            product=prod,
            all_current_run_products=all_collected,
        )
        analysis = ArbitrageEngine.evaluate(
            product=prod,
            market_floor_price=market_floor,
            market_floor_source=floor_src,
        )

        sizes_str = ", ".join(s.display_label for s in prod.sizes)
        half_sizes = [s.display_label for s in prod.sizes if ".5" in s.normalized_size or s.is_fractional]
        half_str = ", ".join(half_sizes) if half_sizes else "Yok (Tam numaralar stokta)"

        print(f"\n[{idx}] {prod.brand} - {prod.title}")
        print(f"    • Kategori & SKU        : {prod.gender} | SKU: {prod.display_sku} (Canonical: {prod.canonical_sku})")
        print(f"    • Hedef Numaralar       : [{sizes_str}]")
        print(f"    • Buçuklu/Kesirli No    : [{half_str}]")
        retail_fmt = f"{prod.retail_price:,.0f}".replace(",", ".")
        sale_fmt = f"{prod.sale_price:,.0f}".replace(",", ".")
        basket_fmt = f"{prod.net_basket_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        floor_fmt = f"{analysis.market_floor_price:,.0f}".replace(",", ".")
        sell_fmt = f"{analysis.suggested_sell_price:,.0f}".replace(",", ".")
        g_profit_fmt = f"{analysis.net_profit_gardrops:+,.0f}".replace(",", ".")
        d_profit_fmt = f"{analysis.net_profit_dolap:+,.0f}".replace(",", ".")

        print(
            f"    • Fiyat & Sepet Hesabı  : Liste: {retail_fmt} TL | Vitrin: {sale_fmt} TL | "
            f"🛒 Sepette Net: {basket_fmt} TL (İndirim: %{prod.total_discount_rate:.1f} - {prod.campaign_name})"
        )
        print(
            f"    • Piyasa & Satış Hedefi : Piyasa Dip: ~{floor_fmt} TL ({analysis.market_floor_source}) | "
            f"🎯 Önerilen Hızlı Satış: {sell_fmt} TL"
        )
        print(
            f"    • Birim Ekonomisi & ROI : "
            f"🟢 Gardrops/Sahibinden Net: {g_profit_fmt} TL (%{analysis.roi_gardrops:.1f} ROI) | "
            f"🟣 Dolap Net: {d_profit_fmt} TL (%{analysis.roi_dolap:.1f} ROI)"
        )
        status_label = (
            "✅ FIRSAT KRİTERLERİNİ SAĞLIYOR"
            if analysis.is_profitable_opportunity
            else f"ℹ️ Filtre Bilgisi: {analysis.rejection_reason}"
        )
        print(f"    • Arbitraj Puanı        : {analysis.arbitrage_score}/100 | {status_label}")
        print(f"    • Ürün Linki            : {prod.url}")


def main() -> None:
    start_time = time.time()
    verify_core_parsers()

    print("=" * 95)
    print("2. CANLI E-TİCARET KAZIMA TESTİ (5 YETKİLİ DİSTRİBÜTÖR SİTE)")
    print("   Siteler: Occasion, SuperStep, InStreet, Barçın, Korayspor")
    print("   Not: Telegram'a bildirim gönderilmez, tüm veriler gerçek HTTP istekleriyle çekilir.")
    print("=" * 95)

    client = StealthHttpClient(min_delay=0.35, max_delay=0.75)
    storage = StateStorage()
    comparator = MarketComparator(http_client=client, storage=storage)

    site_scrapers = [
        ("Occasion (occasion.com.tr)", AkinonScraper(site_key="occasion", http_client=client)),
        ("SuperStep (superstep.com.tr)", AkinonScraper(site_key="superstep", http_client=client)),
        ("InStreet (instreet.com.tr)", InStreetScraper(site_key="instreet", http_client=client)),
        ("Barçın (barcin.com)", BarcinKorayScraper(site_key="barcin", http_client=client)),
        ("Korayspor (korayspor.com)", BarcinKorayScraper(site_key="korayspor", http_client=client)),
    ]

    collected_by_site: dict[str, list[ScrapedProduct]] = {}
    all_products: list[ScrapedProduct] = []

    for label, scraper in site_scrapers:
        print(f"\n>>> {label} taranıyor (İlk 3 hedef markalı ve numaralı ürün çekiliyor)...")
        items = scraper.scrape_all(max_pages_per_category=1, max_products=3)
        collected_by_site[label] = items
        all_products.extend(items)

    print("\n")
    for label, items in collected_by_site.items():
        print_site_products_report(
            site_title=label,
            products=items,
            comparator=comparator,
            all_collected=all_products,
        )
        print()

    # Örnek Telegram HTML Bildirim Şablonu Önizlemesi
    if all_products:
        sample_prod = all_products[0]
        m_floor, m_src = comparator.get_market_floor_price(sample_prod, all_current_run_products=all_products)
        sample_analysis = ArbitrageEngine.evaluate(sample_prod, market_floor_price=m_floor, market_floor_source=m_src)
        print("=" * 95)
        print("3. ÖRNEK TELEGRAM HTML BİLDİRİM ŞABLONU ÖNİZLEMESİ (sendPhoto Caption)")
        print("=" * 95)
        print(TelegramNotifier.build_html_caption(sample_analysis, notification_type="NEW_OPPORTUNITY"))
        print("=" * 95)

    client.close()
    print(f"\n✅ Test çalışması {time.time() - start_time:.1f} saniyede başarıyla tamamlandı.")


if __name__ == "__main__":
    main()
