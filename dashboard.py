import argparse
import json
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

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
    except Exception:
        return "127.0.0.1"


def build_dashboard_payload() -> dict[str, Any]:
    """`data/products_state.json` verisini okuyup tüm ürünler için kâr/ROI ve ilan şablonu üretir."""
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
            listing_tpl = ListingGenerator.generate(analysis)

            if analysis.is_profitable_opportunity and len(prod.sizes) > 0:
                profitable_count += 1

            items_list.append(
                {
                    "sku": d_sku,
                    "canonical_sku": c_sku,
                    "site_key": s_key,
                    "site_name": s_name,
                    "brand": brand,
                    "title": title,
                    "gender": gender,
                    "url": prod.url,
                    "akakce_url": prod.akakce_search_url,
                    "image_url": prod.image_url,
                    "retail_price": round(prod.retail_price),
                    "sale_price": round(prod.sale_price),
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
                    "is_profitable": bool(analysis.is_profitable_opportunity and len(prod.sizes) > 0),
                    "price_trend_badge": analysis.price_trend_badge,
                    "is_inflated_retail": analysis.is_inflated_retail_price,
                    "price_history": s_data.get("price_history") or [],
                    "listing_title": listing_tpl.title,
                    "listing_desc": listing_tpl.description,
                    "listing_price": listing_tpl.recommended_price_tl,
                    "listing_min_price": listing_tpl.min_acceptable_price_tl,
                }
            )

    items_list.sort(key=lambda x: (x["is_profitable"], x["roi_gardrops"], x["net_profit_gardrops"]), reverse=True)

    lan_ip = get_local_network_ip()
    return {
        "meta": storage.state.get("meta") or {},
        "network": {
            "local_url": "http://localhost:8080",
            "lan_url": f"http://{lan_ip}:8080",
            "lan_ip": lan_ip,
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Türkiye E-Ticaret Arbitraj & Flipping Radarı — Kontrol Paneli</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: #0f172a; }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #475569; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans selection:bg-emerald-500 selection:text-white">
  <!-- Toast Bildirim -->
  <div id="toast" class="fixed bottom-5 right-5 z-50 transform translate-y-20 opacity-0 transition-all duration-300 bg-emerald-600 text-white text-sm font-semibold px-4 py-3 rounded-xl shadow-2xl flex items-center gap-2 border border-emerald-400">
    <span>✅</span><span id="toastMsg">İşlem başarılı!</span>
  </div>

  <!-- Header -->
  <header class="border-b border-slate-800 bg-slate-900/90 backdrop-blur sticky top-0 z-30">
    <div class="max-w-7xl mx-auto px-4 py-3.5 flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-600 to-cyan-500 flex items-center justify-center text-xl shadow-lg shadow-emerald-500/20">
          👟
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-xl font-extrabold bg-gradient-to-r from-emerald-400 via-cyan-300 to-indigo-400 bg-clip-text text-transparent">
              Türkiye E-Ticaret Arbitraj Radarı
            </h1>
            <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-700">7 Yetkili Mağaza + Akakçe</span>
          </div>
          <p class="text-xs text-slate-400">Orijinal Distribütör İndirimleri & 2. El Flipping Kâr Analizi</p>
        </div>
      </div>

      <div class="flex items-center flex-wrap gap-2.5 text-xs">
        <button onclick="openPhoneModal()" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-2 rounded-lg flex items-center gap-1.5 transition">
          📱 <span>Telefonda Aç (Wi-Fi)</span>
        </button>
        <button onclick="exportCSV()" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-2 rounded-lg flex items-center gap-1.5 transition">
          📥 <span>Excel / CSV İndir</span>
        </button>
        <button onclick="loadData()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-3.5 py-2 rounded-lg flex items-center gap-1.5 shadow-md shadow-emerald-600/20 transition">
          🔄 <span>Yenile</span>
        </button>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-6 space-y-6">
    <!-- KPI İstatistik Kartları -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-3.5">
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 shadow-sm">
        <div class="text-xs font-medium text-slate-400">İndekslenen Model (SKU)</div>
        <div id="kpiSkus" class="text-2xl lg:text-3xl font-extrabold text-white mt-1">-</div>
        <div class="text-[11px] text-slate-500 mt-1">7 Yetkili Distribütör Kataloğu</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 shadow-sm">
        <div class="text-xs font-medium text-slate-400">Kârlı Arbitraj Fırsatı</div>
        <div id="kpiProfitable" class="text-2xl lg:text-3xl font-extrabold text-emerald-400 mt-1">-</div>
        <div id="kpiThresholds" class="text-[11px] text-emerald-500/80 mt-1">Eşikler Kontrol Edildi</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 shadow-sm">
        <div class="text-xs font-medium text-slate-400">En Yüksek Net Kâr (Gardrops)</div>
        <div id="kpiMaxProfit" class="text-2xl lg:text-3xl font-extrabold text-cyan-400 mt-1">-</div>
        <div class="text-[11px] text-cyan-500/80 mt-1">%6 Hizmet + 95₺ Kargo Düşülmüş</div>
      </div>
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 shadow-sm">
        <div class="text-xs font-medium text-slate-400">Son Tarama Zamanı</div>
        <div id="lastUpdated" class="text-sm font-bold text-slate-200 mt-2">Yükleniyor...</div>
        <div class="text-[11px] text-slate-500 mt-1">15 dakikada bir otomatik güncellenir</div>
      </div>
    </section>

    <!-- Grafikler & Hızlı Kâr Hesaplayıcı -->
    <section class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 lg:col-span-1">
        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">🏪 Mağaza Ürün Dağılımı</h2>
        <canvas id="storeChart" height="170"></canvas>
      </div>

      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 lg:col-span-1">
        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">👟 En Çok Bulunan Markalar</h2>
        <canvas id="brandChart" height="170"></canvas>
      </div>

      <!-- Canlı Kâr Simülatörü -->
      <div class="bg-gradient-to-br from-slate-900 to-slate-900/60 border border-slate-800 rounded-xl p-4 lg:col-span-1 flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between mb-2">
            <h2 class="text-xs font-bold uppercase tracking-wider text-emerald-400">🧮 Canlı Kâr / ROI Simülatörü</h2>
            <span class="text-[10px] text-slate-400">2026 Komisyonları</span>
          </div>
          <p class="text-xs text-slate-400 mb-3">Aklınızdaki herhangi bir ayakkabının alış ve piyasa fiyatını yazarak net kârı görün:</p>
          <div class="grid grid-cols-2 gap-2.5">
            <div>
              <label class="text-[11px] text-slate-400 block mb-1">Sepette Alış (TL):</label>
              <input id="simBuy" type="number" value="2800" oninput="runSim()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-sm font-bold text-emerald-400 focus:outline-none focus:border-emerald-500" />
            </div>
            <div>
              <label class="text-[11px] text-slate-400 block mb-1">Piyasa En Ucuz (TL):</label>
              <input id="simFloor" type="number" value="4900" oninput="runSim()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-sm font-bold text-rose-400 focus:outline-none focus:border-rose-500" />
            </div>
          </div>
        </div>

        <div class="bg-slate-950/80 border border-slate-800 rounded-xl p-3 mt-3 text-xs space-y-1.5">
          <div class="flex justify-between text-slate-300">
            <span>Önerilen Satış (-%12 İskonto):</span>
            <span id="simSell" class="font-bold text-white">4.312 TL</span>
          </div>
          <div class="flex justify-between text-emerald-300 font-bold border-t border-slate-800/80 pt-1.5">
            <span>🟢 Gardrops / Sahibinden Net:</span>
            <span id="simGardrops">+1.158 TL (%41.4 ROI)</span>
          </div>
          <div class="flex justify-between text-cyan-300 font-bold">
            <span>🟣 Dolap Net (%15 + 95₺):</span>
            <span id="simDolap">+770 TL (%27.5 ROI)</span>
          </div>
        </div>
      </div>
    </section>

    <!-- Filtreleme & Arama Araç Çubuğu -->
    <section class="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
        <!-- Arama -->
        <div class="md:col-span-2 relative">
          <input id="searchInput" type="text" placeholder="🔍 Model adı, Marka veya SKU ara (Örn: IF6491, Handball Spezial, Nike, 530)..." oninput="filterAndRender()" class="w-full bg-slate-800 border border-slate-700 rounded-lg pl-3 pr-8 py-2 text-sm text-slate-100 placeholder-slate-400 focus:outline-none focus:border-emerald-500" />
          <button onclick="document.getElementById('searchInput').value=''; filterAndRender();" class="absolute right-2.5 top-2.5 text-slate-400 hover:text-white text-xs">✕</button>
        </div>

        <!-- Mağaza Seçimi -->
        <div>
          <select id="storeFilter" onchange="filterAndRender()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-emerald-500">
            <option value="">🏪 Tüm Yetkili Mağazalar</option>
          </select>
        </div>

        <!-- Sıralama -->
        <div>
          <select id="sortFilter" onchange="filterAndRender()" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-emerald-500 font-semibold">
            <option value="roi_desc">🚀 Sırala: En Yüksek ROI (%)</option>
            <option value="profit_desc">💰 Sırala: En Yüksek Net Kâr (TL)</option>
            <option value="discount_desc">🏷️ Sırala: En Yüksek İndirim (%)</option>
            <option value="buy_asc">📉 Sırala: En Düşük Alış Fiyatı</option>
            <option value="score_desc">⭐ Sırala: Arbitraj Puanı (1-100)</option>
          </select>
        </div>
      </div>

      <!-- Alt Satır: Beden Butonları & Hızlı Switchler -->
      <div class="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-slate-800/80 text-xs">
        <div class="flex items-center flex-wrap gap-1.5">
          <span class="text-slate-400 font-semibold mr-1">👟 Altın Beden:</span>
          <button onclick="setSizeFilter('')" id="sizeBtnAll" class="size-btn px-2.5 py-1 rounded-md bg-emerald-600 text-white font-bold border border-emerald-500">Tümü</button>
          <button onclick="setSizeFilter('42')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">42</button>
          <button onclick="setSizeFilter('42.5')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">42.5</button>
          <button onclick="setSizeFilter('43')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">43</button>
          <button onclick="setSizeFilter('44')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">44</button>
          <button onclick="setSizeFilter('38')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">38</button>
          <button onclick="setSizeFilter('38.5')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">38.5</button>
          <button onclick="setSizeFilter('40')" class="size-btn px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">40</button>
        </div>

        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2 cursor-pointer select-none">
            <input id="onlyProfitable" type="checkbox" checked onchange="filterAndRender()" class="w-4 h-4 accent-emerald-500 rounded cursor-pointer" />
            <span class="text-slate-300 font-semibold">Sadece Kârlı Fırsatları Göster</span>
          </label>
          <span id="filteredCountBadge" class="px-2.5 py-1 bg-slate-800 text-emerald-400 font-bold rounded-lg border border-slate-700">0 Fırsat</span>
        </div>
      </div>
    </section>

    <!-- Ürün Kartları Grid -->
    <section id="itemsGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></section>
  </main>

  <!-- Detay & İlan Şablonu Modalı -->
  <div id="detailModal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-2xl w-full p-6 space-y-4 shadow-2xl overflow-y-auto max-h-[90vh] custom-scrollbar">
      <div class="flex items-start justify-between">
        <div>
          <span id="modalStore" class="text-xs font-bold text-cyan-400 bg-cyan-950/80 px-2.5 py-0.5 rounded-full border border-cyan-800">Mağaza</span>
          <h2 id="modalTitle" class="text-lg font-bold text-white mt-1.5">Ürün Başlığı</h2>
          <div id="modalSku" class="text-xs font-mono text-slate-400">SKU: -</div>
        </div>
        <button onclick="closeModal()" class="text-slate-400 hover:text-white text-xl px-2">✕</button>
      </div>

      <!-- Fiyat Kıyas Özeti -->
      <div class="grid grid-cols-3 gap-2 bg-slate-950 p-3 rounded-xl border border-slate-800 text-center text-xs">
        <div>
          <div class="text-slate-400">Sepette Alış</div>
          <div id="modalBuyPrice" class="text-base font-extrabold text-emerald-400 mt-0.5">0 TL</div>
        </div>
        <div>
          <div class="text-slate-400">Piyasa Dip</div>
          <div id="modalFloorPrice" class="text-base font-bold text-rose-400 mt-0.5">0 TL</div>
        </div>
        <div>
          <div class="text-slate-400">Önerilen Satış</div>
          <div id="modalSellPrice" class="text-base font-bold text-cyan-400 mt-0.5">0 TL</div>
        </div>
      </div>

      <!-- Net Kâr Tablosu -->
      <div class="bg-emerald-950/30 border border-emerald-800/60 rounded-xl p-3 text-xs space-y-1.5">
        <div class="flex justify-between">
          <span class="text-slate-300">🟢 Gardrops / Sahibinden (Param Güvende %6 + 95₺):</span>
          <span id="modalGardropsNet" class="font-bold text-emerald-300">+0 TL (%0 ROI)</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-300">🟣 Dolap Uygulaması (%14 Komisyon + %1 Stopaj + 95₺):</span>
          <span id="modalDolapNet" class="font-bold text-cyan-300">+0 TL (%0 ROI)</span>
        </div>
      </div>

      <!-- Hazır İlan Şablonu Kopyalama Alanı -->
      <div class="space-y-3 pt-2 border-t border-slate-800">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold uppercase text-slate-300">📋 Hazır İlan Şablonu (Gardrops / Dolap / Sahibinden)</span>
          <button onclick="copyModalFullTemplate()" class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg transition">
            Tümünü Kopyala
          </button>
        </div>

        <div>
          <label class="text-[11px] text-slate-400 block mb-1">İlan Başlığı (Tek Tıkla Kopyala):</label>
          <div class="flex gap-2">
            <input id="modalListingTitle" type="text" readonly class="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs font-mono text-slate-200" />
            <button onclick="copyInputValue('modalListingTitle')" class="bg-slate-800 hover:bg-slate-700 px-3 py-1 text-xs rounded-lg border border-slate-700">Kopyala</button>
          </div>
        </div>

        <div>
          <label class="text-[11px] text-slate-400 block mb-1">İlan Açıklaması:</label>
          <textarea id="modalListingDesc" rows="5" readonly class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-xs font-mono text-slate-300 custom-scrollbar"></textarea>
        </div>
      </div>

      <div class="flex gap-2 pt-2 border-t border-slate-800 text-xs font-semibold">
        <a id="modalBuyLink" href="#" target="_blank" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 text-white py-2.5 rounded-xl transition">
          🛒 Mağazada Aç / Sepete Ekle
        </a>
        <a id="modalAkakceLink" href="#" target="_blank" class="flex-1 text-center bg-slate-800 hover:bg-slate-700 text-slate-200 py-2.5 rounded-xl transition">
          📊 Akakçe'de Piyasa Doğrula
        </a>
      </div>
    </div>
  </div>

  <!-- Mobil / Wi-Fi QR & Link Modalı -->
  <div id="phoneModal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl text-center">
      <div class="w-12 h-12 rounded-full bg-emerald-500/10 text-emerald-400 flex items-center justify-center text-2xl mx-auto">
        📱
      </div>
      <h2 class="text-lg font-bold text-white">Telefondan Web Paneline Bağlan</h2>
      <p class="text-xs text-slate-400">
        Telefonunuz aynı Wi-Fi ağına bağlıyken tarayıcınızın adres çubuğuna aşağıdaki adresi yazarak kontrol panelini açabilirsiniz:
      </p>
      <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 flex items-center justify-between">
        <span id="lanUrlDisplay" class="font-mono text-emerald-400 text-sm select-all">http://127.0.0.1:8080</span>
        <button onclick="copyLanUrl()" class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Kopyala</button>
      </div>
      <p class="text-[11px] text-slate-500">
        Telegram botuna herhangi bir mesaj yazdığınızda da bu link otomatik olarak sohbetinize gönderilir.
      </p>
      <button onclick="closePhoneModal()" class="w-full bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-bold py-2.5 rounded-xl">
        Kapat
      </button>
    </div>
  </div>

  <script>
    let rawData = null;
    let selectedSize = '';
    let currentModalItem = null;
    let storeChartInst = null;
    let brandChartInst = null;

    function showToast(msg) {
      const el = document.getElementById('toast');
      document.getElementById('toastMsg').textContent = msg;
      el.classList.remove('translate-y-20', 'opacity-0');
      setTimeout(() => el.classList.add('translate-y-20', 'opacity-0'), 2400);
    }

    function copyToClipboard(text, successMsg) {
      navigator.clipboard.writeText(text);
      showToast(successMsg || 'Panoya kopyalandı!');
    }

    function copyInputValue(id) {
      const val = document.getElementById(id).value;
      copyToClipboard(val, 'Kopyalandı!');
    }

    function openPhoneModal() {
      document.getElementById('phoneModal').classList.remove('hidden');
      document.getElementById('phoneModal').classList.add('flex');
    }

    function closePhoneModal() {
      document.getElementById('phoneModal').classList.add('hidden');
      document.getElementById('phoneModal').classList.remove('flex');
    }

    function copyLanUrl() {
      const url = document.getElementById('lanUrlDisplay').textContent;
      copyToClipboard(url, 'Link kopyalandı! Tarayıcınızda açabilirsiniz.');
    }

    function setSizeFilter(sz) {
      selectedSize = sz;
      document.querySelectorAll('.size-btn').forEach(btn => {
        btn.classList.remove('bg-emerald-600', 'text-white', 'font-bold', 'border-emerald-500');
        btn.classList.add('bg-slate-800', 'text-slate-300', 'border-slate-700');
      });
      event.target.classList.remove('bg-slate-800', 'text-slate-300', 'border-slate-700');
      event.target.classList.add('bg-emerald-600', 'text-white', 'font-bold', 'border-emerald-500');
      filterAndRender();
    }

    function runSim() {
      const buy = parseFloat(document.getElementById('simBuy').value || 0);
      const floor = parseFloat(document.getElementById('simFloor').value || 0);
      if (buy <= 0 || floor <= 0) return;
      const sell = Math.round(floor * 0.88);
      const netG = Math.round(sell * 0.94 - 95 - buy);
      const roiG = ((netG / buy) * 100).toFixed(1);
      const netD = Math.round(sell * 0.85 - 95 - buy);
      const roiD = ((netD / buy) * 100).toFixed(1);
      document.getElementById('simSell').textContent = sell.toLocaleString('tr-TR') + ' TL';
      document.getElementById('simGardrops').textContent = `+${netG.toLocaleString('tr-TR')} TL (%${roiG} ROI)`;
      document.getElementById('simDolap').textContent = `+${netD.toLocaleString('tr-TR')} TL (%${roiD} ROI)`;
    }

    function openModal(idx) {
      const item = window._renderedItems[idx];
      if (!item) return;
      currentModalItem = item;
      document.getElementById('modalStore').textContent = `${item.site_name} • ${item.gender}`;
      document.getElementById('modalTitle').textContent = `${item.brand} - ${item.title}`;
      document.getElementById('modalSku').textContent = `Model Kodu (SKU): ${item.sku} | Kampanya: ${item.campaign_name}`;
      document.getElementById('modalBuyPrice').textContent = `${item.net_basket_price.toLocaleString('tr-TR')} TL`;
      document.getElementById('modalFloorPrice').textContent = `~${item.market_floor_price.toLocaleString('tr-TR')} TL`;
      document.getElementById('modalSellPrice').textContent = `${item.suggested_sell_price.toLocaleString('tr-TR')} TL`;
      document.getElementById('modalGardropsNet').textContent = `+${item.net_profit_gardrops.toLocaleString('tr-TR')} TL (%${item.roi_gardrops} ROI)`;
      document.getElementById('modalDolapNet').textContent = `+${item.net_profit_dolap.toLocaleString('tr-TR')} TL (%${item.roi_dolap} ROI)`;
      document.getElementById('modalListingTitle').value = item.listing_title;
      document.getElementById('modalListingDesc').value = item.listing_desc;
      document.getElementById('modalBuyLink').href = item.url;
      document.getElementById('modalAkakceLink').href = item.akakce_url;

      document.getElementById('detailModal').classList.remove('hidden');
      document.getElementById('detailModal').classList.add('flex');
    }

    function closeModal() {
      document.getElementById('detailModal').classList.add('hidden');
      document.getElementById('detailModal').classList.remove('flex');
    }

    function copyModalFullTemplate() {
      if (!currentModalItem) return;
      const full = `${currentModalItem.listing_title}\\nFiyat: ${currentModalItem.listing_price} TL (Min Pazarlık: ${currentModalItem.listing_min_price} TL)\\n\\n${currentModalItem.listing_desc}`;
      copyToClipboard(full, 'Tam İlan Şablonu panoya kopyalandı!');
    }

    function exportCSV() {
      if (!window._renderedItems || window._renderedItems.length === 0) {
        showToast('İndirilecek ürün bulunamadı.');
        return;
      }
      const headers = ["SKU", "Marka", "Model", "Cinsiyet", "Magaza", "Sepette_Alis_TL", "Piyasa_Dip_TL", "Onerilen_Satis_TL", "Gardrops_Net_TL", "Gardrops_ROI_Pct", "Dolap_Net_TL", "Dolap_ROI_Pct", "Numaralar", "Magaza_URL", "Akakce_URL"];
      const rows = window._renderedItems.map(it => [
        `"${it.sku}"`,
        `"${it.brand}"`,
        `"${it.title.replace(/"/g, '""')}"`,
        `"${it.gender}"`,
        `"${it.site_name}"`,
        it.net_basket_price,
        it.market_floor_price,
        it.suggested_sell_price,
        it.net_profit_gardrops,
        it.roi_gardrops,
        it.net_profit_dolap,
        it.roi_dolap,
        `"${it.sizes.join(';')}"`,
        `"${it.url}"`,
        `"${it.akakce_url}"`
      ]);

      const csvContent = "\\uFEFF" + [headers.join(","), ...rows.map(e => e.join(","))].join("\\n");
      const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.setAttribute("href", url);
      link.setAttribute("download", `arbitraj_firsatlari_${new Date().toISOString().slice(0, 10)}.csv`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      showToast('Excel/CSV dosyası başarıyla indirildi!');
    }

    function filterAndRender() {
      if (!rawData) return;
      const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
      const store = document.getElementById('storeFilter').value;
      const sortBy = document.getElementById('sortFilter').value;
      const onlyProf = document.getElementById('onlyProfitable').checked;

      let list = rawData.items.filter(it => {
        if (onlyProf && !it.is_profitable) return false;
        if (store && it.site_name !== store) return false;
        if (selectedSize && (!it.sizes || !it.sizes.includes(selectedSize))) return false;
        if (q) {
          const matchStr = `${it.brand} ${it.title} ${it.sku} ${it.site_name}`.toLowerCase();
          if (!matchStr.includes(q)) return false;
        }
        return true;
      });

      // Sıralama
      if (sortBy === 'roi_desc') list.sort((a,b) => b.roi_gardrops - a.roi_gardrops);
      else if (sortBy === 'profit_desc') list.sort((a,b) => b.net_profit_gardrops - a.net_profit_gardrops);
      else if (sortBy === 'discount_desc') list.sort((a,b) => b.total_discount_rate - a.total_discount_rate);
      else if (sortBy === 'buy_asc') list.sort((a,b) => a.net_basket_price - b.net_basket_price);
      else if (sortBy === 'score_desc') list.sort((a,b) => b.arbitrage_score - a.arbitrage_score);

      window._renderedItems = list;
      document.getElementById('filteredCountBadge').textContent = `${list.length} Fırsat Listeleniyor`;

      const grid = document.getElementById('itemsGrid');
      if (list.length === 0) {
        grid.innerHTML = `
          <div class="col-span-full bg-slate-900 border border-slate-800 rounded-2xl p-10 text-center text-slate-400 space-y-2">
            <div class="text-3xl">🔍</div>
            <div class="text-base font-bold text-white">Filtrelere uygun fırsat bulunamadı</div>
            <p class="text-xs">Arama kelimesini değiştirebilir veya "Sadece Kârlı Fırsatları Göster" kutucuğunun işaretini kaldırarak tüm kataloğu inceleyebilirsiniz.</p>
          </div>
        `;
        return;
      }

      grid.innerHTML = list.slice(0, 90).map((it, idx) => {
        const sizesHtml = it.sizes && it.sizes.length
          ? it.sizes.map(s => `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono text-[10px] border border-slate-700">${s}</span>`).join('')
          : '<span class="text-slate-500 italic text-[11px]">Referans Katalog</span>';

        return `
          <div class="bg-slate-900/90 border ${it.is_profitable ? 'border-emerald-600/70 hover:border-emerald-500' : 'border-slate-800 hover:border-slate-700'} rounded-2xl overflow-hidden flex flex-col justify-between transition-all duration-200 shadow-lg hover:shadow-emerald-950/20">
            <div>
              <!-- Üst Mağaza Barı -->
              <div class="flex items-center justify-between px-4 py-2.5 bg-slate-800/80 border-b border-slate-800 text-xs">
                <span class="font-bold text-cyan-300 flex items-center gap-1.5">
                  <span class="w-2 h-2 rounded-full bg-cyan-400"></span>
                  ${it.site_name} • ${it.gender}
                </span>
                <span class="font-extrabold text-amber-300 bg-amber-950/80 px-2 py-0.5 rounded-full border border-amber-800/80 text-[11px]">
                  ⭐ ${it.arbitrage_score}/100
                </span>
              </div>

              <!-- Kart Gövdesi -->
              <div class="p-4 space-y-2.5">
                <div class="flex items-center justify-between text-xs">
                  <button onclick="copyToClipboard('${it.sku}', 'SKU Kopyalandı: ${it.sku}')" class="font-mono text-slate-400 hover:text-white bg-slate-800/60 px-2 py-0.5 rounded border border-slate-700/60" title="Kopyala">
                    SKU: ${it.sku} 📋
                  </button>
                  <span class="text-emerald-400 font-bold bg-emerald-950/60 px-2 py-0.5 rounded border border-emerald-800/60">
                    %${it.total_discount_rate} İndirim
                  </span>
                </div>

                <h3 class="font-extrabold text-white text-sm line-clamp-2 leading-snug hover:text-emerald-300 transition cursor-pointer" onclick="openModal(${idx})">
                  ${it.brand} - ${it.title}
                </h3>

                <div class="text-[11px] font-semibold text-emerald-300 flex items-center gap-1">
                  ${it.price_trend_badge}
                </div>

                <!-- Fiyat Kıyas Karesi -->
                <div class="grid grid-cols-2 gap-2 bg-slate-950/80 p-2.5 rounded-xl border border-slate-800/90 text-xs">
                  <div>
                    <div class="text-slate-400 text-[11px]">Sepette Alış</div>
                    <div class="text-base font-extrabold text-emerald-400">${it.net_basket_price.toLocaleString('tr-TR')} TL</div>
                  </div>
                  <div>
                    <div class="text-slate-400 text-[11px]">Piyasa Dip</div>
                    <div class="text-base font-bold text-rose-400">~${it.market_floor_price.toLocaleString('tr-TR')} TL</div>
                  </div>
                </div>

                <!-- Kâr / ROI Kutusu -->
                <div class="bg-gradient-to-r from-emerald-950/50 to-slate-950/70 border border-emerald-800/60 rounded-xl p-2.5 text-xs space-y-1">
                  <div class="flex justify-between items-center">
                    <span class="text-slate-300">🟢 Gardrops Net:</span>
                    <span class="font-extrabold text-emerald-300">+${it.net_profit_gardrops.toLocaleString('tr-TR')} TL (%${it.roi_gardrops} ROI)</span>
                  </div>
                  <div class="flex justify-between items-center text-slate-400 text-[11px]">
                    <span>🟣 Dolap Net:</span>
                    <span class="font-bold text-cyan-300">+${it.net_profit_dolap.toLocaleString('tr-TR')} TL (%${it.roi_dolap} ROI)</span>
                  </div>
                </div>

                <!-- Numaralar -->
                <div class="space-y-1">
                  <div class="text-[11px] text-slate-400 font-semibold">Numaralar:</div>
                  <div class="flex flex-wrap gap-1">${sizesHtml}</div>
                </div>
              </div>
            </div>

            <!-- Alt Aksiyon Butonları -->
            <div class="p-3 bg-slate-950/80 border-t border-slate-800/80 flex items-center gap-1.5 text-xs font-bold">
              <a href="${it.url}" target="_blank" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 text-white py-2 rounded-lg transition shadow-sm">
                🛒 Sepet
              </a>
              <a href="${it.akakce_url}" target="_blank" class="flex-1 text-center bg-slate-800 hover:bg-slate-700 text-slate-200 py-2 rounded-lg transition border border-slate-700">
                📊 Akakçe
              </a>
              <button onclick="openModal(${idx})" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-lg transition" title="Hazır İlan ve Detay">
                📋 İlan
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    async function loadData() {
      try {
        const res = await fetch('/api/state');
        rawData = await res.json();

        document.getElementById('kpiSkus').textContent = rawData.summary.unique_skus.toLocaleString('tr-TR');
        document.getElementById('kpiProfitable').textContent = rawData.summary.profitable_count.toLocaleString('tr-TR');
        document.getElementById('kpiThresholds').textContent = `Min Kâr: ${rawData.thresholds.min_net_profit_tl}₺ | Min ROI: %${rawData.thresholds.min_roi_percent}`;
        document.getElementById('lastUpdated').textContent = (rawData.meta.last_updated_at || '').slice(0, 19).replace('T', ' ');

        if (rawData.network && rawData.network.lan_url) {
          document.getElementById('lanUrlDisplay').textContent = rawData.network.lan_url;
        }

        const maxProf = rawData.items.reduce((acc, x) => Math.max(acc, x.net_profit_gardrops || 0), 0);
        document.getElementById('kpiMaxProfit').textContent = `+${maxProf.toLocaleString('tr-TR')} TL`;

        // Mağaza Seçenekleri
        const storeSel = document.getElementById('storeFilter');
        const currentVal = storeSel.value;
        const stores = Object.keys(rawData.summary.store_counts || {}).sort();
        storeSel.innerHTML = '<option value="">🏪 Tüm Yetkili Mağazalar</option>' + stores.map(s => `<option value="${s}">${s} (${rawData.summary.store_counts[s]})</option>`).join('');
        storeSel.value = currentVal;

        // Mağaza Grafiği
        if (storeChartInst) storeChartInst.destroy();
        storeChartInst = new Chart(document.getElementById('storeChart'), {
          type: 'bar',
          data: {
            labels: Object.keys(rawData.summary.store_counts),
            datasets: [{ label: 'Ürün', data: Object.values(rawData.summary.store_counts), backgroundColor: '#10b981', borderRadius: 4 }]
          },
          options: {
            plugins: { legend: { display: false } },
            scales: {
              y: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8', font: { size: 10 } } },
              x: { grid: { display: false }, ticks: { color: '#94a3b8', font: { size: 10 } } }
            }
          }
        });

        // Marka Grafiği
        const bEntries = Object.entries(rawData.summary.brand_counts).sort((a,b) => b[1]-a[1]).slice(0, 8);
        if (brandChartInst) brandChartInst.destroy();
        brandChartInst = new Chart(document.getElementById('brandChart'), {
          type: 'bar',
          data: {
            labels: bEntries.map(x=>x[0]),
            datasets: [{ label: 'Model', data: bEntries.map(x=>x[1]), backgroundColor: '#38bdf8', borderRadius: 4 }]
          },
          options: {
            plugins: { legend: { display: false } },
            scales: {
              y: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8', font: { size: 10 } } },
              x: { grid: { display: false }, ticks: { color: '#94a3b8', font: { size: 10 } } }
            }
          }
        });

        runSim();
        filterAndRender();
        showToast('Veriler güncellendi!');
      } catch (e) {
        showToast('Veriler yüklenirken hata oluştu.');
      }
    }

    loadData();
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            payload = build_dashboard_payload()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="E-Ticaret Arbitraj Görsel Web Kontrol Paneli")
    parser.add_argument("--host", default="0.0.0.0", help="Sunucu dinleme adresi (varsayılan: 0.0.0.0 tüm ağ)")
    parser.add_argument("--port", type=int, default=8080, help="Sunucu portu (varsayılan: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    args = parser.parse_args()

    lan_ip = get_local_network_ip()
    local_url = f"http://localhost:{args.port}"
    lan_url = f"http://{lan_ip}:{args.port}"

    server = ThreadingHTTPServer((args.host, args.port), DashboardRequestHandler)
    print("=" * 76)
    print("🚀 ARBİTRAJ VE FLIPPING WEB KONTROL PANELİ ÇALIŞIYOR:")
    print(f"   🖥️  Bilgisayarınız İçin: {local_url}")
    print(f"   📱  Telefonunuz İçin (Wi-Fi): {lan_url}")
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
