<div align="center">

<img src="assets/hero-banner.svg" alt="Türkiye E-Ticaret Arbitraj & Flipping Radarı Banner" width="100%" />

<br/>

[![Arbitrage Radar CI/CD](https://img.shields.io/github/actions/workflow/status/yukselyslcmn/arbitraj-botu/arbitrage_radar.yml?branch=main&label=7%2F24%20Arbitraj%20Radar%C4%B1&logo=githubactions&style=for-the-badge)](https://github.com/yukselyslcmn/arbitraj-botu/actions)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Anti-Bot Engine](https://img.shields.io/badge/Stealth-curl__cffi%20%7C%20TLS%20Impersonation-10b981?style=for-the-badge&logo=cloudflare&logoColor=white)](#%EF%B8%8F-mimari-ve-teknoloji-y%C4%B1%C4%9F%C4%B1n%C4%B1)
[![Telegram Alerts](https://img.shields.io/badge/Telegram-Foto%C4%9Frafl%C4%B1%20Canl%C4%B1%20Alarm-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](#-telegram-bildirim-kart%C4%B1-%C3%B6nizlemesi)

**Türkiye'deki 5 yetkili orijinal spor ayakkabı distribütörünü (`Occasion`, `SuperStep`, `InStreet`, `Barçın`, `Korayspor`) eşzamanlı tarayan, sepetteki gizli indirimleri hesaplayan, `5 Mağaza Çapraz Fiyat İndeksi + Akakçe` ile gerçek piyasa dip fiyatını doğrulayan ve ikinci el platformları (`Gardrops`, `Sahibinden`, `Letgo`, `Dolap`) için net kâr / ROI analizi yaparak Telegram'a fotoğraflı bildirim gönderen otonom arbitraj radarı.**

</div>

---

## 🎯 Neden Bu Sistem?

E-ticaret sitelerindeki standart indirim takipçileri yalnızca **vitrin fiyatına** ve **şişirilmiş liste fiyatına** bakar. Oysa gerçek flipping ve arbitraj fırsatları:
1. **Sepette Ekstra İndirimlerde** (*"Sepette %30 İndirim"*, *"Sepette 750 TL İndirim"*, *"2. Ürüne %50"*),
2. **En Hızlı Nakde Dönen Altın Bedenlerde** (`Erkek: 41–46`, `Kadın: 37–40.5` — `42 2/3` ve `40.5` gibi kesirli/buçuklu bedenler dahil),
3. **5 Yetkili Orijinal Mağaza + Akakçe Çapraz Fiyat Doğrulamasında** gizlidir.

<div align="center">
  <img src="assets/architecture-flow.svg" alt="Uçtan Uca Arbitraj Mimarisi" width="100%" />
</div>

---

## 🏪 Taranan Yetkili Orijinal Mağazalar ve Çapraz Kıyaslama

Sistem yalnızca **%100 orijinal ve faturalı** ürün satan Türkiye'nin 5 büyük yetkili distribütörünü tarar ve her mağazanın hem indirimli hem de normal fiyatlı kataloglarını `data/products_state.json` içinde indeksleyerek **birbirleriyle ve Akakçe ile** kıyaslar:

| Yetkili Mağaza | Altyapı / Veri Kaynağı | Yakalanan Özel Kampanya & Veri Türü |
| :--- | :--- | :--- |
| **Occasion** (`occasion.com.tr`) | Akinon JSON API + Detay Zenginleştirme | Sepette ekstra `%` / `TL` indirimler, kesirli beden stokları (`1/3`, `2/3`, `.5`) |
| **SuperStep** (`superstep.com.tr`) | Akinon JSON API + Kampanya Rozetleri | Outlet + Yeni sezon sneaker kataloğu, sepet içi otomatik indirim kuralları |
| **InStreet** (`instreet.com.tr`) | Next.js App Router (`self.__next_f.push`) | `ruleInfo` sepet fiyatları, `animatedBadgeList` kampanyaları, varyant bazlı stok |
| **Barçın** (`barcin.com`) | Akinon JSON + HTML Fallback | Nike / Jordan / Adidas yetkili distribütör stokları ve outlet sepet indirimleri |
| **Korayspor** (`korayspor.com`) | Next.js `__NEXT_DATA__` SSR + Detay Beden | İndirimli ve sezon spor ayakkabı kataloğu, anlık beden kırılımı |
| **Akakçe** (`akakce.com`) | Canlı Piyasa Doğrulayıcı (`span.pt_v9/v8`) | Trendyol Plus, Hepsiburada, Amazon, Boyner dahil **en ucuz satıcı** doğrulaması |

---

## 📱 Telegram Bildirim Kartı Önizlemesi

Fırsat yakalandığında Telegram botunuz üzerinden ürün fotoğrafı, tam maliyet/kâr tablosu, altın beden listesi ve tek tıkla satın alma / kontrol butonlarıyla birlikte anında telefonunuza düşer:

<div align="center">
  <img src="assets/telegram-card-preview.svg" alt="Telegram Bildirim Kartı Önizlemesi" width="540" />
</div>

---

## 🧠 Algoritma ve Karar Akış Şeması

```mermaid
flowchart TD
    A["5 Yetkili Mağaza Eşzamanlı Tarama<br/>(Occasion, SuperStep, InStreet, Barçın, Korayspor)"] --> B["StealthHttpClient<br/>(curl_cffi TLS Impersonation -> cloudscraper -> httpx)"]
    B --> C["Veri Normalizasyonu<br/>• Kanonik SKU (747CFA0002)<br/>• Kesirli Beden (42 2/3 -> 42.5)<br/>• Net Sepet Fiyatı (BasketPromotionParser)"]
    C --> D["StateStorage İndeksleme<br/>(5 Mağazanın Tüm Kataloğu Kaydedilir)"]
    D --> E{"Ön Filtreler Geçildi mi?<br/>• Hedef Marka & Ayakkabı Kategorisi<br/>• Altın Beden Stokta Var mı?<br/>• Net Sepet <= 5.500 TL & İndirim >= %40"}
    E -- Hayır --> Z["Sadece Çapraz Mağaza Referans Fiyatı Olarak Sakla"]
    E -- Evet --> F["Çift Katmanlı Piyasa Dip Fiyat Doğrulaması<br/>1) Diğer 4 Yetkili Mağazadaki En Düşük Fiyat<br/>2) Akakçe Canlı En Ucuz Satıcı Fiyatı"]
    F --> G["İkinci El Net Kâr & ROI Hesaplama<br/>• Önerilen Satış = Piyasa Dip x 0.88<br/>• Gardrops (%6 + 95 TL) | Dolap (%15 + 95 TL)"]
    G --> H{"Kârlılık Kriteri Sağlandı mı?<br/>Net Kâr >= 500 TL VE ROI >= %25"}
    H -- Evet --> I["Telegram Fotoğraflı Alarm Gönder<br/>(İlk Çalışma & %5 Ekstra Fiyat Çöküş Korumalı)"]
```

---

## 🧮 Matematiksel Kâr, ROI ve Puanlama Formülleri

Bot, ürünün şişirilmiş liste fiyatına (`retail_price`) **asla güvenmez**. Kâr hesabını yalnızca **5 yetkili mağaza çapraz kıyaslaması ve Akakçe'deki en ucuz satıcı fiyatından (`market_floor_price`)** yapar:

1. **Hızlı Nakde Dönüş (Quick-Flip) Satış Fiyatı:**
   $$\text{Önerilen Satış Fiyatı} = \text{Piyasa Dip Fiyatı} \times (1 - 0.12)$$
2. **Gardrops / Sahibinden / Letgo Net Kârı (`%6` Hizmet Bedeli + `95 TL` Kargo/Paketleme):**
   $$\text{Net Kâr}_{\text{Gardrops}} = (\text{Önerilen Satış} \times 0.94) - 95\,\text{TL} - \text{Sepette Net Alış}$$
3. **Dolap Net Kârı (`%14` Komisyon + `%1` Hizmet + `95 TL` Kargo):**
   $$\text{Net Kâr}_{\text{Dolap}} = (\text{Önerilen Satış} \times 0.85) - 95\,\text{TL} - \text{Sepette Net Alış}$$
4. **Yatırım Getirisi (ROI %):**
   $$\text{ROI (\%)} = \left( \frac{\text{Net Kâr}_{\text{Gardrops}}}{\text{Sepette Net Alış}} \right) \times 100$$

---

## 🗂️ Proje Dizin Yapısı

```text
arbitraj-botu/
├── assets/
│   ├── hero-banner.svg              # README üst kapak görseli
│   ├── architecture-flow.svg        # 4 aşamalı mimari akış görseli
│   └── telegram-card-preview.svg    # Telegram bildirim kartı görseli
├── core/
│   ├── http_client.py               # TLS Impersonation (curl_cffi -> cloudscraper -> httpx)
│   ├── parsers.py                   # SizeNormalizer (kesirli/buçuklu beden) & BasketPromotionParser
│   ├── arbitrage_engine.py          # Net kâr, ROI, Arbitraj Puanı (1-100) ve filtre motoru
│   ├── storage.py                   # 5 mağaza çapraz fiyat veritabanı & spam/fiyat düşüş koruması
│   └── notifier.py                  # Telegram Bot API (sendPhoto + Inline Keyboard)
├── scrapers/
│   ├── base_scraper.py              # Ortak kazıyıcı şablonu ve JSON-LD ayrıştırıcı
│   ├── akinon_scraper.py            # Occasion, SuperStep ve Barçın kazıyıcısı
│   ├── instreet_scraper.py          # InStreet Next.js App Router (__next_f) kazıyıcısı
│   ├── barcin_koray_scraper.py      # Korayspor (__NEXT_DATA__ SSR) ve Barçın hibrit kazıyıcısı
│   └── market_comparator.py         # 5 Yetkili Mağaza + Akakçe canlı piyasa doğrulayıcı
├── data/
│   └── products_state.json          # Çapraz mağaza fiyat indeksi ve bildirim geçmişi
├── .github/workflows/
│   └── arbitrage_radar.yml          # GitHub Actions 7/24 bulut otomasyon iş akışı
├── config.py                        # Eşik değerleri, altın bedenler, hedef markalar ve komisyonlar
├── main.py                          # Çoklu iş parçacıklı (ThreadPool) ana orkestratör
├── 1_KURULUM_YAP.bat                # Tek tıkla Windows sanal ortam ve kütüphane kurulumu
├── 2_TEK_SEFERLIK_TEST_ET.bat       # Tek tıkla canlı test taraması
└── 3_BOTU_7_24_DONGUDE_CALISTIR.bat # Tek tıkla yerel 15 dakikalık kesintisiz döngü
```

---

## 🚀 Hızlı Kurulum ve Çalıştırma

### 1. Ortamı Kurun
Windows üzerinde tek tıkla kurulum için `1_KURULUM_YAP.bat` dosyasına çift tıklayabilir veya terminalden çalıştırabilirsiniz:

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
```

### 2. `.env` Yapılandırması
Proje kök dizininde `.env.example` dosyasını `.env` olarak kopyalayın ve Telegram bilgilerinizi girin:

```env
TELEGRAM_BOT_TOKEN=1234567890:AAH_ornek_bot_token_buraya_gelecek
TELEGRAM_CHAT_ID=-1001234567890
MIN_NET_PROFIT_TL=550
MIN_ROI_PERCENT=30
MAX_BASKET_PRICE=5500
MIN_TOTAL_DISCOUNT_RATE=40
```

### 3. Çalıştırma Komutları

* **Tüm 5 Yetkili Mağazayı Tek Seferlik Canlı Tarama:**
  ```powershell
  .\venv\Scripts\python.exe main.py --once
  ```
* **Telegram Mesajı Atmadan Simülasyon (Dry-Run) Testi:**
  ```powershell
  .\venv\Scripts\python.exe main.py --once --dry-run --max-pages 1
  ```
* **Bilgisayarda 7/24 Kesintisiz (Tam 15 Dakikada Bir) Çalıştırma:**
  `3_BOTU_7_24_DONGUDE_CALISTIR.bat` dosyasına çift tıklayın veya:
  ```powershell
  .\venv\Scripts\python.exe main.py --loop --interval 900
  ```

---

## ☁️ GitHub Actions ile 7/24 Ücretsiz Bulut Otomasyonu

Proje içindeki [`.github/workflows/arbitrage_radar.yml`](.github/workflows/arbitrage_radar.yml) iş akışı sayesinde bilgisayarınız kapalıyken bile GitHub sunucularında otomatik çalışır:
1. GitHub deponuzun **Settings → Secrets and variables → Actions** bölümüne `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` değerlerini ekleyin.
2. **Settings → Actions → General → Workflow permissions** altından **Read and write permissions** seçeneğini işaretleyin.
3. İş akışı her çalıştığında 5 mağazanın güncel fiyat indeksini ve bildirilen fırsatları `data/products_state.json` dosyasına otomatik kaydedip depoya senkronize eder; böylece aynı ürün için tekrar tekrar bildirim atılmaz, yalnızca **yeni fırsatlar** veya **en az `%5` ekstra fiyat çöküşleri** bildirilir.
