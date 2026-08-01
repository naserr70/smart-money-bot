"""
Typed signal objects. Keeping message formatting on the dataclass itself
(rather than scattered f-strings in the analyzer) makes both easier to test
and to change independently.
"""
from dataclasses import dataclass
from enum import Enum


class SignalDirection(Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


@dataclass
class MarketSignal:
    """A CEX ticker-based signal: unusual 24h-volume-delta + price move over one poll cycle."""

    symbol: str
    price: float
    change_5m: float
    change_24h: float
    inflow_usd: float
    spike_multiplier: float
    direction: SignalDirection

    def to_telegram(self) -> str:
        if self.direction == SignalDirection.INFLOW:
            header = "🚨 *ورود پول هوشمند (SMART MONEY IN)* 🚨"
            price_line = f"📈 *رشد قیمت اخیر:* `+{self.change_5m:.2f}%`"
            flow_label = "ورود پول خالص"
            advice = "🎯 *توصیه:* بررسی چارت در تایم‌فریم ۱۵ دقیقه و ورود پله‌ای."
        else:
            header = "🔻 *خروج پول هوشمند (SMART MONEY OUT)* 🔻"
            price_line = f"📉 *افت قیمت اخیر:* `{self.change_5m:.2f}%`"
            flow_label = "خروج پول خالص (تخمینی)"
            advice = "🎯 *توصیه:* احتمال توزیع/خروج نهنگ؛ احتیاط در نگهداری پوزیشن."

        return (
            f"{header}\n\n"
            f"🪙 *نماد:* #{self.symbol} _(موجود در نوبیتکس)_\n"
            f"💵 *قیمت جهانی:* ${self.price:,.4f}\n"
            f"📊 *تغییرات ۲۴ ساعته:* `{self.change_24h:+.2f}%`\n\n"
            f"{price_line}\n"
            f"🔥 *{flow_label}:* `${self.inflow_usd/1e3:,.1f}K`\n"
            f"⚡ *جهش حجم معاملاتی:* `{self.spike_multiplier:.1f}X` برابر میانگین واقعی\n\n"
            f"{advice}\n"
            f"_منبع: تیکر صرافی (Volume/Price Ticker Signal)_"
        )


@dataclass
class ExchangeFlowSignal:
    """An on-chain signal: a large transfer into/out of a known exchange wallet.
    Independent from, and complementary to, the ticker-based MarketSignal above."""

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
        }.get(self.chain, "")
        link = f"{explorer}{self.tx_hash}" if explorer else self.tx_hash

        if self.direction == SignalDirection.INFLOW:
            header = "📥 *واریز نهنگ به صرافی (EXCHANGE INFLOW)* 📥"
            advice = "🎯 *توصیه:* احتمال افزایش فشار فروش؛ دارایی در حال واریز به کیف‌پول صرافی است."
        else:
            header = "📤 *برداشت نهنگ از صرافی (EXCHANGE OUTFLOW)* 📤"
            advice = "🎯 *توصیه:* احتمال کاهش عرضه در بازار؛ دارایی در حال خروج به کیف شخصی/کلد استوریج است."

        return (
            f"{header}\n\n"
            f"⛓ *شبکه:* `{self.chain}`\n"
            f"🪙 *دارایی:* #{self.token_symbol}\n"
            f"🏦 *صرافی:* {self.exchange_name}\n"
            f"📦 *مقدار:* `{self.amount_native:,.4f} {self.token_symbol}`\n"
            f"💰 *ارزش تراکنش:* ${self.amount_usd:,.0f}\n"
            f"🔗 [مشاهده تراکنش]({link})\n\n"
            f"{advice}\n"
            f"_منبع: تحلیل آن‌چین کیف‌پول‌های شناخته‌شده صرافی (On-chain Wallet Signal)_"
        )
