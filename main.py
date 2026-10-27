import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import config
from core.arbitrage_engine import ArbitrageAnalysis, ArbitrageEngine, ScrapedProduct
from core.notifier import TelegramNotifier
from core.storage import StateStorage
from core.telegram_bot_commands import TelegramCommandHandler
from scrapers.akinon_scraper import AkinonScraper
from scrapers.barcin_koray_scraper import BarcinKorayScraper
from scrapers.base_scraper import BaseScraper
from scrapers.instreet_scraper import InStreetScraper
from scrapers.market_comparator import MarketComparator


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


logger = logging.getLogger("arbitraj.main")


def create_scraper(site_key: str) -> BaseScraper:
    if site_key in ("occasion", "superstep", "sportive", "fashfed"):
        return AkinonScraper(site_key=site_key)
    if site_key == "instreet":
        return InStreetScraper(site_key=site_key)
    if site_key in ("barcin", "korayspor"):
        return BarcinKorayScraper(site_key=site_key)
    raise ValueError(f"Desteklenmeyen site anahtarı: {site_key}")


def _scrape_single_site_worker(
    site_key: str,
    max_pages: int,
    max_products: Optional[int],
) -> tuple[str, list[ScrapedProduct]]:
    scraper = create_scraper(site_key)
    try:
        items = scraper.scrape_all(
            max_pages_per_category=max_pages,
            max_products=max_products,
        )
        return site_key, items
    except Exception as exc:
        logger.error("[%s] Tarama işçisi hatası: %s", site_key, exc)
        return site_key, []
    finally:
        scraper.client.close()


def _interleave_candidates_by_site(products: list[ScrapedProduct]) -> list[ScrapedProduct]:
    """
    Tek bir mağazanın tüm Akakçe sorgu kotasını tekeline almasını önlemek için
    her mağazanın en yüksek indirimli ürünlerini Round-Robin düzeninde harmanlar.
    """
    by_site: dict[str, list[ScrapedProduct]] = {}
    for p in products:
        by_site.setdefault(p.site_key, []).append(p)

    for s_key in by_site:
        by_site[s_key].sort(
            key=lambda x: (x.basket_discount_rate > 0, x.total_discount_rate),
            reverse=True,
        )

    interleaved: list[ScrapedProduct] = []
    max_len = max((len(v) for v in by_site.values()), default=0)
    site_keys = list(by_site.keys())

    for idx in range(max_len):
        for s_key in site_keys:
            site_list = by_site[s_key]
            if idx < len(site_list):
                interleaved.append(site_list[idx])

    return interleaved


def run_arbitrage_cycle(
    target_sites: list[str],
    max_pages: int = config.MAX_PAGES_PER_CATEGORY,
    max_products_per_site: Optional[int] = None,
    dry_run: bool = False,
) -> list[ArbitrageAnalysis]:
    """
    Tüm hedef siteleri ThreadPoolExecutor ile eşzamanlı tarar,
    durum veritabanını günceller, yüksek indirimli ürünleri Akakçe ve çapraz
    piyasa ile doğrulayıp arbitraj kriterlerini sağlayanları Telegram'a bildirir.
    """
    start_ts = time.time()
    storage = StateStorage()
    notifier = TelegramNotifier()
    comparator = MarketComparator(storage=storage)
    cmd_handler = TelegramCommandHandler(storage=storage, comparator=comparator)

    if not dry_run:
        processed_cmds = cmd_handler.process_pending_commands()
        if processed_cmds > 0:
            logger.info("Telegram üzerinden %d kullanıcı komutu işlendi.", processed_cmds)

    logger.info(
        "Arbitraj tarama döngüsü başlatıldı | Siteler: %s | İlk Çalışma Mı: %s",
        ", ".join(target_sites),
        storage.is_first_run,
    )

    all_scraped: list[ScrapedProduct] = []
    workers = min(config.MAX_WORKERS, max(1, len(target_sites)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_scrape_single_site_worker, s_key, max_pages, max_products_per_site): s_key
            for s_key in target_sites
        }
        for future in as_completed(futures):
            s_key = futures[future]
            try:
                _, site_products = future.result()
                logger.info("[%s] %d ürün alındı.", s_key.upper(), len(site_products))
                all_scraped.extend(site_products)
            except Exception as exc:
                logger.error("[%s] Eşzamanlı görev hatası: %s", s_key, exc)

    # 1. Tüm ürünleri StateStorage içine kaydet (çapraz site fiyat karşılaştırması ve fiyat geçmişi için)
    for prod in all_scraped:
        storage.upsert_scraped_product(prod)

    profitable_opportunities: list[ArbitrageAnalysis] = []
    notified_count = 0
    skipped_first_run_count = 0

    # 2. Ön filtreleri geçen ürünler için site-dengeli (Round-Robin) sırada piyasa dip fiyatını sorgula
    balanced_candidates = _interleave_candidates_by_site(all_scraped)
    live_akakce_queries = 0
    max_live_akakce_per_cycle = 35

    for prod in balanced_candidates:
        passed_pre, _ = ArbitrageEngine.check_pre_filters(prod)
        if not passed_pre:
            continue

        should_send_preview, preview_type = storage.should_notify(prod)
        use_live_akakce = should_send_preview and (live_akakce_queries < max_live_akakce_per_cycle)
        if use_live_akakce:
            live_akakce_queries += 1

        market_floor, floor_source = comparator.get_market_floor_price(
            product=prod,
            all_current_run_products=all_scraped,
            allow_live_query=use_live_akakce,
        )
        trend_badge = storage.get_price_trend_summary(prod.canonical_sku, prod.site_key, prod.net_basket_price)
        analysis = ArbitrageEngine.evaluate(
            product=prod,
            market_floor_price=market_floor,
            market_floor_source=floor_source,
            price_trend_badge=trend_badge,
        )

        if not analysis.is_profitable_opportunity and preview_type != "WATCHLIST_ALERT":
            continue

        profitable_opportunities.append(analysis)

        if not should_send_preview:
            if preview_type == "SKIP_FIRST_RUN":
                skipped_first_run_count += 1
            continue

        if dry_run:
            logger.info(
                "[DRY-RUN] Fırsat Yakalandı (%s): %s - %s | Alış: %.0f TL | Gardrops Net: +%.0f TL (%%%0.f ROI) | Puan: %d/100",
                preview_type,
                prod.site_name,
                prod.display_sku,
                prod.net_basket_price,
                analysis.net_profit_gardrops,
                analysis.roi_gardrops,
                analysis.arbitrage_score,
            )
            notified_count += 1
        else:
            sent_ok = notifier.send_opportunity(analysis, notification_type=preview_type)
            if sent_ok or not notifier.is_configured:
                storage.mark_notified(prod)
                notified_count += 1

    # 3. Durum dosyasını (data/products_state.json) diske kaydet (Dry-run değilse)
    if not dry_run:
        storage.save()
    comparator.client.close()
    cmd_handler.client.close()

    elapsed = time.time() - start_ts
    logger.info(
        "Döngü tamamlandı (%.1f sn) | Toplam Ürün: %d | Kârlı Fırsat: %d | Bildirilen: %d | İlk Çalışma Koruması: %d",
        elapsed,
        len(all_scraped),
        len(profitable_opportunities),
        notified_count,
        skipped_first_run_count,
    )
    return profitable_opportunities


def run_telegram_command_listener() -> None:
    """Telegram komutlarını (`/durum`, `/firsatlar`, `/ara`, `/filtre`, `/takip`) anlık dinler."""
    storage = StateStorage()
    comparator = MarketComparator(storage=storage)
    handler = TelegramCommandHandler(storage=storage, comparator=comparator)
    logger.info("Telegram İnteraktif Komut Botu dinlemede (/yardim, /durum, /firsatlar, /ara, /filtre, /takip)...")
    try:
        while True:
            storage._load()
            count = handler.process_pending_commands()
            if count > 0:
                storage.save()
                logger.info("%d Telegram komutu yanıtlandı.", count)
            time.sleep(2.0)
    except KeyboardInterrupt:
        logger.info("Komut dinleyici durduruldu.")
    finally:
        comparator.client.close()
        handler.client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Türkiye Yetkili Distribütör E-Ticaret Arbitraj & Flipping Radarı"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Tek seferlik tarama çalıştır ve çık (GitHub Actions varsayılanı)",
    )
    mode_group.add_argument(
        "--loop",
        action="store_true",
        help="Belirtilen aralıklarla (varsayılan 15 dk) 7/24 kesintisiz döngüde çalıştır",
    )
    mode_group.add_argument(
        "--listen-commands",
        action="store_true",
        help="Telegram interaktif komutlarını (/ara, /firsatlar, /filtre, /takip) anlık dinle",
    )
    parser.add_argument(
        "--sites",
        type=str,
        default="occasion,superstep,instreet,barcin,korayspor,sportive,fashfed",
        help="Taranacak siteler (virgülle ayrılmış: occasion,superstep,instreet,barcin,korayspor,sportive,fashfed)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=config.MAX_PAGES_PER_CATEGORY,
        help="Kategori başına taranacak maksimum sayfa sayısı",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Site başına toplanacak maksimum ürün sınırı (isteğe bağlı)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Telegram'a mesaj göndermeden sadece analiz ve kayıt yap",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Detaylı hata ayıklama (DEBUG) loglarını aç",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(verbose=args.verbose)

    if args.listen_commands:
        run_telegram_command_listener()
        return

    target_sites = [s.strip().lower() for s in args.sites.split(",") if s.strip() in config.SITE_TARGETS]
    if not target_sites:
        logger.error("Geçerli hiçbir hedef site seçilmedi! Mevcut siteler: %s", list(config.SITE_TARGETS.keys()))
        sys.exit(1)

    if args.loop:
        logger.info("7/24 Sürekli Tarama Modu (--loop) aktif. Döngü aralığı: %d saniye.", config.LOOP_INTERVAL_SECONDS)
        while True:
            try:
                run_arbitrage_cycle(
                    target_sites=target_sites,
                    max_pages=args.max_pages,
                    max_products_per_site=args.max_products,
                    dry_run=args.dry_run,
                )
            except KeyboardInterrupt:
                logger.info("Kullanıcı tarafından durduruldu.")
                break
            except Exception as exc:
                logger.exception("Döngü sırasında beklenmeyen hata: %s", exc)

            logger.info("Sonraki tarama için %d saniye bekleniyor...", config.LOOP_INTERVAL_SECONDS)
            time.sleep(config.LOOP_INTERVAL_SECONDS)
    else:
        run_arbitrage_cycle(
            target_sites=target_sites,
            max_pages=args.max_pages,
            max_products_per_site=args.max_products,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
