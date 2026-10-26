import html
import logging
import time
from typing import Optional

import config
from core.arbitrage_engine import ArbitrageAnalysis
from core.http_client import StealthHttpClient

logger = logging.getLogger("arbitraj.notifier")


class TelegramNotifier:
    """
    Görselli Telegram Bildirim İstemcisi.
    Öncelikle `sendPhoto` uç noktasını dener; görsel URL'si geçersizse veya
    Telegram tarafından indirilemezse otomatik olarak `sendMessage` uç noktasına düşer.
    Ağ isteklerinde TLS engellerini aşmak için `StealthHttpClient` (`curl_cffi`) kullanır.
    """

    def __init__(
        self,
        bot_token: str = config.TELEGRAM_BOT_TOKEN,
        chat_id: str = config.TELEGRAM_CHAT_ID,
        flood_sleep: float = config.TELEGRAM_FLOOD_SLEEP_SECONDS,
        http_client: Optional[StealthHttpClient] = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.flood_sleep = flood_sleep
        self._last_sent_ts: float = 0.0
        self.client = http_client or StealthHttpClient(min_delay=0.2, max_delay=0.5, max_retries=3, timeout=20)

    @property
    def is_configured(self) -> bool:
        return bool(
            self.bot_token
            and self.chat_id
            and "ornek_bot_token" not in self.bot_token
        )

    @staticmethod
    def format_tl(amount: float) -> str:
        """Sayısı Türkçe binlik noktalı formata çevirir (Örn: 2499.9 -> '2.500 TL')."""
        rounded = int(round(amount))
        return f"{rounded:,} TL".replace(",", ".")

    @classmethod
    def build_html_caption(
        cls,
        analysis: ArbitrageAnalysis,
        notification_type: str = "NEW_OPPORTUNITY",
    ) -> str:
        p = analysis.product

        if notification_type == "EXTRA_PRICE_CRASH":
            header = f"⚡ <b>EKSTRA FİYAT ÇÖKÜŞÜ! (Puan: {analysis.arbitrage_score}/100)</b>"
        else:
            header = f"🔥 <b>ARBİTRAJ FIRSATI (Puan: {analysis.arbitrage_score}/100)</b>"

        gender_lower = (p.gender or "").lower()
        if "kadın" in gender_lower or "kadin" in gender_lower:
            gender_badge = "🚺 Kadın"
        elif "erkek" in gender_lower:
            gender_badge = "🚹 Erkek"
        else:
            gender_badge = "🚻 Unisex"

        safe_site = html.escape(p.site_name)
        safe_brand = html.escape(p.brand)
        safe_title = html.escape(p.title)
        safe_sku = html.escape(p.display_sku)
        safe_campaign = html.escape(p.campaign_name or "Sepet İndirimi")

        size_labels = [html.escape(s.display_label) for s in p.sizes]
        sizes_text = ", ".join(size_labels) if size_labels else "Stokta"

        min_stock = p.min_stock_count
        if min_stock is not None and 1 <= min_stock <= 3:
            stock_warning = f" <b>⚠️ SON {min_stock} ADET!</b>"
        elif p.is_low_stock:
            stock_warning = " <b>⚠️ SON ADETLER!</b>"
        else:
            stock_warning = ""

        retail_str = cls.format_tl(p.retail_price)
        sale_str = cls.format_tl(p.sale_price)
        basket_str = cls.format_tl(p.net_basket_price)
        market_str = cls.format_tl(analysis.market_floor_price)
        sell_str = cls.format_tl(analysis.suggested_sell_price)

        profit_g_sign = "+" if analysis.net_profit_gardrops >= 0 else ""
        profit_g_str = f"{profit_g_sign}{int(round(analysis.net_profit_gardrops)):,} TL".replace(",", ".")

        profit_d_sign = "+" if analysis.net_profit_dolap >= 0 else ""
        profit_d_str = f"{profit_d_sign}{int(round(analysis.net_profit_dolap)):,} TL".replace(",", ".")

        safe_product_url = html.escape(p.url, quote=True)
        safe_akakce_url = html.escape(p.akakce_search_url, quote=True)

        lines = [
            f"🚨 {header}",
            "",
            f"🏪 <b>Kaynak Site &amp; Kategori:</b> [{safe_site}] | [{gender_badge}]",
            f"👟 <b>Marka &amp; Model:</b> {safe_brand} - {safe_title} (<code>SKU: {safe_sku}</code>)",
            f"📏 <b>Stoktaki Hedef Numaralar:</b> [{sizes_text}]{stock_warning}",
            "",
            "💳 <b>Alış Detayı:</b>",
            f"  • Liste Fiyatı: <s>{retail_str}</s> | Vitrin: <code>{sale_str}</code>",
            (
                f"  • 🛒 <b>Sepette Net Alış:</b> <b><code>{basket_str}</code></b> "
                f"<i>(Toplam İndirim: <b>%{p.total_discount_rate:.0f}</b> | Kampanya: {safe_campaign})</i>"
            ),
            "",
            "📊 <b>Piyasa Kıyaslaması:</b>",
            f"  • 🌐 Diğer Sitelerde En Ucuz: <code>~{market_str}</code>",
            f"  • 🎯 <b>Önerilen Satış Fiyatımız:</b> <b><code>{sell_str}</code></b>",
            "",
            "💰 <b>Tahmini Net Kâr Tablosu:</b>",
            (
                f"  • 🟢 <b>Gardrops / Sahibinden Net:</b> "
                f"<b><code>{profit_g_str}</code></b> (<b><code>%{analysis.roi_gardrops:.0f}</code> Kâr</b>)"
            ),
            (
                f"  • 🟣 <b>Dolap Net:</b> "
                f"<b><code>{profit_d_str}</code></b> (<b><code>%{analysis.roi_dolap:.0f}</code> Kâr</b>)"
            ),
            "",
            (
                f'🔗 <a href="{safe_product_url}">🛒 Ürünü Hemen Sepete Ekle</a> | '
                f'🔍 <a href="{safe_akakce_url}">📈 Akakçe\'de Piyasa Fiyatını Gör</a>'
            ),
        ]
        return "\n".join(lines)

    def _respect_flood_limit(self) -> None:
        elapsed = time.time() - self._last_sent_ts
        if self._last_sent_ts > 0 and elapsed < self.flood_sleep:
            time.sleep(self.flood_sleep - elapsed)
        self._last_sent_ts = time.time()

    def send_opportunity(
        self,
        analysis: ArbitrageAnalysis,
        notification_type: str = "NEW_OPPORTUNITY",
    ) -> bool:
        """
        Fırsat analizini Telegram'a fotoğraflı (`sendPhoto`) olarak gönderir.
        Fotoğraf gönderimi başarısız olursa otomatik olarak `sendMessage` ile gönderir.
        """
        caption = self.build_html_caption(analysis, notification_type=notification_type)

        if not self.is_configured:
            logger.warning("Telegram Token veya Chat ID tanımlı değil, mesaj gönderilmedi.")
            return False

        self._respect_flood_limit()
        base_api = f"https://api.telegram.org/bot{self.bot_token}"
        image_url: Optional[str] = analysis.product.image_url

        # 1. Görselli Gönderim (sendPhoto)
        if image_url and image_url.startswith("http"):
            try:
                photo_payload = {
                    "chat_id": self.chat_id,
                    "photo": image_url,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                }
                resp = self.client.post(f"{base_api}/sendPhoto", data=photo_payload, skip_throttle=True)
                if resp and resp.status_code == 200 and resp.json().get("ok"):
                    logger.info(
                        "Telegram fotoğraflı bildirim gönderildi: %s (%s)",
                        analysis.product.display_sku,
                        analysis.product.site_name,
                    )
                    return True
                logger.warning(
                    "sendPhoto başarısız oldu (HTTP %s: %s), sendMessage'a düşülüyor...",
                    resp.status_code if resp else "None",
                    resp.text[:160] if resp else "No response",
                )
            except Exception as exc:
                logger.warning("sendPhoto isteği sırasında hata (%s), sendMessage'a düşülüyor...", exc)

        # 2. Yedek Metin Gönderimi (sendMessage)
        try:
            msg_payload = {
                "chat_id": self.chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            }
            resp = self.client.post(f"{base_api}/sendMessage", data=msg_payload, skip_throttle=True)
            if resp and resp.status_code == 200 and resp.json().get("ok"):
                logger.info(
                    "Telegram metin bildirimi gönderildi: %s (%s)",
                    analysis.product.display_sku,
                    analysis.product.site_name,
                )
                return True
            logger.error(
                "Telegram sendMessage hatası (HTTP %s): %s",
                resp.status_code if resp else "None",
                resp.text[:250] if resp else "No response",
            )
        except Exception as exc:
            logger.error("Telegram sendMessage bağlantı hatası: %s", exc)

        return False
