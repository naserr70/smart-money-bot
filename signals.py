"""
Typed signal objects.

MarketSignal:
    CEX 5m closed-candle signal.

Volume logic:
    current closed candle volume compared against the
    simple arithmetic mean of the previous 48 closed candles.

Pump / dump:
    static price threshold + optional 72h statistical anomaly.
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

    baseline_volume_usd: float = 0.0

    current_candle_volume_usd: float = 0.0

    baseline_candles: int = 48

    def to_telegram(self) -> str:

        is_pump_labeled = (
            self.trigger
            in (
                TriggerType.STATISTICAL,
                TriggerType.BOTH,
            )
        )

        if self.direction == SignalDirection.INFLOW:

            if is_pump_labeled:

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
                "📈 <b>رشد قیمت کندل ۵ دقیقه‌ای:</b> "
                f"<code>+{self.change_5m:.2f}%</code>"
            )

            flow_label = (
                "ورود پول / حجم معامله‌شده"
            )

            advice = (
                "🎯 <b>توصیه:</b> بررسی چارت در "
                "تایم‌فریم ۱۵ دقیقه و ورود پله‌ای."
            )

        else:

            if is_pump_labeled:

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
                "📉 <b>افت قیمت کندل ۵ دقیقه‌ای:</b> "
                f"<code>{self.change_5m:.2f}%</code>"
            )

            flow_label = (
                "خروج پول / حجم معامله‌شده"
            )

            advice = (
                "🎯 <b>توصیه:</b> احتمال توزیع/خروج؛ "
                "احتیاط در نگهداری پوزیشن."
            )

        detection_line = ""

        if self.trigger == TriggerType.STATIC:

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> "
                "جهش حجم نسبت به میانگین "
                f"{self.baseline_candles} کندل قبلی\n"
            )

        elif self.trigger == TriggerType.STATISTICAL:

            z = (
                f"{self.zscore:.1f}σ"
                if self.zscore is not None
                else "N/A"
            )

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> "
                "ناهنجاری آماری نسبت به رفتار ۷۲ ساعت اخیر "
                f"(<code>{esc(z)}</code>)\n"
            )

        elif self.trigger == TriggerType.BOTH:

            z = (
                f"{self.zscore:.1f}σ"
                if self.zscore is not None
                else "N/A"
            )

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> "
                "جهش حجم + ناهنجاری آماری "
                f"(<code>{esc(z)}</code>)\n"
            )

        baseline_line = ""

        if self.baseline_volume_usd > 0:

            baseline_line = (
                "📏 <b>میانگین حجم ۴۸ کندل قبلی:</b> "
                f"<code>${self.baseline_volume_usd / 1e3:,.1f}K</code>\n"
            )

        return (
            f"{header}\n\n"

            f"🪙 <b>نماد:</b> "
            f"#{esc(self.symbol)} "
            f"<i>(موجود در نوبیتکس)</i>\n"

            f"💵 <b>قیمت جهانی:</b> "
            f"${self.price:,.8f}\n"

            f"📊 <b>تغییرات ۲۴ ساعته:</b> "
            f"<code>{self.change_24h:+.2f}%</code>\n\n"

            f"{price_line}\n"

            f"🔥 <b>{esc(flow_label)}:</b> "
            f"<code>${self.inflow_usd / 1e3:,.1f}K</code>\n"

            f"📊 <b>حجم کندل بسته‌شده:</b> "
            f"<code>${self.current_candle_volume_usd / 1e3:,.1f}K</code>\n"

            f"{baseline_line}"

            f"⚡ <b>جهش حجم معاملاتی:</b> "
            f"<code>{self.spike_multiplier:.2f}X</code> "
            f"نسبت به میانگین\n"

            f"{detection_line}\n"

            f"{advice}\n"

            f"<i>منبع: "
            f"5m Closed Candle Volume/Price Signal"
            f"</i>"
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
            "ETH": (
                "https://etherscan.io/tx/"
            ),
            "BSC": (
                "https://bscscan.com/tx/"
            ),
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

        if self.direction == SignalDirection.INFLOW:

            header = (
                "📥 <b>واریز نهنگ به صرافی "
                "(EXCHANGE INFLOW)</b> 📥"
            )

            advice = (
                "🎯 <b>توصیه:</b> احتمال افزایش "
                "فشار فروش؛ دارایی به کیف‌پول صرافی وارد شده."
            )

        else:

            header = (
                "📤 <b>برداشت نهنگ از صرافی "
                "(EXCHANGE OUTFLOW)</b> 📤"
            )

            advice = (
                "🎯 <b>توصیه:</b> احتمال کاهش عرضه؛ "
                "دارایی از صرافی خارج شده."
            )

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

            f'🔗 <a href="{esc(link)}">'
            f"مشاهده تراکنش</a>\n\n"

            f"{advice}\n"

            f"<i>منبع: تحلیل آن‌چین کیف‌پول‌های "
            f"شناخته‌شده صرافی</i>"
        )