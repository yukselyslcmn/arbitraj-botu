---
name: arbitraj-yoneticisi
description: >-
  E-Ticaret Arbitraj Botu yönetimi, canlı site taraması, filtre/eşik güncellemesi,
  yeni distribütör site ekleme ve kâr/ROI simülasyonu yapma becerisi. Kullanıcı
  arbitraj taraması yapmak, fırsatları listelemek, eşikleri değiştirmek veya
  yeni site kazıyıcısı eklemek istediğinde bu beceriyi etkinleştir.
---

# E-Ticaret Arbitraj Yöneticisi (Skill Runbook)

Bu beceri, Türkiye yetkili distribütör e-ticaret arbitraj projesini (`arbitraj-botu`) hatasız yönetmek ve çalıştırmak için kullanılır.

## 1. Komut Çalıştırma Kuralı (Çok Önemli)
Sistemde global `python` komutu yerine her zaman yerel sanal ortamı kullan:
- **Canlı Konsol Testi (Telegram'sız):**
  `.\venv\Scripts\python.exe test_run.py`
- **Ana Bot Dry-Run (Belirli Siteler):**
  `.\venv\Scripts\python.exe main.py --once --dry-run --sites occasion,superstep,instreet,barcin,korayspor --max-pages 2`
- **Gerçek Telegram Bildirimli Tarama:**
  `.\venv\Scripts\python.exe main.py --once`

## 2. Durum Veritabanından (`data/products_state.json`) Fırsat Analizi
Kullanıcı mevcut yakalanan ürünleri veya en yüksek indirimli ayakkabıları sorduğunda:
1. `data/products_state.json` dosyasını oku.
2. Ürünleri `total_discount_rate` veya `net_basket_price` değerine göre sırala.
3. `core/arbitrage_engine.py` içindeki `ArbitrageEngine.evaluate()` formülüyle Gardrops (`%6 + 95 TL`) ve Dolap (`%15 + 95 TL`) net kârlarını hesaplayıp tablo halinde sun.

## 3. Yeni Site Kazıyıcısı Ekleme Prosedürü
Yeni bir yetkili satıcı (Örn: Sneaks Up, FashFed, Sportive, Boyner) ekleneceğinde:
1. `config.py` içindeki `SITE_TARGETS` sözlüğüne site anahtarını ve kategori URL'lerini ekle.
2. Site Akinon altyapısındaysa `scrapers/akinon_scraper.py` (`AkinonScraper`) sınıfına bağla; özel JSON/HTML kullanıyorsa `scrapers/base_scraper.py` (`BaseScraper`) sınıfından miras alan yeni bir modül oluştur.
3. `main.py` içindeki `create_scraper()` fabrikasına yeni site anahtarını kaydet.
