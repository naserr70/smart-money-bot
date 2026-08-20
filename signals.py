"""
Typed market and exchange-flow signal objects.

Telegram output intentionally exposes only user-facing information.
Internal exchange/source/debug information is never displayed.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from formatting import esc


class SignalDirection(Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


class TriggerType(Enum):
    STATIC = "static"
    STATISTICAL = "statistical"
    BOTH = "both"


@dataclass
class MarketSignal:

    symbol: str
    price: float
    change_5m: float
    change_24h: float
    inflow_usd: float
    spike_multiplier: float
    direction: SignalDirection

    trigger: TriggerType = TriggerType.STATIC
    zscore: Optional[float] = None

    # Internal only.
    source: str = ""

    def to_telegram(self) -> str:

        is_inflow = (
            self.direction
            == SignalDirection.INFLOW
        )

        is_statistical = (
            self.trigger
            in (
                TriggerType.STATISTICAL,
                TriggerType.BOTH,
            )
        )

        if is_inflow:

            if is_statistical:
                header = (
                    "🚀 <b>پامپ شناسایی شد "
                    "(PUMP DETECTED)</b> 🚀"
                )
            else:
                header = (
                    "🚨 <b>ورود پول هوشمند "
                    "(SMART MONEY IN)</b> 🚨"
                )

            price_line = (
                "📈 <b>رشد قیمت اخیر:</b> "
                f"<code>+{self.change_5m:.2f}%</code>"
            )

            flow_label = "ورود پول خالص"

            advice = (
                "🎯 <b>توصیه:</b> "
                "بررسی چارت در تایم‌فریم ۱۵ دقیقه "
                "و ورود پله‌ای."
            )

        else:

            if is_statistical:
                header = (
                    "💥 <b>دامپ ناگهانی شناسایی شد "
                    "(SUDDEN DUMP DETECTED)</b> 💥"
                )
            else:
                header = (
                    "🔻 <b>خروج پول هوشمند "
                    "(SMART MONEY OUT)</b> 🔻"
                )

            price_line = (
                "📉 <b>افت قیمت اخیر:</b> "
                f"<code>{self.change_5m:.2f}%</code>"
            )

            flow_label = "خروج پول خالص"

            advice = (
                "🎯 <b>توصیه:</b> "
                "احتمال توزیع/خروج نهنگ؛ "
                "احتیاط در نگهداری پوزیشن."
            )

        return (
            f"{header}\n\n"
            f"🪙 <b>نماد:</b> "
            f"#{esc(self.symbol)} "
            f"<i>(موجود در نوبیتکس)</i>\n"
            f"💵 <b>قیمت جهانی:</b> "
            f"${self.price:,.4f}\n"
            f"📊 <b>تغییرات ۲۴ ساعته:</b> "
            f"<code>{self.change_24h:+.2f}%</code>\n\n"
            f"{price_line}\n"
            f"🔥 <b>{esc(flow_label)}:</b> "
            f"<code>${self.inflow_usd / 1e3:,.1f}K</code>\n"
            f"⚡ <b>جهش حجم معاملاتی:</b> "
            f"<code>{self.spike_multiplier:.1f}X</code> "
            "برابر میانگین ۴ ساعت گذشته\n\n"
            f"{advice}"
        )


@dataclass
class ExchangeFlowSignal:

    chain: str
    token_symbol: str
    exchange_name: str
    amount_usd: float
    amount_native: float
    tx_hash: str
    direction: SignalDirection

    def to_telegram(self) -> str:

        explorer = {
            "ETH": "https://etherscan.io/tx/",
            "BSC": "https://bscscan.com/tx/",
            "TRON": (
                "https://tronscan.org/#/transaction/"
            ),
        }.get(
            self.chain,
            "",
        )

        link = (
            f"{explorer}{self.tx_hash}"
            if explorer
            else self.tx_hash
        )

        if (
            self.direction
            == SignalDirection.INFLOW
        ):

            header = (
                "📥 <b>واریز نهنگ به صرافی "
                "(EXCHANGE INFLOW)</b> 📥"
            )

            advice = (
                "🎯 <b>توصیه:</b> "
                "احتمال افزایش فشار فروش؛ "
                "دارایی در حال واریز به کیف‌پول صرافی است."
            )

        else:

            header = (
                "📤 <b>برداشت نهنگ از صرافی "
                "(EXCHANGE OUTFLOW)</b> 📤"
            )

            advice = (
                "🎯 <b>توصیه:</b> "
                "احتمال کاهش عرضه در بازار؛ "
                "دارایی در حال خروج به کیف شخصی/کلد است."
            )

        safe_link = esc(link)

        return (
            f"{header}\n\n"
            f"⛓ <b>شبکه:</b> "
            f"<code>{esc(self.chain)}</code>\n"
            f"🪙 <b>دارایی:</b> "
            f"#{esc(self.token_symbol)}\n"
            f"🏦 <b>صرافی:</b> "
            f"{esc(self.exchange_name)}\n"
            f"📦 <b>مقدار:</b> "
            f"<code>{self.amount_native:,.4f} "
            f"{esc(self.token_symbol)}</code>\n"
            f"💰 <b>ارزش تراکنش:</b> "
            f"${self.amount_usd:,.0f}\n"
            f'🔗 <a href="{safe_link}">'
            "مشاهده تراکنش</a>\n\n"
            f"{advice}"
        )