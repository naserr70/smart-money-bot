"""
Typed signal objects. Keeping message formatting on the dataclass itself
(rather than scattered f-strings in the analyzer) makes both easier to test
and to change independently.

Messages use Telegram's HTML parse_mode (not Markdown) — Markdown's legacy
parser breaks ("can't parse entities") on unescaped '_', '*', '`' in any
interpolated text, which realistically shows up in token symbols from
external APIs, exchange labels, etc. Any non-literal text here goes through
formatting.esc() before being placed inside a tag.
"""
from dataclasses import dataclass
from enum import Enum

from formatting import esc


class SignalDirection(Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


class TriggerType(Enum):
    """Why a MarketSignal fired — used purely for labeling/wording, not logic.
    STATIC: only the fixed PRICE_PUMP_MIN/MAX %% threshold was crossed.
    STATISTICAL: only the z-score anomaly (relative to this coin's own recent
    behavior) crossed PUMP_ZSCORE_THRESHOLD — this is the actual "پامپ"
    detector; it fires on moves that are unusual FOR THAT COIN even if small
    in absolute %%.
    BOTH: both conditions were true at once (the strongest case)."""
    STATIC = "static"
    STATISTICAL = "statistical"
    BOTH = "both"


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
    trigger: TriggerType = TriggerType.STATIC
    zscore: float = None

    def to_telegram(self) -> str:
        is_pump_labeled = self.trigger in (TriggerType.STATISTICAL, TriggerType.BOTH)

        if self.direction == SignalDirection.INFLOW:
            if is_pump_labeled:
                header = "🚀 <b>پامپ شناسایی شد (PUMP DETECTED)</b> 🚀"
            else:
                header = "🚨 <b>ورود پول هوشمند (SMART MONEY IN)</b> 🚨"
            price_line = f"📈 <b>رشد قیمت اخیر:</b> <code>+{self.change_5m:.2f}%</code>"
            flow_label = "ورود پول خالص"
            advice = "🎯 <b>توصیه:</b> بررسی چارت در تایم‌فریم ۱۵ دقیقه و ورود پله‌ای."
        else:
            if is_pump_labeled:
                header = "💥 <b>دامپ ناگهانی شناسایی شد (SUDDEN DUMP DETECTED)</b> 💥"
            else:
                header = "🔻 <b>خروج پول هوشمند (SMART MONEY OUT)</b> 🔻"
            price_line = f"📉 <b>افت قیمت اخیر:</b> <code>{self.change_5m:.2f}%</code>"
            flow_label = "خروج پول خالص (تخمینی)"
            advice = "🎯 <b>توصیه:</b> احتمال توزیع/خروج نهنگ؛ احتیاط در نگهداری پوزیشن."

        detection_line = ""
        if self.trigger == TriggerType.STATIC:
            detection_line = "🔎 <b>نوع تشخیص:</b> عبور از آستانه‌ی ثابت درصدی\n"
        elif self.trigger == TriggerType.STATISTICAL:
            z = f"{self.zscore:.1f}σ" if self.zscore is not None else "N/A"
            detection_line = f"🔎 <b>نوع تشخیص:</b> ناهنجاری آماری نسبت به رفتار عادی خودِ این ارز (<code>{esc(z)}</code>)\n"
        elif self.trigger == TriggerType.BOTH:
            z = f"{self.zscore:.1f}σ" if self.zscore is not None else "N/A"
            detection_line = f"🔎 <b>نوع تشخیص:</b> هم آستانه‌ی ثابت، هم ناهنجاری آماری (<code>{esc(z)}</code>)\n"

        return (
            f"{header}\n\n"
            f"🪙 <b>نماد:</b> #{esc(self.symbol)} <i>(موجود در نوبیتکس)</i>\n"
            f"💵 <b>قیمت جهانی:</b> ${self.price:,.4f}\n"
            f"📊 <b>تغییرات ۲۴ ساعته:</b> <code>{self.change_24h:+.2f}%</code>\n\n"
            f"{price_line}\n"
            f"🔥 <b>{esc(flow_label)}:</b> <code>${self.inflow_usd/1e3:,.1f}K</code>\n"
            f"⚡ <b>جهش حجم معاملاتی:</b> <code>{self.spike_multiplier:.1f}X</code> برابر میانگین واقعی\n"
            f"{detection_line}\n"
            f"{advice}\n"
            f"<i>منبع: تیکر صرافی (Volume/Price Ticker Signal)</i>"
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
            "TRON": "https://tronscan.org/#/transaction/",
        }.get(self.chain, "")
        link = f"{explorer}{self.tx_hash}" if explorer else self.tx_hash

        if self.direction == SignalDirection.INFLOW:
            header = "📥 <b>واریز نهنگ به صرافی (EXCHANGE INFLOW)</b> 📥"
            advice = "🎯 <b>توصیه:</b> احتمال افزایش فشار فروش؛ دارایی در حال واریز به کیف‌پول صرافی است."
        else:
            header = "📤 <b>برداشت نهنگ از صرافی (EXCHANGE OUTFLOW)</b> 📤"
            advice = "🎯 <b>توصیه:</b> احتمال کاهش عرضه در بازار؛ دارایی در حال خروج به کیف شخصی/کلد استوریج است."

        return (
            f"{header}\n\n"
            f"⛓ <b>شبکه:</b> <code>{esc(self.chain)}</code>\n"
            f"🪙 <b>دارایی:</b> #{esc(self.token_symbol)}\n"
            f"🏦 <b>صرافی:</b> {esc(self.exchange_name)}\n"
            f"📦 <b>مقدار:</b> <code>{self.amount_native:,.4f} {esc(self.token_symbol)}</code>\n"
            f"💰 <b>ارزش تراکنش:</b> ${self.amount_usd:,.0f}\n"
            f'🔗 <a href="{esc(link)}">مشاهده تراکنش</a>\n\n'
            f"{advice}\n"
            f"<i>منبع: تحلیل آن‌چین کیف‌پول‌های شناخته‌شده صرافی (On-chain Wallet Signal)</i>"
        )
