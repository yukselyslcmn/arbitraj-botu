import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.arbitrage_engine import ArbitrageAnalysis


@dataclass
class QuickFlipListingTemplate:
    """Gardrops, Dolap ve Sahibinden (Param Güvende) için hazır ilan şablonu."""

    title: str
    description: str
    recommended_price_tl: int
    min_acceptable_price_tl: int
    tags: list[str]


class ListingGenerator:
    """
    Yakalanan arbitraj fırsatları için Gardrops, Dolap ve Sahibinden'e
    tek tıkla kopyalanıp yapıştırılabilecek SEO uyumlu ilan başlığı, açıklama
    ve fiyatlandırma şablonu üreten motor.
    """

    @classmethod
    def generate(cls, analysis: "ArbitrageAnalysis") -> QuickFlipListingTemplate:
        p = analysis.product
        sizes_list = [s.normalized_size for s in p.sizes]
        sizes_short = "/".join(sizes_list[:4]) if sizes_list else "Standart"

        clean_model = p.title
        if p.brand.lower() in clean_model.lower():
            # Başlıkta marka tekrarını önle
            title_core = clean_model
        else:
            title_core = f"{p.brand} {clean_model}"

        raw_title = f"Sıfır Faturalı {title_core} ({p.display_sku}) No:{sizes_short}"
        if len(raw_title) > 70:
            raw_title = f"Sıfır Faturalı {p.brand} ({p.display_sku}) No:{sizes_short}"

        rec_price = int(round(analysis.suggested_sell_price))
        # Pazarlık payı sonrası minimum kabul edilebilir fiyat (Maliyet + Kargo + %18 net marj)
        min_floor = int(round((analysis.buy_cost * 1.18 + 95.0) / 0.94))
        min_acceptable = min(rec_price, max(min_floor, int(round(rec_price * 0.93))))

        brand_tag = p.brand.lower().replace(" ", "")
        tags = [
            f"#{brand_tag}",
            "#orijinal",
            "#faturali",
            "#sifirkutusunda",
            "#sneaker",
            f"#{p.display_sku.lower().replace('-', '')}",
        ]

        desc_lines = [
            f"✅ %100 Orijinal & {p.site_name} Yetkili Distribütör Faturalı",
            f"👟 Model: {title_core}",
            f"🏷️ Ürün Kodu (SKU): {p.display_sku}",
            f"📏 Mevcut Numaralar: {', '.join(s.display_label for s in p.sizes) or 'Stokta'}",
            f"📊 Piyasa Satış Fiyatı: ~{int(round(analysis.market_floor_price)):,} TL".replace(",", "."),
            "📦 Durum: Sıfır, orijinal kutusunda, etiketli ve hiç giyilmemiştir.",
            "🚀 Kargo: Aynı gün veya ertesi iş günü özenli çift kutu paketleme ile gönderilir.",
            " ".join(tags),
        ]

        return QuickFlipListingTemplate(
            title=raw_title,
            description="\n".join(desc_lines),
            recommended_price_tl=rec_price,
            min_acceptable_price_tl=min_acceptable,
            tags=tags,
        )

    @classmethod
    def build_telegram_copy_block(cls, analysis: "ArbitrageAnalysis") -> str:
        """Telegram HTML mesajı içine eklenecek tek tıkla kopyalanabilir ilan bloğu."""
        tpl = cls.generate(analysis)
        safe_title = html.escape(tpl.title)
        safe_desc = html.escape(
            f"{tpl.title}\n"
            f"Fiyat: {tpl.recommended_price_tl:,} TL (Min Pazarlık: {tpl.min_acceptable_price_tl:,} TL)\n"
            f"{tpl.description}".replace(",", ".")
        )
        return (
            "📋 <b>Hazır İlan Şablonu (Tek Tıkla Kopyala):</b>\n"
            f"  • <b>Başlık:</b> <code>{safe_title}</code>\n"
            f"<pre>{safe_desc}</pre>"
        )
