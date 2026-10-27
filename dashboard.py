import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.listing_generator import ListingGenerator
from core.parsers import NormalizedSizeInfo
from core.storage import StateStorage


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

            if analysis.is_profitable_opportunity:
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
                    "is_profitable": analysis.is_profitable_opportunity,
                    "price_trend_badge": analysis.price_trend_badge,
                    "is_inflated_retail": analysis.is_inflated_retail_price,
                    "price_history": s_data.get("price_history") or [],
                    "listing_title": listing_tpl.title,
                    "listing_copy_text": f"{listing_tpl.title}\nFiyat: {listing_tpl.recommended_price_tl} TL\n\n{listing_tpl.description}",
                }
            )

    items_list.sort(key=lambda x: (x["is_profitable"], x["net_profit_gardrops"], x["total_discount_rate"]), reverse=True)

    return {
        "meta": storage.state.get("meta") or {},
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
        "items": items_list[:250],
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Türkiye E-Ticaret Arbitraj & Flipping Radarı — Kontrol Paneli</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
  <header class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30">
    <div class="max-w-7xl mx-auto px-4 py-4 flex flex-wrap items-center justify-between gap-4">
      <div>
        <span class="text-xs font-bold uppercase tracking-wider px-2.5 py-1 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-700">7 Yetkili Mağaza + Akakçe Canlı İndeks</span>
        <h1 class="text-2xl font-extrabold mt-1 bg-gradient-to-r from-emerald-400 via-cyan-400 to-indigo-400 bg-clip-text text-transparent">
          Türkiye E-Ticaret Arbitraj & Flipping Radarı
        </h1>
      </div>
      <div class="flex items-center gap-3 text-xs text-slate-400">
        <div id="lastUpdated" class="bg-slate-800 px-3 py-2 rounded-lg border border-slate-700">Yükleniyor...</div>
        <button onclick="loadData()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-4 py-2 rounded-lg transition">🔄 Yenile</button>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-6 space-y-6">
    <!-- KPI Cards -->
    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="text-xs text-slate-400">İndekslenen Benzersiz Model (SKU)</div>
        <div id="kpiSkus" class="text-3xl font-extrabold text-white mt-1">-</div>
        <div class="text-xs text-emerald-400 mt-1">7 Yetkili Orijinal Distribütör</div>
      </div>
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="text-xs text-slate-400">Aktif Kârlı Arbitraj Fırsatı</div>
        <div id="kpiProfitable" class="text-3xl font-extrabold text-emerald-400 mt-1">-</div>
        <div id="kpiThresholds" class="text-xs text-slate-400 mt-1">Min Kâr / ROI</div>
      </div>
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="text-xs text-slate-400">En Yüksek Net Kâr (Gardrops)</div>
        <div id="kpiMaxProfit" class="text-3xl font-extrabold text-cyan-400 mt-1">-</div>
        <div class="text-xs text-slate-400 mt-1">%6 Hizmet + 95 TL Kargo Düşülmüş</div>
      </div>
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="text-xs text-slate-400">Hızlı Kâr & ROI Simülatörü</div>
        <div class="flex gap-2 mt-2">
          <input id="simBuy" type="number" placeholder="Alış (TL)" value="2800" oninput="runSim()" class="w-1/2 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs" />
          <input id="simFloor" type="number" placeholder="Piyasa (TL)" value="4900" oninput="runSim()" class="w-1/2 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs" />
        </div>
        <div id="simResult" class="text-xs font-bold text-emerald-400 mt-2">-</div>
      </div>
    </section>

    <!-- Charts -->
    <section class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <h2 class="text-sm font-bold text-slate-300 mb-3">🏪 Yetkili Mağaza Ürün Dağılımı</h2>
        <canvas id="storeChart" height="140"></canvas>
      </div>
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <h2 class="text-sm font-bold text-slate-300 mb-3">👟 Marka Dağılımı</h2>
        <canvas id="brandChart" height="140"></canvas>
      </div>
    </section>

    <!-- Filters -->
    <section class="bg-slate-900 border border-slate-800 rounded-xl p-4 flex flex-wrap items-center gap-3">
      <input id="searchInput" type="text" placeholder="🔍 Model, Marka veya SKU ara (Örn: IF6491, Spezial, Nike)..." oninput="renderItems()" class="flex-1 min-w-[240px] bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm" />
      <select id="storeFilter" onchange="renderItems()" class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm">
        <option value="">Tüm Mağazalar</option>
      </select>
      <label class="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
        <input id="onlyProfitable" type="checkbox" checked onchange="renderItems()" class="accent-emerald-500 w-4 h-4" />
        Sadece Hedef Bedenli & Kârlı Fırsatları Göster
      </label>
    </section>

    <!-- Product Grid -->
    <section id="itemsGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></section>
  </main>

  <script>
    let globalData = null;
    let storeChartInstance = null;
    let brandChartInstance = null;

    function runSim() {
      const buy = parseFloat(document.getElementById('simBuy').value || 0);
      const floor = parseFloat(document.getElementById('simFloor').value || 0);
      if (buy <= 0 || floor <= 0) return;
      const sell = Math.round(floor * 0.88);
      const netG = Math.round(sell * 0.94 - 95 - buy);
      const roiG = ((netG / buy) * 100).toFixed(1);
      const netD = Math.round(sell * 0.85 - 95 - buy);
      document.getElementById('simResult').textContent = `Satış: ${sell}₺ | Gardrops: +${netG}₺ (%${roiG}) | Dolap: +${netD}₺`;
    }

    function copyTemplate(idx) {
      const item = window._filteredItems[idx];
      if (!item) return;
      navigator.clipboard.writeText(item.listing_copy_text);
      alert('Hazır İlan Şablonu panoya kopyalandı!\\n\\n' + item.listing_title);
    }

    function renderItems() {
      if (!globalData) return;
      const q = (document.getElementById('searchInput').value || '').toLowerCase();
      const store = document.getElementById('storeFilter').value;
      const onlyProf = document.getElementById('onlyProfitable').checked;

      const filtered = globalData.items.filter(it => {
        if (onlyProf && (!it.is_profitable || !it.sizes || it.sizes.length === 0)) return false;
        if (store && it.site_name !== store) return false;
        if (q && !(`${it.brand} ${it.title} ${it.sku}`.toLowerCase().includes(q))) return false;
        return true;
      });

      window._filteredItems = filtered;
      const grid = document.getElementById('itemsGrid');
      if (filtered.length === 0) {
        grid.innerHTML = `<div class="col-span-full bg-slate-900 border border-slate-800 rounded-xl p-8 text-center text-slate-400">Filtreye uygun ürün bulunamadı. Tüm kataloğu görmek için "Sadece Kârlı Fırsatları Göster" kutusunun işaretini kaldırabilirsiniz.</div>`;
        return;
      }

      grid.innerHTML = filtered.slice(0, 60).map((it, idx) => `
        <div class="bg-slate-900 border ${it.is_profitable ? 'border-emerald-700/70' : 'border-slate-800'} rounded-xl overflow-hidden flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between px-4 py-2.5 bg-slate-800/70 border-b border-slate-800 text-xs">
              <span class="font-bold text-cyan-300">${it.site_name} • ${it.gender}</span>
              <span class="font-extrabold text-amber-400">⭐ ${it.arbitrage_score}/100</span>
            </div>
            <div class="p-4 space-y-2">
              <div class="text-xs text-slate-400 font-mono">SKU: ${it.sku} | İndirim: %${it.total_discount_rate}</div>
              <h3 class="font-bold text-white text-sm line-clamp-2">${it.brand} - ${it.title}</h3>
              <div class="text-xs text-emerald-300">${it.price_trend_badge}</div>
              <div class="grid grid-cols-2 gap-2 bg-slate-950/70 p-2.5 rounded-lg border border-slate-800 text-xs">
                <div>
                  <div class="text-slate-400">Sepette Alış</div>
                  <div class="text-base font-extrabold text-emerald-400">${it.net_basket_price} TL</div>
                </div>
                <div>
                  <div class="text-slate-400">Piyasa En Ucuz</div>
                  <div class="text-base font-bold text-rose-400">~${it.market_floor_price} TL</div>
                </div>
              </div>
              <div class="bg-emerald-950/40 border border-emerald-800/60 rounded-lg p-2.5 text-xs space-y-1">
                <div class="flex justify-between"><span>🟢 Gardrops Net:</span><span class="font-extrabold text-emerald-300">+${it.net_profit_gardrops} TL (%${it.roi_gardrops} ROI)</span></div>
                <div class="flex justify-between"><span>🟣 Dolap Net:</span><span class="font-bold text-cyan-300">+${it.net_profit_dolap} TL (%${it.roi_dolap} ROI)</span></div>
              </div>
              <div class="text-xs text-amber-200">👟 Numaralar: ${it.sizes && it.sizes.length ? it.sizes.join(', ') : 'Referans Katalog Ürünü'}</div>
            </div>
          </div>
          <div class="p-3 bg-slate-950/60 border-t border-slate-800 flex gap-2 text-xs font-semibold">
            <a href="${it.url}" target="_blank" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 text-white py-2 rounded-lg">🛒 Sepete Ekle</a>
            <a href="${it.akakce_url}" target="_blank" class="flex-1 text-center bg-slate-800 hover:bg-slate-700 text-slate-200 py-2 rounded-lg">📊 Akakçe</a>
            <button onclick="copyTemplate(${idx})" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-lg" title="Hazır İlan Şablonunu Kopyala">📋 İlan</button>
          </div>
        </div>
      `).join('');
    }

    async function loadData() {
      const res = await fetch('/api/state');
      globalData = await res.json();
      document.getElementById('kpiSkus').textContent = globalData.summary.unique_skus;
      document.getElementById('kpiProfitable').textContent = globalData.summary.profitable_count;
      document.getElementById('kpiThresholds').textContent = `Min Kâr: ${globalData.thresholds.min_net_profit_tl}₺ | Min ROI: %${globalData.thresholds.min_roi_percent}`;
      document.getElementById('lastUpdated').textContent = 'Son Güncelleme: ' + (globalData.meta.last_updated_at || '').slice(0, 19).replace('T', ' ');

      const maxProf = globalData.items.reduce((acc, x) => Math.max(acc, x.net_profit_gardrops || 0), 0);
      document.getElementById('kpiMaxProfit').textContent = '+' + maxProf + ' TL';

      const storeSel = document.getElementById('storeFilter');
      const currentVal = storeSel.value;
      const stores = Object.keys(globalData.summary.store_counts || {});
      storeSel.innerHTML = '<option value="">Tüm Mağazalar</option>' + stores.map(s => `<option value="${s}">${s} (${globalData.summary.store_counts[s]})</option>`).join('');
      storeSel.value = currentVal;

      const sLabels = Object.keys(globalData.summary.store_counts);
      const sValues = Object.values(globalData.summary.store_counts);
      if (storeChartInstance) storeChartInstance.destroy();
      storeChartInstance = new Chart(document.getElementById('storeChart'), {
        type: 'bar',
        data: { labels: sLabels, datasets: [{ label: 'Ürün Sayısı', data: sValues, backgroundColor: '#10b981' }] },
        options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#94a3b8' } }, x: { ticks: { color: '#94a3b8' } } } }
      });

      const bEntries = Object.entries(globalData.summary.brand_counts).sort((a,b) => b[1]-a[1]).slice(0, 8);
      if (brandChartInstance) brandChartInstance.destroy();
      brandChartInstance = new Chart(document.getElementById('brandChart'), {
        type: 'bar',
        data: { labels: bEntries.map(x=>x[0]), datasets: [{ label: 'Model Sayısı', data: bEntries.map(x=>x[1]), backgroundColor: '#38bdf8' }] },
        options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#94a3b8' } }, x: { ticks: { color: '#94a3b8' } } } }
      });

      runSim();
      renderItems();
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
    parser.add_argument("--host", default="127.0.0.1", help="Sunucu adresi (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Sunucu portu (varsayılan: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), DashboardRequestHandler)
    print(f"🚀 Arbitraj Görsel Web Kontrol Paneli çalışıyor: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb paneli kapatıldı.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
