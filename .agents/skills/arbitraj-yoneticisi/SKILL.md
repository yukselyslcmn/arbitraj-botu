---
name: arbitraj-yoneticisi
description: >-
  E-Ticaret Arbitraj Botu yönetimi, canlı site taraması, filtre/eşik güncellemesi,
  yeni distribütör site ekleme, hazır ilan şablonu üretme, interaktif Telegram komutları
  ve görsel web kontrol paneli (`dashboard.py`) çalıştırma becerisi. Kullanıcı
  arbitraj taraması yapmak, fırsatları listelemek, eşikleri değiştirmek veya
  yeni site kazıyıcısı eklemek istediğinde bu beceriyi etkinleştir.
---

# E-Ticaret Arbitraj Yöneticisi (Skill Runbook)

Bu beceri, Türkiye yetkili distribütör e-ticaret arbitraj projesini (`arbitraj-botu`) hatasız yönetmek ve çalıştırmak için kullanılır.

## 1. Komut Çalıştırma Kuralı (Çok Önemli)
Sistemde global `python` komutu yerine her zaman yerel sanal ortamı kullan:
- **Canlı Konsol Testi (Telegram'sız):**
  `.\venv\Scripts\python.exe test_run.py`
- **7 Mağaza Dry-Run Taraması:**
  `.\venv\Scripts\python.exe main.py --once --dry-run --sites occasion,superstep,instreet,barcin,korayspor,sportive,fashfed --max-pages 1`
- **Gerçek Telegram Bildirimli Tarama:**
  `.\venv\Scripts\python.exe main.py --once`
- **İnteraktif Telegram Komut Dinleyici (`/durum`, `/firsatlar`, `/ara`, `/filtre`, `/takip`):**
  `.\venv\Scripts\python.exe main.py --listen-commands`
- **Görsel Web Kontrol Paneli (`http://127.0.0.1:8080`):**
  `.\venv\Scripts\python.exe dashboard.py`

## 2. Durum Veritabanından (`data/products_state.json`) Fırsat Analizi
Kullanıcı mevcut yakalanan ürünleri veya en yüksek indirimli ayakkabıları sorduğunda:
1. `data/products_state.json` dosyasını oku veya `dashboard.build_dashboard_payload()` çağır.
2. Ürünleri `total_discount_rate` veya `net_profit_gardrops` değerine göre sırala.
3. `core/arbitrage_engine.py` içindeki `ArbitrageEngine.evaluate()` formülüyle Gardrops (`%6 + 95 TL`) ve Dolap (`%15 + 95 TL`) net kârlarını ve `core/listing_generator.py` ile hazır ilan şablonunu sun.

## 3. Yeni Site Kazıyıcısı Ekleme Prosedürü
Yeni bir yetkili satıcı ekleneceğinde:
1. `config.py` içindeki `SITE_TARGETS` sözlüğüne site anahtarını ve kategori URL'lerini ekle.
2. Site Akinon altyapısındaysa `scrapers/akinon_scraper.py` (`AkinonScraper`) sınıfına bağla; özel JSON/HTML kullanıyorsa `scrapers/base_scraper.py` (`BaseScraper`) sınıfından miras alan yeni bir modül oluştur.
3. `main.py` içindeki `create_scraper()` fabrikasına yeni site anahtarını kaydet.
