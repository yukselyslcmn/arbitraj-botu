# Arbitraj Botu - Antigravity Proje Kuralları (GEMINI.md)

## Ortam ve Python Yürütücüsü
- Bu Windows sisteminde global `python` komutu Microsoft Store kısayoluna bağlı olduğu için doğrudan `python` komutu **kullanılmamalıdır**.
- Tüm Python ve Pip komutları her zaman proje içindeki sanal ortam üzerinden çalıştırılmalıdır:
  - Python: `.\venv\Scripts\python.exe`
  - Pip: `.\venv\Scripts\pip.exe`

## Mimari Standartlar
- Ağ isteklerinde her zaman `core/http_client.py` içindeki `StealthHttpClient` (`curl_cffi` -> `cloudscraper` -> `httpx` fallback zinciri) kullanılmalıdır.
- Beden eşleştirmelerinde `core/parsers.py` içindeki `SizeNormalizer` (kesirli `1/3`, `2/3`, `1/2` -> `.5` dönüşümü) ve fiyat hesaplamalarında `BasketPromotionParser` kullanılmalıdır.
- `data/products_state.json` dosyası GitHub Actions durum takibi için versiyon kontrolünde tutulur; `.env` dosyası ise kesinlikle commit edilmez.
- Yapılan tüm kod ve özellik güncellemeleri SSH üzerinden (`git push origin main`) GitHub deposuna aktarılmalıdır. (Git yolu: `C:\Users\yukse\AppData\Local\Programs\Git\cmd\git.exe`)
