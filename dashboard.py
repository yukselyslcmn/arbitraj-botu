import argparse
import json
import socket
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.listing_generator import ListingGenerator
from core.parsers import NormalizedSizeInfo
from core.storage import StateStorage


def get_local_network_ip() -> str:
    """Yerel ağ (Wi-Fi / LAN) IP adresini tespit eder."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return str(ip)
    except OSError:
        return "127.0.0.1"


def build_dashboard_payload(compact: bool = False) -> dict[str, Any]:
    """
    `data/products_state.json` verisini okuyup tüm ürünler için kâr/ROI ve ilan şablonu üretir.
    `compact=True` (GitHub Pages bulut paneli) modunda bedeni olmayan referans katalog ürünlerinin
    ilan metinleri ve fiyat geçmişi çıkarılarak mobil veri tüketimi azaltılır.
    """
    storage = StateStorage()
    products_map = storage.state.get("products") or {}

    store_counts: dict[str, int] = {}
    brand_counts: dict[str, int] = {}
    items_list: list[dict[str, Any]] = []
    profitable_count = 0

    for c_sku, sku_entry in products_map.items():
        if not isinstance(sku_entry, dict):
            continue
        brand = str(sku_entry.get("brand") or "Diğer")
        title = str(sku_entry.get("title") or "")
        gender = str(sku_entry.get("gender") or "Unisex")
        d_sku = str(sku_entry.get("display_sku") or c_sku)
        verified_floor = sku_entry.get("verified_market_floor_price")
        verified_src = str(sku_entry.get("verified_market_floor_source") or "Çapraz Mağaza / Piyasa")
        verified_url = sku_entry.get("verified_akakce_url")

        sites_map = sku_entry.get("sites") or {}
        for s_key, s_data in sites_map.items():
            if not isinstance(s_data, dict):
                continue
            s_name = str(s_data.get("site_name") or s_key)
            store_counts[s_name] = store_counts.get(s_name, 0) + 1
            brand_counts[brand] = brand_counts.get(brand, 0) + 1

            raw_sizes = s_data.get("sizes") or []
            sizes = [NormalizedSizeInfo(original_text=str(sz), normalized_size=str(sz)) for sz in raw_sizes]
            net_basket = float(s_data.get("net_basket_price") or 0)
            if net_basket <= 0:
                continue

            prod = ScrapedProduct(
                site_key=s_key,
                site_name=s_name,
                canonical_sku=str(c_sku),
                display_sku=d_sku,
                brand=brand,
                title=title,
                gender=gender,
                url=str(s_data.get("url") or ""),
                image_url=str(s_data.get("image_url") or ""),
                retail_price=float(s_data.get("retail_price") or net_basket),
                sale_price=float(s_data.get("sale_price") or net_basket),
                net_basket_price=net_basket,
                total_discount_rate=float(s_data.get("total_discount_rate") or 0),
                basket_discount_rate=0.0,
                campaign_name=str(s_data.get("campaign_name") or "Standart Fiyat"),
                sizes=sizes,
                verified_akakce_url=str(verified_url) if verified_url else None,
            )

            floor_val = float(verified_floor) if isinstance(verified_floor, (int, float)) and verified_floor > 0 else None
            floor_source = verified_src
            if floor_val is None:
                cross = storage.get_cross_site_floor_price(str(c_sku), s_key, brand, gender)
                if cross:
                    floor_val, floor_source = cross

            trend_badge = storage.get_price_trend_summary(str(c_sku), s_key, net_basket)
            analysis = ArbitrageEngine.evaluate(
                product=prod,
                market_floor_price=floor_val,
                market_floor_source=floor_source,
                price_trend_badge=trend_badge,
            )
            is_profitable = bool(analysis.is_profitable_opportunity and len(prod.sizes) > 0)
            if is_profitable:
                profitable_count += 1

            item: dict[str, Any] = {
                "sku": d_sku,
                "site_name": s_name,
                "brand": brand,
                "title": title,
                "gender": gender,
                "url": prod.url,
                "akakce_url": prod.akakce_search_url,
                "image_url": prod.image_url,
                "retail_price": round(prod.retail_price),
                "net_basket_price": round(prod.net_basket_price),
                "total_discount_rate": round(prod.total_discount_rate, 1),
                "campaign_name": prod.campaign_name,
                "sizes": [s.normalized_size for s in prod.sizes],
                "market_floor_price": round(analysis.market_floor_price),
                "market_floor_source": analysis.market_floor_source,
                "suggested_sell_price": round(analysis.suggested_sell_price),
                "net_profit_gardrops": round(analysis.net_profit_gardrops),
                "roi_gardrops": round(analysis.roi_gardrops, 1),
                "net_profit_dolap": round(analysis.net_profit_dolap),
                "roi_dolap": round(analysis.roi_dolap, 1),
                "arbitrage_score": analysis.arbitrage_score,
                "is_profitable": is_profitable,
                "price_trend_badge": analysis.price_trend_badge,
                "is_inflated_retail": analysis.is_inflated_retail_price,
            }

            # Bulut modunda sadece bedeni olan alınabilir ürünlere ilan şablonu ekle (veri tasarrufu)
            if not compact or prod.sizes:
                listing_tpl = ListingGenerator.generate(analysis)
                item["listing_title"] = listing_tpl.title
                item["listing_desc"] = listing_tpl.description
                item["listing_price"] = listing_tpl.recommended_price_tl
                item["listing_min_price"] = listing_tpl.min_acceptable_price_tl
            if not compact:
                item["price_history"] = s_data.get("price_history") or []

            items_list.append(item)

    items_list.sort(key=lambda x: (x["is_profitable"], x["roi_gardrops"], x["net_profit_gardrops"]), reverse=True)

    lan_ip = get_local_network_ip()
    return {
        "meta": storage.state.get("meta") or {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network": {
            "public_url": config.PUBLIC_DASHBOARD_URL,
            "local_url": f"http://localhost:{config.LOCAL_DASHBOARD_PORT}",
            "lan_url": "" if compact else f"http://{lan_ip}:{config.LOCAL_DASHBOARD_PORT}",
        },
        "thresholds": {
            "max_basket_price": config.MAX_BASKET_PRICE,
            "min_total_discount_rate": config.MIN_TOTAL_DISCOUNT_RATE,
            "min_net_profit_tl": config.MIN_NET_PROFIT_TL,
            "min_roi_percent": config.MIN_ROI_PERCENT,
        },
        "summary": {
            "unique_skus": len(products_map),
            "total_listings": len(items_list),
            "profitable_count": profitable_count,
            "store_counts": store_counts,
            "brand_counts": brand_counts,
        },
        "items": items_list,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="theme-color" content="#020617" />
  <meta name="robots" content="noindex, nofollow" />
  <title>Arbitraj Radarı — Kontrol Paneli</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: #0f172a; }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
    .chip-scroll { scrollbar-width: none; }
    .chip-scroll::-webkit-scrollbar { display: none; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans selection:bg-emerald-500 selection:text-white">
  <div id="toast" class="fixed bottom-5 right-5 left-5 sm:left-auto z-50 translate-y-24 opacity-0 transition-all duration-300 bg-emerald-600 text-white text-sm font-semibold px-4 py-3 rounded-xl shadow-2xl border border-emerald-400 text-center sm:text-left">
    <span id="toastMsg">İşlem başarılı!</span>
  </div>

  <header class="border-b border-slate-800 bg-slate-900/90 backdrop-blur sticky top-0 z-30">
    <div class="max-w-7xl mx-auto px-3 sm:px-4 py-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-10 h-10 shrink-0 rounded-xl bg-gradient-to-tr from-emerald-600 to-cyan-500 flex items-center justify-center text-xl">👟</div>
        <div class="min-w-0">
          <h1 class="text-base sm:text-xl font-extrabold bg-gradient-to-r from-emerald-400 via-cyan-300 to-indigo-400 bg-clip-text text-transparent truncate">Arbitraj &amp; Flipping Radarı</h1>
          <div class="flex items-center gap-1.5 text-[11px]">
            <span id="modeBadge" class="px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 border border-slate-700">Yükleniyor…</span>
            <span id="lastUpdated" class="text-slate-400 truncate"></span>
          </div>
        </div>
      </div>
      <div class="flex items-center gap-2 text-xs">
        <button onclick="openShareModal()" class="bg-slate-800 hover:bg-slate-700 border border-slate-700 px-3 py-2 rounded-lg">📱 <span class="hidden sm:inline">Her Yerden Aç</span></button>
        <button onclick="exportCSV()" class="bg-slate-800 hover:bg-slate-700 border border-slate-700 px-3 py-2 rounded-lg">📥 <span class="hidden sm:inline">CSV</span></button>
        <button onclick="loadData(true)" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-3 py-2 rounded-lg">🔄 <span class="hidden sm:inline">Yenile</span></button>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-3 sm:px-4 py-5 space-y-5">
    <section class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5">
        <div class="text-xs text-slate-400">İndekslenen Model</div>
        <div id="kpiSkus" class="text-2xl lg:text-3xl font-extrabold text-white mt-1">-</div>
        <div class="text-[11px] text-slate-500 mt-1">7 yetkili mağaza</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5">
        <div class="text-xs text-slate-400">Kârlı Fırsat</div>
        <div id="kpiProfitable" class="text-2xl lg:text-3xl font-extrabold text-emerald-400 mt-1">-</div>
        <div id="kpiThresholds" class="text-[11px] text-emerald-500/80 mt-1">-</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5">
        <div class="text-xs text-slate-400">En Yüksek Net Kâr</div>
        <div id="kpiMaxProfit" class="text-2xl lg:text-3xl font-extrabold text-cyan-400 mt-1">-</div>
        <div class="text-[11px] text-cyan-500/80 mt-1">Gardrops (%6 + 95₺)</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5">
        <div class="text-xs text-slate-400">Ortalama ROI</div>
        <div id="kpiAvgRoi" class="text-2xl lg:text-3xl font-extrabold text-amber-300 mt-1">-</div>
        <div class="text-[11px] text-slate-500 mt-1">Kârlı fırsatlarda</div>
      </div>
    </section>

    <section class="bg-slate-900 border border-slate-800 rounded-xl p-3.5 space-y-3 sticky top-[68px] z-20 shadow-xl shadow-black/30">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-2.5">
        <div class="md:col-span-2 relative">
          <input id="searchInput" type="search" placeholder="🔍 Model, marka veya SKU (Örn: Spezial, IF6491, 530)" oninput="debouncedRender()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm placeholder-slate-400 focus:outline-none focus:border-emerald-500" />
        </div>
        <select id="storeFilter" onchange="filterAndRender()" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-emerald-500">
          <option value="">🏪 Tüm Mağazalar</option>
        </select>
        <select id="sortFilter" onchange="filterAndRender()" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm font-semibold focus:outline-none focus:border-emerald-500">
          <option value="roi_desc">🚀 En Yüksek ROI</option>
          <option value="profit_desc">💰 En Yüksek Net Kâr</option>
          <option value="discount_desc">🏷️ En Yüksek İndirim</option>
          <option value="buy_asc">📉 En Düşük Alış</option>
          <option value="score_desc">⭐ Arbitraj Puanı</option>
        </select>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div id="sizeChips" class="flex items-center gap-1.5 overflow-x-auto chip-scroll max-w-full"></div>
        <div class="flex items-center gap-3">
          <select id="genderFilter" onchange="filterAndRender()" class="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1">
            <option value="">🚻 Tümü</option>
            <option value="Erkek">🚹 Erkek</option>
            <option value="Kadın">🚺 Kadın</option>
          </select>
          <label class="flex items-center gap-1.5 cursor-pointer select-none">
            <input id="onlyProfitable" type="checkbox" checked onchange="filterAndRender()" class="w-4 h-4 accent-emerald-500" />
            <span class="text-slate-300 font-semibold">Sadece kârlı</span>
          </label>
          <span id="filteredCountBadge" class="px-2 py-1 bg-slate-800 text-emerald-400 font-bold rounded-lg border border-slate-700">0</span>
        </div>
      </div>
    </section>

    <section id="itemsGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5"></section>
    <div class="text-center">
      <button id="loadMoreBtn" onclick="showMore()" class="hidden bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm font-bold px-5 py-2.5 rounded-xl">Daha Fazla Göster</button>
    </div>

    <section class="grid grid-cols-1 lg:grid-cols-3 gap-3.5">
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4">
        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">🏪 Mağaza Dağılımı</h2>
        <canvas id="storeChart" height="170"></canvas>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4">
        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">👟 Markalar</h2>
        <canvas id="brandChart" height="170"></canvas>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 space-y-3">
        <h2 class="text-xs font-bold uppercase tracking-wider text-emerald-400">🧮 Kâr / ROI Simülatörü</h2>
        <div class="grid grid-cols-2 gap-2.5">
          <label class="text-[11px] text-slate-400">Sepette Alış (TL)
            <input id="simBuy" type="number" inputmode="numeric" value="2800" oninput="runSim()" class="mt-1 w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-sm font-bold text-emerald-400" />
          </label>
          <label class="text-[11px] text-slate-400">Piyasa En Ucuz (TL)
            <input id="simFloor" type="number" inputmode="numeric" value="4900" oninput="runSim()" class="mt-1 w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-sm font-bold text-rose-400" />
          </label>
        </div>
        <div class="bg-slate-950/80 border border-slate-800 rounded-xl p-3 text-xs space-y-1.5">
          <div class="flex justify-between"><span>Önerilen Satış (-%12):</span><span id="simSell" class="font-bold">-</span></div>
          <div class="flex justify-between text-emerald-300 font-bold"><span>🟢 Gardrops Net:</span><span id="simGardrops">-</span></div>
          <div class="flex justify-between text-cyan-300 font-bold"><span>🟣 Dolap Net:</span><span id="simDolap">-</span></div>
        </div>
      </div>
    </section>
  </main>

  <div id="detailModal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden items-end sm:items-center justify-center sm:p-4" onclick="if(event.target===this)closeModal()">
    <div class="bg-slate-900 border border-slate-800 rounded-t-2xl sm:rounded-2xl max-w-2xl w-full p-5 space-y-4 overflow-y-auto max-h-[92vh] custom-scrollbar">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <span id="modalStore" class="text-xs font-bold text-cyan-400 bg-cyan-950/80 px-2.5 py-0.5 rounded-full border border-cyan-800"></span>
          <h2 id="modalTitle" class="text-base sm:text-lg font-bold text-white mt-1.5"></h2>
          <div id="modalSku" class="text-xs font-mono text-slate-400 break-all"></div>
        </div>
        <button onclick="closeModal()" class="text-slate-400 hover:text-white text-2xl px-2 leading-none">×</button>
      </div>
      <div class="grid grid-cols-3 gap-2 bg-slate-950 p-3 rounded-xl border border-slate-800 text-center text-xs">
        <div><div class="text-slate-400">Sepette Alış</div><div id="modalBuyPrice" class="text-base font-extrabold text-emerald-400 mt-0.5"></div></div>
        <div><div class="text-slate-400">Piyasa Dip</div><div id="modalFloorPrice" class="text-base font-bold text-rose-400 mt-0.5"></div></div>
        <div><div class="text-slate-400">Önerilen Satış</div><div id="modalSellPrice" class="text-base font-bold text-cyan-400 mt-0.5"></div></div>
      </div>
      <div id="modalFloorSource" class="text-[11px] text-slate-400"></div>
      <div class="bg-emerald-950/30 border border-emerald-800/60 rounded-xl p-3 text-xs space-y-1.5">
        <div class="flex justify-between gap-2"><span class="text-slate-300">🟢 Gardrops / Sahibinden:</span><span id="modalGardropsNet" class="font-bold text-emerald-300"></span></div>
        <div class="flex justify-between gap-2"><span class="text-slate-300">🟣 Dolap:</span><span id="modalDolapNet" class="font-bold text-cyan-300"></span></div>
      </div>
      <div id="modalSizes" class="flex flex-wrap gap-1"></div>
      <div id="modalListingBlock" class="space-y-3 pt-2 border-t border-slate-800">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold uppercase text-slate-300">📋 Hazır İlan Şablonu</span>
          <button onclick="copyModalFullTemplate()" class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Tümünü Kopyala</button>
        </div>
        <div class="flex gap-2">
          <input id="modalListingTitle" type="text" readonly class="flex-1 min-w-0 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs font-mono" />
          <button onclick="copyText(document.getElementById('modalListingTitle').value, 'Başlık kopyalandı!')" class="bg-slate-800 hover:bg-slate-700 px-3 text-xs rounded-lg border border-slate-700">Kopyala</button>
        </div>
        <textarea id="modalListingDesc" rows="6" readonly class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-xs font-mono text-slate-300 custom-scrollbar"></textarea>
      </div>
      <div class="flex gap-2 pt-2 border-t border-slate-800 text-xs font-semibold">
        <a id="modalBuyLink" href="#" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 text-white py-2.5 rounded-xl">🛒 Mağazada Aç</a>
        <a id="modalAkakceLink" href="#" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-slate-800 hover:bg-slate-700 py-2.5 rounded-xl">📊 Akakçe</a>
      </div>
    </div>
  </div>

  <div id="shareModal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden items-center justify-center p-4" onclick="if(event.target===this)closeShareModal()">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 space-y-4 text-center">
      <div class="text-3xl">🌍</div>
      <h2 class="text-lg font-bold text-white">Paneli Her Yerden Aç</h2>
      <div id="publicUrlBlock" class="space-y-2 text-left">
        <div class="text-xs text-slate-400">☁️ Bulut Panel (mobil veri dahil her yerden, ~15 dk'da bir güncellenir):</div>
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-2.5 flex items-center justify-between gap-2">
          <a id="publicUrlDisplay" href="#" target="_blank" rel="noopener noreferrer" class="font-mono text-emerald-400 text-xs break-all"></a>
          <button onclick="copyText(document.getElementById('publicUrlDisplay').textContent, 'Bulut panel linki kopyalandı!')" class="shrink-0 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Kopyala</button>
        </div>
      </div>
      <div id="lanUrlBlock" class="space-y-2 text-left">
        <div class="text-xs text-slate-400">🏠 Aynı Wi-Fi ağındaki telefon (canlı yerel veri):</div>
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-2.5 flex items-center justify-between gap-2">
          <span id="lanUrlDisplay" class="font-mono text-cyan-400 text-xs break-all"></span>
          <button onclick="copyText(document.getElementById('lanUrlDisplay').textContent, 'Wi-Fi linki kopyalandı!')" class="shrink-0 bg-slate-700 hover:bg-slate-600 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Kopyala</button>
        </div>
      </div>
      <p class="text-[11px] text-slate-500">Telegram botuna <b>/panel</b> yazabilir veya sohbetin sol altındaki <b>📊 Panel</b> menü butonuna dokunabilirsiniz.</p>
      <button onclick="closeShareModal()" class="w-full bg-slate-800 hover:bg-slate-700 text-xs font-bold py-2.5 rounded-xl">Kapat</button>
    </div>
  </div>

  <script>
    const PAGE_SIZE = 30;
    const SIZE_OPTIONS = ['', '41', '42', '42.5', '43', '44', '45', '37.5', '38', '38.5', '39', '40'];
    let rawData = null;
    let selectedSize = '';
    let visibleCount = PAGE_SIZE;
    let renderedItems = [];
    let currentModalItem = null;
    let storeChartInst = null;
    let brandChartInst = null;
    let renderTimer = null;

    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.initData !== undefined) { try { tg.ready(); tg.expand(); } catch (e) {} }

    function esc(v) {
      return String(v === undefined || v === null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function safeUrl(u) {
      const s = String(u || '');
      return /^https?:\\/\\//i.test(s) ? s : '#';
    }
    function tl(n) { return Number(n || 0).toLocaleString('tr-TR') + ' TL'; }
    function signedTl(n) { const v = Number(n || 0); return (v >= 0 ? '+' : '') + v.toLocaleString('tr-TR') + ' TL'; }

    function showToast(msg) {
      const el = document.getElementById('toast');
      document.getElementById('toastMsg').textContent = msg;
      el.classList.remove('translate-y-24', 'opacity-0');
      clearTimeout(window._toastT);
      window._toastT = setTimeout(() => el.classList.add('translate-y-24', 'opacity-0'), 2300);
    }

    async function copyText(text, msg) {
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
      }
      showToast(msg || 'Kopyalandı!');
    }

    function openShareModal() { const m = document.getElementById('shareModal'); m.classList.remove('hidden'); m.classList.add('flex'); }
    function closeShareModal() { const m = document.getElementById('shareModal'); m.classList.add('hidden'); m.classList.remove('flex'); }

    function renderSizeChips() {
      document.getElementById('sizeChips').innerHTML = '<span class="text-slate-400 font-semibold shrink-0">👟</span>' +
        SIZE_OPTIONS.map(sz => {
          const active = sz === selectedSize;
          const cls = active ? 'bg-emerald-600 text-white border-emerald-500 font-bold' : 'bg-slate-800 text-slate-300 border-slate-700';
          return `<button data-size="${esc(sz)}" class="size-chip shrink-0 px-2.5 py-1 rounded-md border ${cls}">${sz ? esc(sz) : 'Tümü'}</button>`;
        }).join('');
      document.querySelectorAll('.size-chip').forEach(btn => btn.addEventListener('click', () => {
        selectedSize = btn.dataset.size; renderSizeChips(); filterAndRender();
      }));
    }

    function runSim() {
      const buy = parseFloat(document.getElementById('simBuy').value || 0);
      const floor = parseFloat(document.getElementById('simFloor').value || 0);
      if (buy <= 0 || floor <= 0) return;
      const sell = Math.round(floor * 0.88);
      const netG = Math.round(sell * 0.94 - 95 - buy);
      const netD = Math.round(sell * 0.85 - 95 - buy);
      document.getElementById('simSell').textContent = tl(sell);
      document.getElementById('simGardrops').textContent = `${signedTl(netG)} (%${((netG / buy) * 100).toFixed(1)})`;
      document.getElementById('simDolap').textContent = `${signedTl(netD)} (%${((netD / buy) * 100).toFixed(1)})`;
    }

    function openModal(idx) {
      const it = renderedItems[idx];
      if (!it) return;
      currentModalItem = it;
      document.getElementById('modalStore').textContent = `${it.site_name} • ${it.gender}`;
      document.getElementById('modalTitle').textContent = `${it.brand} - ${it.title}`;
      document.getElementById('modalSku').textContent = `SKU: ${it.sku} | ${it.campaign_name}`;
      document.getElementById('modalBuyPrice').textContent = tl(it.net_basket_price);
      document.getElementById('modalFloorPrice').textContent = '~' + tl(it.market_floor_price);
      document.getElementById('modalSellPrice').textContent = tl(it.suggested_sell_price);
      document.getElementById('modalFloorSource').textContent = `Piyasa kaynağı: ${it.market_floor_source}` + (it.is_inflated_retail ? ' • ⚠️ Şişirilmiş liste fiyatı' : '');
      document.getElementById('modalGardropsNet').textContent = `${signedTl(it.net_profit_gardrops)} (%${it.roi_gardrops} ROI)`;
      document.getElementById('modalDolapNet').textContent = `${signedTl(it.net_profit_dolap)} (%${it.roi_dolap} ROI)`;
      document.getElementById('modalSizes').innerHTML = (it.sizes || []).map(s => `<span class="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 font-mono text-xs">${esc(s)}</span>`).join('');
      const hasListing = !!it.listing_title;
      document.getElementById('modalListingBlock').classList.toggle('hidden', !hasListing);
      document.getElementById('modalListingTitle').value = it.listing_title || '';
      document.getElementById('modalListingDesc').value = it.listing_desc || '';
      document.getElementById('modalBuyLink').href = safeUrl(it.url);
      document.getElementById('modalAkakceLink').href = safeUrl(it.akakce_url);
      const m = document.getElementById('detailModal'); m.classList.remove('hidden'); m.classList.add('flex');
    }
    function closeModal() { const m = document.getElementById('detailModal'); m.classList.add('hidden'); m.classList.remove('flex'); }

    function copyModalFullTemplate() {
      const it = currentModalItem;
      if (!it || !it.listing_title) return;
      copyText(`${it.listing_title}\\nFiyat: ${it.listing_price} TL (Min Pazarlık: ${it.listing_min_price} TL)\\n\\n${it.listing_desc}`, 'Tam ilan şablonu kopyalandı!');
    }

    function exportCSV() {
      if (!renderedItems.length) { showToast('İndirilecek ürün yok.'); return; }
      const q = v => `"${String(v === undefined || v === null ? '' : v).replace(/"/g, '""')}"`;
      const headers = ['SKU','Marka','Model','Cinsiyet','Magaza','Sepette_Alis_TL','Piyasa_Dip_TL','Onerilen_Satis_TL','Gardrops_Net_TL','Gardrops_ROI','Dolap_Net_TL','Dolap_ROI','Numaralar','Magaza_URL','Akakce_URL'];
      const rows = renderedItems.map(it => [q(it.sku), q(it.brand), q(it.title), q(it.gender), q(it.site_name), it.net_basket_price, it.market_floor_price, it.suggested_sell_price, it.net_profit_gardrops, it.roi_gardrops, it.net_profit_dolap, it.roi_dolap, q((it.sizes || []).join(';')), q(it.url), q(it.akakce_url)].join(','));
      const blob = new Blob(['\\uFEFF' + [headers.join(','), ...rows].join('\\n')], { type: 'text/csv;charset=utf-8;' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `arbitraj_firsatlari_${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      showToast('CSV indirildi!');
    }

    function debouncedRender() { clearTimeout(renderTimer); renderTimer = setTimeout(filterAndRender, 180); }

    function filterAndRender() {
      if (!rawData) return;
      const q = (document.getElementById('searchInput').value || '').toLocaleLowerCase('tr-TR').trim();
      const store = document.getElementById('storeFilter').value;
      const gender = document.getElementById('genderFilter').value;
      const sortBy = document.getElementById('sortFilter').value;
      const onlyProf = document.getElementById('onlyProfitable').checked;

      const list = rawData.items.filter(it => {
        if (onlyProf && !it.is_profitable) return false;
        if (store && it.site_name !== store) return false;
        if (gender && it.gender !== gender) return false;
        if (selectedSize && !(it.sizes || []).includes(selectedSize)) return false;
        if (q && !`${it.brand} ${it.title} ${it.sku} ${it.site_name}`.toLocaleLowerCase('tr-TR').includes(q)) return false;
        return true;
      });
      const sorters = {
        roi_desc: (a, b) => b.roi_gardrops - a.roi_gardrops,
        profit_desc: (a, b) => b.net_profit_gardrops - a.net_profit_gardrops,
        discount_desc: (a, b) => b.total_discount_rate - a.total_discount_rate,
        buy_asc: (a, b) => a.net_basket_price - b.net_basket_price,
        score_desc: (a, b) => b.arbitrage_score - a.arbitrage_score,
      };
      list.sort(sorters[sortBy] || sorters.roi_desc);
      renderedItems = list;
      visibleCount = PAGE_SIZE;
      document.getElementById('filteredCountBadge').textContent = `${list.length} ürün`;
      renderGrid();
    }

    function showMore() { visibleCount += PAGE_SIZE; renderGrid(); }

    function renderGrid() {
      const grid = document.getElementById('itemsGrid');
      const moreBtn = document.getElementById('loadMoreBtn');
      if (!renderedItems.length) {
        grid.innerHTML = `<div class="col-span-full bg-slate-900 border border-slate-800 rounded-2xl p-10 text-center text-slate-400 space-y-2">
            <div class="text-3xl">🔍</div><div class="text-base font-bold text-white">Filtrelere uygun ürün yok</div>
            <p class="text-xs">"Sadece kârlı" kutusunu kaldırarak tüm kataloğu görebilirsiniz.</p></div>`;
        moreBtn.classList.add('hidden');
        return;
      }
      grid.innerHTML = renderedItems.slice(0, visibleCount).map((it, idx) => {
        const sizesHtml = (it.sizes && it.sizes.length)
          ? it.sizes.map(s => `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono text-[10px] border border-slate-700">${esc(s)}</span>`).join('')
          : '<span class="text-slate-500 italic text-[11px]">Referans katalog (hedef beden yok)</span>';
        const profitCls = it.net_profit_gardrops >= 0 ? 'text-emerald-300' : 'text-rose-300';
        return `
          <article class="bg-slate-900/90 border ${it.is_profitable ? 'border-emerald-600/70' : 'border-slate-800'} rounded-2xl overflow-hidden flex flex-col justify-between">
            <div>
              <div class="flex items-center justify-between px-3.5 py-2 bg-slate-800/80 border-b border-slate-800 text-xs">
                <span class="font-bold text-cyan-300 truncate">${esc(it.site_name)} • ${esc(it.gender)}</span>
                <span class="font-extrabold text-amber-300 bg-amber-950/80 px-2 py-0.5 rounded-full border border-amber-800/80 text-[11px] shrink-0">⭐ ${esc(it.arbitrage_score)}</span>
              </div>
              <div class="p-3.5 space-y-2.5">
                <div class="flex items-center justify-between text-xs gap-2">
                  <button data-copy-sku="${idx}" class="font-mono text-slate-400 hover:text-white bg-slate-800/60 px-2 py-0.5 rounded border border-slate-700/60 truncate">SKU: ${esc(it.sku)} 📋</button>
                  <span class="text-emerald-400 font-bold bg-emerald-950/60 px-2 py-0.5 rounded border border-emerald-800/60 shrink-0">%${esc(it.total_discount_rate)}</span>
                </div>
                <h3 data-open="${idx}" class="font-extrabold text-white text-sm line-clamp-2 leading-snug cursor-pointer hover:text-emerald-300">${esc(it.brand)} - ${esc(it.title)}</h3>
                <div class="text-[11px] font-semibold text-emerald-300">${esc(it.price_trend_badge)}</div>
                <div class="grid grid-cols-2 gap-2 bg-slate-950/80 p-2.5 rounded-xl border border-slate-800 text-xs">
                  <div><div class="text-slate-400 text-[11px]">Sepette Alış</div><div class="text-base font-extrabold text-emerald-400">${tl(it.net_basket_price)}</div></div>
                  <div><div class="text-slate-400 text-[11px]">Piyasa Dip</div><div class="text-base font-bold text-rose-400">~${tl(it.market_floor_price)}</div></div>
                </div>
                <div class="bg-gradient-to-r from-emerald-950/50 to-slate-950/70 border border-emerald-800/60 rounded-xl p-2.5 text-xs space-y-1">
                  <div class="flex justify-between"><span class="text-slate-300">🟢 Gardrops:</span><span class="font-extrabold ${profitCls}">${signedTl(it.net_profit_gardrops)} (%${esc(it.roi_gardrops)})</span></div>
                  <div class="flex justify-between text-[11px]"><span class="text-slate-400">🟣 Dolap:</span><span class="font-bold text-cyan-300">${signedTl(it.net_profit_dolap)} (%${esc(it.roi_dolap)})</span></div>
                </div>
                <div class="flex flex-wrap gap-1">${sizesHtml}</div>
              </div>
            </div>
            <div class="p-2.5 bg-slate-950/80 border-t border-slate-800 flex items-center gap-1.5 text-xs font-bold">
              <a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 text-white py-2 rounded-lg">🛒 Sepet</a>
              <a href="${esc(safeUrl(it.akakce_url))}" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-slate-800 hover:bg-slate-700 py-2 rounded-lg border border-slate-700">📊 Akakçe</a>
              <button data-open="${idx}" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-lg">📋 Detay</button>
            </div>
          </article>`;
      }).join('');
      grid.querySelectorAll('[data-open]').forEach(el => el.addEventListener('click', () => openModal(Number(el.dataset.open))));
      grid.querySelectorAll('[data-copy-sku]').forEach(el => el.addEventListener('click', () => {
        const it = renderedItems[Number(el.dataset.copySku)];
        if (it) copyText(it.sku, `SKU kopyalandı: ${it.sku}`);
      }));
      moreBtn.classList.toggle('hidden', visibleCount >= renderedItems.length);
    }

    async function fetchState() {
      // 1) Yerel sunucu (dashboard.py) canlı API  2) GitHub Pages bulut anlık görüntüsü (data.json)
      const sources = [
        { url: 'api/state', mode: 'local' },
        { url: 'data.json?t=' + Date.now(), mode: 'cloud' },
      ];
      for (const src of sources) {
        try {
          const res = await fetch(src.url, { cache: 'no-store' });
          const ct = res.headers.get('content-type') || '';
          if (res.ok && ct.includes('json')) return { data: await res.json(), mode: src.mode };
        } catch (e) { /* sonraki kaynağı dene */ }
      }
      throw new Error('Veri kaynağına ulaşılamadı');
    }

    function renderCharts() {
      const chartOpts = { plugins: { legend: { display: false } }, scales: {
        y: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8', font: { size: 10 } } },
        x: { grid: { display: false }, ticks: { color: '#94a3b8', font: { size: 10 } } } } };
      if (storeChartInst) storeChartInst.destroy();
      storeChartInst = new Chart(document.getElementById('storeChart'), { type: 'bar', options: chartOpts,
        data: { labels: Object.keys(rawData.summary.store_counts), datasets: [{ data: Object.values(rawData.summary.store_counts), backgroundColor: '#10b981', borderRadius: 4 }] } });
      const b = Object.entries(rawData.summary.brand_counts).sort((x, y) => y[1] - x[1]).slice(0, 8);
      if (brandChartInst) brandChartInst.destroy();
      brandChartInst = new Chart(document.getElementById('brandChart'), { type: 'bar', options: chartOpts,
        data: { labels: b.map(x => x[0]), datasets: [{ data: b.map(x => x[1]), backgroundColor: '#38bdf8', borderRadius: 4 }] } });
    }

    async function loadData(manual) {
      try {
        const { data, mode } = await fetchState();
        rawData = data;
        const badge = document.getElementById('modeBadge');
        badge.textContent = mode === 'local' ? '🖥️ Yerel Canlı Mod' : '☁️ Bulut Modu';
        badge.className = 'px-2 py-0.5 rounded-full border ' + (mode === 'local' ? 'bg-cyan-950 text-cyan-300 border-cyan-800' : 'bg-indigo-950 text-indigo-300 border-indigo-800');
        const ts = (rawData.meta.last_updated_at || rawData.generated_at || '');
        document.getElementById('lastUpdated').textContent = ts ? 'Son tarama: ' + new Date(ts).toLocaleString('tr-TR') : '';

        const prof = rawData.items.filter(x => x.is_profitable);
        document.getElementById('kpiSkus').textContent = rawData.summary.unique_skus.toLocaleString('tr-TR');
        document.getElementById('kpiProfitable').textContent = rawData.summary.profitable_count.toLocaleString('tr-TR');
        document.getElementById('kpiThresholds').textContent = `Min ${rawData.thresholds.min_net_profit_tl}₺ / %${rawData.thresholds.min_roi_percent} ROI`;
        document.getElementById('kpiMaxProfit').textContent = signedTl(prof.reduce((m, x) => Math.max(m, x.net_profit_gardrops || 0), 0));
        document.getElementById('kpiAvgRoi').textContent = prof.length ? '%' + (prof.reduce((s, x) => s + x.roi_gardrops, 0) / prof.length).toFixed(1) : '-';

        const net = rawData.network || {};
        const pub = net.public_url || '';
        document.getElementById('publicUrlBlock').classList.toggle('hidden', !pub);
        const pubEl = document.getElementById('publicUrlDisplay');
        pubEl.textContent = pub; pubEl.href = safeUrl(pub);
        document.getElementById('lanUrlBlock').classList.toggle('hidden', !(mode === 'local' && net.lan_url));
        document.getElementById('lanUrlDisplay').textContent = net.lan_url || '';

        const storeSel = document.getElementById('storeFilter');
        const prev = storeSel.value;
        storeSel.innerHTML = '<option value="">🏪 Tüm Mağazalar</option>' + Object.keys(rawData.summary.store_counts).sort()
          .map(s => `<option value="${esc(s)}">${esc(s)} (${rawData.summary.store_counts[s]})</option>`).join('');
        storeSel.value = prev;

        renderCharts();
        runSim();
        filterAndRender();
        if (manual) showToast('Veriler güncellendi!');
      } catch (e) {
        document.getElementById('itemsGrid').innerHTML = `<div class="col-span-full bg-rose-950/40 border border-rose-800 rounded-2xl p-8 text-center text-rose-200 text-sm">Veriler yüklenemedi. Bulut panel ilk kez kuruluyorsa birkaç dakika sonra tekrar deneyin.</div>`;
      }
    }

    renderSizeChips();
    loadData(false);
  </script>
</body>
</html>
"""


def export_static_site(out_dir: Path) -> Path:
    """
    GitHub Pages ile her yerden (mobil veri dahil) açılabilen statik bulut paneli üretir:
    `docs/index.html` + `docs/data.json` + `docs/.nojekyll`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload(compact=True)
    (out_dir / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (out_dir / "index.html").write_text(DASHBOARD_HTML, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return out_dir


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/api/state", "/data.json"):
            body = json.dumps(build_dashboard_payload(), ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return
        self._send_bytes(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="E-Ticaret Arbitraj Görsel Web Kontrol Paneli")
    parser.add_argument("--host", default="0.0.0.0", help="Sunucu dinleme adresi (varsayılan: 0.0.0.0 tüm ağ)")
    parser.add_argument("--port", type=int, default=config.LOCAL_DASHBOARD_PORT, help="Sunucu portu")
    parser.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    parser.add_argument(
        "--export-static",
        metavar="KLASOR",
        default=None,
        help="GitHub Pages bulut paneli için statik site üret (Örn: --export-static docs)",
    )
    args = parser.parse_args()

    if args.export_static:
        out = export_static_site(Path(args.export_static))
        size_kb = (out / "data.json").stat().st_size / 1024
        print(f"☁️ Bulut paneli üretildi: {out.resolve()} (data.json: {size_kb:.0f} KB)")
        if config.PUBLIC_DASHBOARD_URL:
            print(f"🌍 Yayın adresi: {config.PUBLIC_DASHBOARD_URL}")
        return

    lan_ip = get_local_network_ip()
    local_url = f"http://localhost:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), DashboardRequestHandler)
    print("=" * 76)
    print("🚀 ARBİTRAJ WEB KONTROL PANELİ ÇALIŞIYOR:")
    print(f"   🖥️  Bu bilgisayar      : {local_url}")
    print(f"   🏠  Aynı Wi-Fi telefon : http://{lan_ip}:{args.port}")
    if config.PUBLIC_DASHBOARD_URL:
        print(f"   ☁️  Her yerden (bulut) : {config.PUBLIC_DASHBOARD_URL}")
    print("=" * 76)

    if not args.no_browser:
        webbrowser.open(local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb paneli kapatıldı.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
