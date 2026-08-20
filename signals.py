"""
Typed signal objects.

Signal formatting is intentionally minimal:
- No data-source disclosure.
- No detailed detection methodology.
- No z-score disclosure.
- No "Nobitex" labeling.
- Only information useful for acting on the signal.
"""

from dataclasses import dataclass
from enum import Enum

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
    """
    CEX market signal.

    The internal fields remain available to the bot, but the Telegram
    representation intentionally exposes only the essential information.
    """

    symbol: str
    price: float
    change_5m: float
    change_24h: float
    inflow_usd: float
    spike_multiplier: float
    direction: SignalDirection
    trigger: TriggerType = TriggerType.STATIC
    zscore: float = None

    def to_telegram(self) -> str:
        """
        Minimal user-facing signal message.

        Deliberately does NOT expose:
        - exchange/data source
        - detection algorithm
        - z-score
        - trigger type
        - internal statistical details
        - Nobitex availability wording
        """

        if self.direction == SignalDirection.INFLOW:
            header = "🚀 <b>پامپ شناسایی شد</b> 🚀"
            move_line = (
                f"📈 <b>رشد اخیر:</b> "
                f"<code>+{self.change_5m:.2f}%</code>"
            )
            money_label = "ورود پول"
            advice = "🎯 <b>بررسی چارت و ورود پله‌ای</b>"
        else:
            header = "💥 <b>دامپ شناسایی شد</b> 💥"
            move_line = (
                f"📉 <b>افت اخیر:</b> "
                f"<code>{self.change_5m:.2f}%</code>"
            )
            money_label = "خروج پول"
            advice = "🎯 <b>احتیاط در نگهداری پوزیشن</b>"

        return (
            f"{header}\n\n"
            f"🪙 <b>#{esc(self.symbol)}</b>\n"
            f"💵 <b>قیمت:</b> <code>${self.price:,.4f}</code>\n"
            f"📊 <b>تغییر ۲۴ساعته:</b> "
            f"<code>{self.change_24h:+.2f}%</code>\n\n"
            f"{move_line}\n"
            f"🔥 <b>{esc(money_label)}:</b> "
            f"<code>${self.inflow_usd / 1e3:,.1f}K</code>\n"
            f"⚡ <b>جهش حجم:</b> "
            f"<code>{self.spike_multiplier:.1f}X</code>\n\n"
            f"{advice}"
        )


@dataclass
class ExchangeFlowSignal:
    """
    On-chain exchange-wallet flow signal.

    This remains separate from MarketSignal.
    """

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
            "TRON": "https://tronscan.org/#/transaction/",
        }.get(self.chain, "")

        link = f"{explorer}{self.tx_hash}" if explorer else self.tx_hash

        if self.direction == SignalDirection.INFLOW:
            header = "📥 <b>واریز نهنگ به صرافی</b> 📥"
            advice = (
                "🎯 <b>احتمال افزایش فشار فروش؛ "
                "احتیاط کنید.</b>"
            )
        else:
            header = "📤 <b>برداشت نهنگ از صرافی</b> 📤"
            advice = (
                "🎯 <b>احتمال کاهش عرضه در بازار؛ "
                "بررسی بیشتر توصیه می‌شود.</b>"
            )

        return (
            f"{header}\n\n"
            f"⛓ <b>شبکه:</b> "
            f"<code>{esc(self.chain)}</code>\n"
            f"🪙 <b>دارایی:</b> "
            f"<code>#{esc(self.token_symbol)}</code>\n"
            f"🏦 <b>صرافی:</b> "
            f"{esc(self.exchange_name)}\n"
            f"📦 <b>مقدار:</b> "
            f"<code>{self.amount_native:,.4f} "
            f"{esc(self.token_symbol)}</code>\n"
            f"💰 <b>ارزش:</b> "
            f"<code>${self.amount_usd:,.0f}</code>\n"
            f'🔗 <a href="{esc(link)}">مشاهده تراکنش</a>\n\n'
            f"{advice}"
        )