import html
import logging
import socket
from typing import Optional

import config
from core.arbitrage_engine import ArbitrageEngine, ScrapedProduct
from core.http_client import StealthHttpClient, mask_sensitive
from core.listing_generator import ListingGenerator
from core.parsers import NormalizedSizeInfo
from core.storage import StateStorage
from scrapers.market_comparator import MarketComparator

logger = logging.getLogger("arbitraj.telegram_commands")


def get_local_network_ip() -> str:
    """Yerel ağ (Wi-Fi / LAN) IP adresini tespit eder (Telefondan web paneline erişim için)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return str(ip)
    except Exception:
        return "127.0.0.1"


class TelegramCommandHandler:
    """
    İki yönlü İnteraktif Telegram Komut ve Mesaj İstemcisi (`getUpdates`).
    Kullanıcı sohbetten ne yazarsa yazsın hem komutları (/durum, /ara, /filtre, /takip)
    çalıştırır hem de tek tıkla tarayıcıda açılacak Web Paneli linklerini iletir.
    """

    def __init__(
        self,
        storage: StateStorage,
        comparator: Optional[MarketComparator] = None,
        bot_token: str = config.TELEGRAM_BOT_TOKEN,
        chat_id: str = config.TELEGRAM_CHAT_ID,
        http_client: Optional[StealthHttpClient] = None,
    ) -> None:
        self.storage = storage
        self.comparator = comparator or MarketComparator(storage=storage)
        self.bot_token = bot_token
        self.chat_id = str(chat_id).strip()
        self.client = http_client or StealthHttpClient(min_delay=0.1, max_delay=0.3, max_retries=2, timeout=15)

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id and "ornek_bot_token" not in self.bot_token)

    def send_reply(self, text: str, reply_markup: Optional[dict] = None) -> bool:
        if not self.is_configured:
            return False
        base_api = f"https://api.telegram.org/bot{self.bot_token}"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.client.post(
                f"{base_api}/sendMessage",
                json_payload=payload,
                skip_throttle=True,
            )
            return bool(resp and resp.status_code == 200 and resp.json().get("ok"))
        except Exception as exc:
            logger.warning("Komut yanıtı gönderilemedi: %s", mask_sensitive(exc))
            return False

    def process_pending_commands(self) -> int:
        """Bekleyen Telegram mesajlarını ve komutlarını (`getUpdates`) çeker, çalıştırır."""
        if not self.is_configured:
            return 0

        last_update_id = self.storage.get_last_telegram_update_id()
        offset = (last_update_id + 1) if last_update_id > 0 else 0
        base_api = f"https://api.telegram.org/bot{self.bot_token}"

        try:
            resp = self.client.get(
                f"{base_api}/getUpdates",
                params={"offset": offset, "timeout": 1, "limit": 20},
                is_json=True,
                skip_throttle=True,
            )
            if not resp or resp.status_code != 200:
                return 0
            payload = resp.json()
            if not isinstance(payload, dict) or not payload.get("ok"):
                return 0
            updates = payload.get("result") or []
        except Exception as exc:
            logger.debug("Telegram getUpdates hatası: %s", mask_sensitive(exc))
            return 0

        processed = 0
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            upd_id = int(upd.get("update_id") or 0)
            if upd_id > last_update_id:
                last_update_id = upd_id
                self.storage.set_last_telegram_update_id(last_update_id)

            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_obj = msg.get("chat") or {}
            sender_chat_id = str(chat_obj.get("id") or "").strip()
            # Güvenlik: Sadece yetkili TELEGRAM_CHAT_ID'den gelen mesajları kabul et
            if sender_chat_id != self.chat_id:
                continue

            raw_text = str(msg.get("text") or "").strip()
            if not raw_text:
                continue

            self._dispatch_message(raw_text)
            processed += 1

        return processed

    def _dispatch_message(self, raw_text: str) -> None:
        parts = raw_text.split()
        first_token = parts[0].lower().split("@")[0]
        args = parts[1:]
        text_lower = raw_text.lower()

        # 1. Doğrudan Web Panel Talebi veya Panel Anahtar Kelimeleri
        panel_keywords = ("panel", "link", "web", "site", "dashboard", "göster", "veriler", "aç", "ac", "arayüz", "ekran")
        if first_token in ("/panel", "/web", "/link", "/dashboard") or any(kw in text_lower for kw in panel_keywords):
            self._send_web_panel_link()
            return

        # 2. Belirli Komutlar
        if first_token in ("/start", "/yardim", "/help"):
            self._send_help_and_panel()
        elif first_token in ("/durum", "/status"):
            self.send_reply(self._build_status_message())
        elif first_token in ("/firsatlar", "/top"):
            self.send_reply(self._build_top_opportunities_message())
        elif first_token in ("/ara", "/search"):
            self.send_reply(self._handle_search_command(" ".join(args)))
        elif first_token in ("/filtre", "/filter"):
            self.send_reply(self._handle_filter_command(args))
        elif first_token in ("/takip", "/watch"):
            self.send_reply(self._handle_watch_command(args))
        else:
            # Kullanıcı herhangi serbest bir metin yazdığında hem panel linkini hem komutları ver
            self._send_friendly_default_reply(raw_text)

    def _get_panel_urls(self) -> tuple[str, str, str]:
        public_url = config.PUBLIC_DASHBOARD_URL or "https://yukselyslcmn.github.io/arbitraj-botu/"
        lan_ip = get_local_network_ip()
        lan_url = f"http://{lan_ip}:{config.LOCAL_DASHBOARD_PORT}"
        local_url = f"http://localhost:{config.LOCAL_DASHBOARD_PORT}"
        return public_url, lan_url, local_url

    def register_bot_interface(self) -> dict[str, bool]:
        """
        Telegram Bot API üzerinden:
        1. setMyCommands: Komut menüsünü (/panel, /durum, /firsatlar vb.) kaydeder.
        2. setChatMenuButton: Sohbet kutusunun soluna kalıcı '[📊 Web Paneli]' Mini App butonunu yerleştirir.
        """
        if not self.is_configured:
            return {"commands": False, "menu_button": False}

        base_api = f"https://api.telegram.org/bot{self.bot_token}"
        results = {"commands": False, "menu_button": False}

        # 1. Komut listesini kaydet
        commands_payload = {
            "commands": [
                {"command": "panel", "description": "🌐 Bulut Web Paneli (Her Yerden Mobil Erişim)"},
                {"command": "firsatlar", "description": "🔥 En Yüksek Kârlı İlk 5 Fırsat"},
                {"command": "durum", "description": "📊 Sistem Durumu ve Eşikler"},
                {"command": "ara", "description": "🔍 Model/SKU Ara ve Hazır İlan Al"},
                {"command": "filtre", "description": "⚙️ Minimum Kâr ve ROI Eşiklerini Güncelle"},
                {"command": "takip", "description": "🎯 Özel Model Fiyat Takip Alarmı Kur"},
                {"command": "yardim", "description": "ℹ️ Bot Kullanım Kılavuzu"},
            ]
        }
        try:
            r1 = self.client.post(f"{base_api}/setMyCommands", json_payload=commands_payload, skip_throttle=True)
            results["commands"] = bool(r1 and r1.status_code == 200 and r1.json().get("ok"))
        except Exception as exc:
            logger.warning("setMyCommands hatası: %s", mask_sensitive(exc))

        # 2. Kalıcı Sohbet Menü Butonunu (Telegram Mini App) kaydet
        public_url, _, _ = self._get_panel_urls()
        menu_payload = {
            "chat_id": int(self.chat_id) if self.chat_id.isdigit() else self.chat_id,
            "menu_button": {
                "type": "web_app",
                "text": "📊 Web Paneli",
                "web_app": {"url": public_url},
            },
        }
        try:
            r2 = self.client.post(f"{base_api}/setChatMenuButton", json_payload=menu_payload, skip_throttle=True)
            results["menu_button"] = bool(r2 and r2.status_code == 200 and r2.json().get("ok"))
        except Exception as exc:
            logger.warning("setChatMenuButton hatası: %s", mask_sensitive(exc))

        return results

    def _send_web_panel_link(self) -> None:
        public_url, lan_url, local_url = self._get_panel_urls()
        products = self.storage.state.get("products", {})
        total_skus = len(products)

        text = "\n".join(
            [
                "🌐 <b>E-TİCARET ARBİTRAJ WEB KONTROL PANELİ</b>",
                "",
                "İşyerindeyken, dışarıdayken veya evde Wi-Fi olmadan telefonunuzdan mobil veriyle dilediğiniz zaman erişebilirsiniz:",
                "",
                f"🌍 <b>Mobil / Bulut Erişimi (Her Yerden):</b>\n👉 <a href=\"{public_url}\">{public_url}</a>",
                "",
                f"🖥️ <b>Ev/Ofis Bilgisayarı (Lokal):</b>\n👉 <a href=\"{local_url}\">{local_url}</a>",
                "",
                f"📱 <b>Yerel Wi-Fi Ağı:</b>\n👉 <a href=\"{lan_url}\">{lan_url}</a>",
                "",
                f"📊 <b>Veritabanı:</b> <code>{total_skus} benzersiz model</code> analiz edildi.",
                "💡 <i>Aşağıdaki <b>'🚀 Bulut Web Paneli'</b> butonuna veya sol alttaki <b>'📊 Web Paneli'</b> menü tuşuna basarak anında açabilirsiniz!</i>",
            ]
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "🚀 Bulut Web Paneli (Her Yerden Aç)", "url": public_url},
                ],
                [
                    {"text": "📱 Telegram İçi Mini App", "web_app": {"url": public_url}},
                ],
                [
                    {"text": "💻 Bilgisayar (Lokal)", "url": local_url},
                    {"text": "📶 Ev Wi-Fi", "url": lan_url},
                ],
            ]
        }
        self.send_reply(text, reply_markup=reply_markup)

    def _send_help_and_panel(self) -> None:
        public_url, lan_url, local_url = self._get_panel_urls()
        text = "\n".join(
            [
                "🤖 <b>Türkiye E-Ticaret Arbitraj &amp; Flipping Radarı</b>",
                "",
                "🌐 <b>Web Paneli:</b> Tarayıcıda açmak için <code>/panel</code> yazabilir veya aşağıdaki butonlara tıklayabilirsiniz.",
                f"👉 <b>Mobil Bulut Paneli:</b> <a href=\"{public_url}\">{public_url}</a>",
                "",
                "<b>Kullanabileceğiniz Komutlar:</b>",
                "🌐 <code>/panel</code> — Mobil ve bulut kontrol paneli bağlantıları",
                "📊 <code>/durum</code> — 7 yetkili mağaza istatistikleri ve aktif eşikler",
                "🔥 <code>/firsatlar</code> — En yüksek kârlı ilk 5 fırsat",
                "🔍 <code>/ara &lt;model/SKU&gt;</code> — Canlı arama ve anında ilan şablonu (Örn: <code>/ara IF6491</code>)",
                "⚙️ <code>/filtre &lt;kar&gt; &lt;roi&gt;</code> — Eşikleri güncelle (Örn: <code>/filtre 600 30</code>)",
                "🎯 <code>/takip &lt;SKU&gt; &lt;fiyat&gt;</code> — Özel fiyat alarmı kur (Örn: <code>/takip IF6491 3200</code>)",
            ]
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🚀 Bulut Web Panelini Aç", "url": public_url}],
                [{"text": "📱 Telegram Mini App", "web_app": {"url": public_url}}],
            ]
        }
        self.send_reply(text, reply_markup=reply_markup)

    def _send_friendly_default_reply(self, user_msg: str) -> None:
        public_url, lan_url, local_url = self._get_panel_urls()
        safe_msg = html.escape(user_msg[:60])
        text = "\n".join(
            [
                f"👋 <i>'{safe_msg}'</i> mesajınızı aldım!",
                "",
                "Tüm fırsatları, filtreleri, kâr simülatörünü ve grafikleri dilediğiniz yerden açabilirsiniz:",
                f"👉 <b>Bulut Paneli (Mobil Veri Uyumlu):</b>\n<a href=\"{public_url}\">{public_url}</a>",
                "",
                "🔍 Doğrudan model aramak için: <code>/ara IF6491</code>",
                "📊 Mağaza durumunu görmek için: <code>/durum</code>",
                "📋 Tüm komutları listelemek için: <code>/yardim</code>",
            ]
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🚀 Bulut Web Paneli", "url": public_url}],
                [{"text": "📱 Telegram İçi Mini App", "web_app": {"url": public_url}}],
            ]
        }
        self.send_reply(text, reply_markup=reply_markup)

    def _build_status_message(self) -> str:
        products = self.storage.state.get("products", {})
        store_counts: dict[str, int] = {}
        for sku_entry in products.values():
            if not isinstance(sku_entry, dict):
                continue
            for s_key, s_data in (sku_entry.get("sites") or {}).items():
                if isinstance(s_data, dict):
                    s_name = str(s_data.get("site_name") or s_key)
                    store_counts[s_name] = store_counts.get(s_name, 0) + 1

        stores_str = ", ".join(f"<b>{html.escape(k)}:</b> {v}" for k, v in sorted(store_counts.items())) or "Henüz yok"
        watchlist = self.storage.get_watchlist()
        public_url, _, local_url = self._get_panel_urls()

        return "\n".join(
            [
                "📡 <b>ARBİTRAJ RADARI SİSTEM DURUMU</b>",
                "",
                f"📦 <b>İndekslenen Benzersiz Model (SKU):</b> <code>{len(products)}</code>",
                f"🏪 <b>Mağaza Dağılımı:</b> {stores_str}",
                f"🎯 <b>Takip Listesi (/takip):</b> <code>{len(watchlist)}</code> ürün",
                "",
                "⚙️ <b>Aktif Ticari Eşikler:</b>",
                f"  • Maksimum Sepet Alış: <code>{config.MAX_BASKET_PRICE:.0f} TL</code>",
                f"  • Minimum Toplam İndirim: <code>%{config.MIN_TOTAL_DISCOUNT_RATE:.0f}</code>",
                f"  • Minimum Gardrops Net Kâr: <code>{config.MIN_NET_PROFIT_TL:.0f} TL</code>",
                f"  • Minimum ROI: <code>%{config.MIN_ROI_PERCENT:.0f}</code>",
                "",
                f"🌐 <b>Bulut Web Paneli:</b> <a href=\"{public_url}\">{public_url}</a>",
                f"💻 <b>Lokal Panel:</b> <a href=\"{local_url}\">{local_url}</a>",
            ]
        )

    def _build_top_opportunities_message(self) -> str:
        analyses = self._evaluate_all_stored_opportunities()
        if not analyses:
            return "ℹ️ Şu anda aktif eşikleri geçen kayıtlı fırsat bulunamadı."

        lines = ["🏆 <b>EN YÜKSEK KÂRLI İLK 5 ARBİTRAJ FIRSATI</b>", ""]
        for idx, item in enumerate(analyses[:5], start=1):
            p = item.product
            lines.append(
                f"{idx}. <b>[{html.escape(p.site_name)}] {html.escape(p.brand)} - {html.escape(p.title)}</b> (<code>{html.escape(p.display_sku)}</code>)\n"
                f"   🛒 Alış: <b>{p.net_basket_price:,.0f} TL</b> | 🌐 Piyasa: ~{item.market_floor_price:,.0f} TL\n"
                f"   💰 Gardrops Net: <b>+{item.net_profit_gardrops:,.0f} TL (%{item.roi_gardrops:.0f} ROI)</b> | "
                f'<a href="{html.escape(p.url, quote=True)}">Mağazaya Git</a>'.replace(",", ".")
            )
            lines.append("")
        local_url, _ = self._get_panel_urls()
        lines.append(f"🌐 <b>Tüm 50+ fırsatı Web Panelinde Gör:</b> <a href=\"{local_url}\">{local_url}</a>")
        return "\n".join(lines)

    def _handle_search_command(self, query: str) -> str:
        q = query.strip()
        if not q:
            return "⚠️ Lütfen aranacak ürün kodunu veya model adını yazın. Örn: <code>/ara IF6491</code>"

        q_low = q.lower()
        q_clean = q.upper().replace("-", "").replace(" ", "")
        matches: list[ScrapedProduct] = []

        for c_sku, sku_entry in (self.storage.state.get("products") or {}).items():
            if not isinstance(sku_entry, dict):
                continue
            title = str(sku_entry.get("title") or "")
            brand = str(sku_entry.get("brand") or "")
            d_sku = str(sku_entry.get("display_sku") or c_sku)
            if q_clean in str(c_sku).upper() or q_low in f"{brand} {title} {d_sku}".lower():
                for s_key, s_data in (sku_entry.get("sites") or {}).items():
                    if not isinstance(s_data, dict):
                        continue
                    sizes = [
                        NormalizedSizeInfo(original_text=str(sz), normalized_size=str(sz))
                        for sz in (s_data.get("sizes") or [])
                    ]
                    prod = ScrapedProduct(
                        site_key=s_key,
                        site_name=str(s_data.get("site_name") or s_key),
                        canonical_sku=str(c_sku),
                        display_sku=d_sku,
                        brand=brand,
                        title=title,
                        gender=str(sku_entry.get("gender") or "Unisex"),
                        url=str(s_data.get("url") or ""),
                        image_url=str(s_data.get("image_url") or ""),
                        retail_price=float(s_data.get("retail_price") or 0),
                        sale_price=float(s_data.get("sale_price") or 0),
                        net_basket_price=float(s_data.get("net_basket_price") or 0),
                        total_discount_rate=float(s_data.get("total_discount_rate") or 0),
                        basket_discount_rate=0.0,
                        campaign_name=str(s_data.get("campaign_name") or ""),
                        sizes=sizes,
                        verified_akakce_url=sku_entry.get("verified_akakce_url"),
                    )
                    if prod.net_basket_price > 0:
                        matches.append(prod)

        if not matches:
            return f"🔍 <code>{html.escape(q)}</code> için veritabanında eşleşen ürün bulunamadı."

        matches.sort(key=lambda x: x.net_basket_price)
        best = matches[0]
        floor_price, floor_src = self.comparator.get_market_floor_price(best, allow_live_query=True)
        analysis = ArbitrageEngine.evaluate(best, market_floor_price=floor_price, market_floor_source=floor_src)

        store_lines = [
            f"  • <b>{html.escape(m.site_name)}:</b> <code>{m.net_basket_price:,.0f} TL</code> (%{m.total_discount_rate:.0f} İndirim)".replace(
                ",", "."
            )
            for m in matches[:6]
        ]
        copy_block = ListingGenerator.build_telegram_copy_block(analysis)

        return "\n".join(
            [
                f"🔍 <b>ARAMA SONUCU: {html.escape(best.brand)} - {html.escape(best.title)} ({html.escape(best.display_sku)})</b>",
                "",
                "🏪 <b>Yetkili Mağaza Fiyatları:</b>",
                *store_lines,
                "",
                f"🌐 <b>Piyasa Dip Fiyatı:</b> <code>~{floor_price:,.0f} TL</code> ({html.escape(floor_src)})".replace(",", "."),
                f"🟢 <b>Gardrops Net Kâr:</b> <code>{analysis.net_profit_gardrops:+,.0f} TL</code> (<code>%{analysis.roi_gardrops:.0f} ROI</code>)".replace(
                    ",", "."
                ),
                f"🟣 <b>Dolap Net Kâr:</b> <code>{analysis.net_profit_dolap:+,.0f} TL</code> (<code>%{analysis.roi_dolap:.0f} ROI</code>)".replace(
                    ",", "."
                ),
                "",
                copy_block,
            ]
        )

    def _handle_filter_command(self, args: list[str]) -> str:
        if len(args) < 2:
            return (
                "⚙️ <b>Kullanım:</b> <code>/filtre &lt;min_net_kar_tl&gt; &lt;min_roi_yuzde&gt;</code>\n"
                f"Mevcut: Min Kâr = <code>{config.MIN_NET_PROFIT_TL:.0f} TL</code>, Min ROI = <code>%{config.MIN_ROI_PERCENT:.0f}</code>"
            )
        try:
            new_profit = float(args[0])
            new_roi = float(args[1])
        except ValueError:
            return "⚠️ Lütfen sayısal değer girin. Örn: <code>/filtre 600 30</code>"

        config.MIN_NET_PROFIT_TL = new_profit
        config.MIN_ROI_PERCENT = new_roi
        self.storage.save_custom_filters(min_net_profit_tl=new_profit, min_roi_percent=new_roi)
        self.storage.save()
        return (
            "✅ <b>Ticari filtre eşikleri güncellendi ve kaydedildi:</b>\n"
            f"  • Minimum Net Kâr: <code>{new_profit:.0f} TL</code>\n"
            f"  • Minimum ROI: <code>%{new_roi:.0f}</code>"
        )

    def _handle_watch_command(self, args: list[str]) -> str:
        if len(args) < 2:
            return "🎯 <b>Kullanım:</b> <code>/takip &lt;SKU&gt; &lt;hedef_fiyat_tl&gt;</code> (Örn: <code>/takip IF6491 3200</code>)"
        sku_arg = args[0].upper().strip()
        try:
            target_price = float(args[1])
        except ValueError:
            return "⚠️ Hedef fiyat sayısal olmalıdır. Örn: <code>/takip IF6491 3200</code>"

        self.storage.add_watchlist_item(sku_arg, target_price)
        self.storage.save()
        return (
            f"🎯 <b>Takip Alarmı Kuruldu!</b>\n"
            f"<code>{html.escape(sku_arg)}</code> kodlu ürün herhangi bir yetkili mağazada "
            f"<b>{target_price:,.0f} TL</b> veya altına düştüğünde anında bildirim alacaksınız.".replace(",", ".")
        )

    def _evaluate_all_stored_opportunities(self):
        results = []
        for c_sku, sku_entry in (self.storage.state.get("products") or {}).items():
            if not isinstance(sku_entry, dict):
                continue
            verified_floor = sku_entry.get("verified_market_floor_price")
            verified_src = str(sku_entry.get("verified_market_floor_source") or "Kayıtlı Piyasa Dip")
            for s_key, s_data in (sku_entry.get("sites") or {}).items():
                if not isinstance(s_data, dict):
                    continue
                raw_sizes = s_data.get("sizes") or []
                if not raw_sizes:
                    continue
                sizes = [NormalizedSizeInfo(original_text=str(sz), normalized_size=str(sz)) for sz in raw_sizes]
                prod = ScrapedProduct(
                    site_key=s_key,
                    site_name=str(s_data.get("site_name") or s_key),
                    canonical_sku=str(c_sku),
                    display_sku=str(sku_entry.get("display_sku") or c_sku),
                    brand=str(sku_entry.get("brand") or ""),
                    title=str(sku_entry.get("title") or ""),
                    gender=str(sku_entry.get("gender") or "Unisex"),
                    url=str(s_data.get("url") or ""),
                    image_url=str(s_data.get("image_url") or ""),
                    retail_price=float(s_data.get("retail_price") or 0),
                    sale_price=float(s_data.get("sale_price") or 0),
                    net_basket_price=float(s_data.get("net_basket_price") or 0),
                    total_discount_rate=float(s_data.get("total_discount_rate") or 0),
                    basket_discount_rate=0.0,
                    campaign_name=str(s_data.get("campaign_name") or ""),
                    sizes=sizes,
                )
                floor_val = float(verified_floor) if isinstance(verified_floor, (int, float)) and verified_floor > 0 else None
                if floor_val is None:
                    cross = self.storage.get_cross_site_floor_price(str(c_sku), s_key, prod.brand, prod.gender)
                    if cross:
                        floor_val, verified_src = cross
                if floor_val:
                    ev = ArbitrageEngine.evaluate(prod, market_floor_price=floor_val, market_floor_source=verified_src)
                    if ev.is_profitable_opportunity:
                        results.append(ev)

        results.sort(key=lambda x: (x.net_profit_gardrops, x.roi_gardrops), reverse=True)
        return results
