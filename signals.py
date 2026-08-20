"""
Typed signal objects.

MarketSignal represents CEX ticker-based signals.

The signal contains:
  * actual current price
  * current-cycle price change
  * 24h price change
  * calculated volume inflow/outflow
  * volume spike multiplier
  * signal direction
  * detection method
  * optional statistical z-score

The analyzer is responsible for the actual detection logic.
This module only represents and formats the result.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from formatting import esc


class SignalDirection(Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


class TriggerType(Enum):
    """
    Why a MarketSignal fired.

    STATIC:
        Fixed percentage + volume-spike conditions.

    STATISTICAL:
        Price movement is statistically abnormal compared with
        the symbol's long-term 72-hour behavior.

    BOTH:
        Both detection systems agree.
    """

    STATIC = "static"
    STATISTICAL = "statistical"
    BOTH = "both"


@dataclass
class MarketSignal:
    """
    CEX ticker-based smart-money signal.

    Volume:
        Calculated from the change in quote volume between two
        market snapshots.

    Smart-money baseline:
        48 historical 5-minute candles = 4 hours.

    Pump/Dump statistical baseline:
        864 historical 5-minute candles = 72 hours.
    """

    symbol: str

    price: float

    # Current scan-cycle price movement.
    change_5m: float

    # Exchange-reported 24h price movement.
    change_24h: float

    # Current volume delta / estimated money flow.
    inflow_usd: float

    # Current flow relative to historical baseline.
    spike_multiplier: float

    direction: SignalDirection

    trigger: TriggerType = TriggerType.STATIC

    # Price anomaly relative to 72h history.
    zscore: Optional[float] = None

    # Number of historical candles used for volume comparison.
    volume_baseline_candles: int = 48

    # Number of historical candles used for statistical pump/dump detection.
    price_baseline_candles: int = 864

    def to_telegram(self) -> str:

        is_pump_labeled = (
            self.trigger
            in (
                TriggerType.STATISTICAL,
                TriggerType.BOTH,
            )
        )

        # --------------------------------------------------------------
        # Direction
        # --------------------------------------------------------------

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
                f"📈 <b>رشد قیمت اخیر:</b> "
                f"<code>+{self.change_5m:.2f}%</code>"
            )

            flow_label = "ورود پول خالص"

            advice = (
                "🎯 <b>توصیه:</b> بررسی چارت در تایم‌فریم "
                "۱۵ دقیقه و ورود پله‌ای."
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
                f"📉 <b>افت قیمت اخیر:</b> "
                f"<code>{self.change_5m:.2f}%</code>"
            )

            flow_label = "خروج پول خالص (تخمینی)"

            advice = (
                "🎯 <b>توصیه:</b> احتمال توزیع/خروج نهنگ؛ "
                "احتیاط در نگهداری پوزیشن."
            )

        # --------------------------------------------------------------
        # Detection information
        # --------------------------------------------------------------

        detection_line = ""

        if self.trigger == TriggerType.STATIC:

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> عبور از آستانه ثابت "
                "درصدی + جهش حجم\n"
            )

        elif self.trigger == TriggerType.STATISTICAL:

            z = (
                f"{self.zscore:.1f}σ"
                if self.zscore is not None
                else "N/A"
            )

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> ناهنجاری آماری قیمت "
                "نسبت به رفتار ۷۲ ساعت اخیر "
                f"(<code>{esc(z)}</code>)\n"
            )

        elif self.trigger == TriggerType.BOTH:

            z = (
                f"{self.zscore:.1f}σ"
                if self.zscore is not None
                else "N/A"
            )

            detection_line = (
                "🔎 <b>نوع تشخیص:</b> هم آستانه ثابت و هم "
                "ناهنجاری آماری "
                f"(<code>{esc(z)}</code>)\n"
            )

        # --------------------------------------------------------------
        # Final message
        # --------------------------------------------------------------

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
            f"برابر میانگین "
            f"{self.volume_baseline_candles} کندل گذشته\n"

            f"📚 <b>دوره بررسی حجم:</b> "
            f"<code>"
            f"{self.volume_baseline_candles * 5 / 60:.1f}"
            f" ساعت"
            f"</code>\n"

            f"📈 <b>دوره بررسی پامپ/دامپ:</b> "
            f"<code>"
            f"{self.price_baseline_candles * 5 / 60:.0f}"
            f" ساعت"
            f"</code>\n"

            f"{detection_line}\n"

            f"{advice}\n"

            f"<i>"
            f"منبع: تیکر صرافی "
            f"(Volume/Price Ticker Signal)"
            f"</i>"
        )


@dataclass
class ExchangeFlowSignal:
    """
    On-chain signal:
    large transfer into/out of a known exchange wallet.

    This signal is completely independent from MarketSignal.
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
        }.get(
            self.chain,
            "",
        )

        link = (
            f"{explorer}{self.tx_hash}"
            if explorer
            else self.tx_hash
        )

        # --------------------------------------------------------------
        # Direction
        # --------------------------------------------------------------

        if self.direction == SignalDirection.INFLOW:

            header = (
                "📥 <b>واریز نهنگ به صرافی "
                "(EXCHANGE INFLOW)</b> 📥"
            )

            advice = (
                "🎯 <b>توصیه:</b> احتمال افزایش فشار فروش؛ "
                "دارایی در حال واریز به کیف‌پول صرافی است."
            )

        else:

            header = (
                "📤 <b>برداشت نهنگ از صرافی "
                "(EXCHANGE OUTFLOW)</b> 📤"
            )

            advice = (
                "🎯 <b>توصیه:</b> احتمال کاهش عرضه در بازار؛ "
                "دارایی در حال خروج به کیف شخصی/کلد استوریج است."
            )

        # --------------------------------------------------------------
        # Final message
        # --------------------------------------------------------------

        return (
            f"{header}\n\n"

            f"⛓ <b>شبکه:</b> "
            f"<code>{esc(self.chain)}</code>\n"

            f"🪙 <b>دارایی:</b> "
            f"#{esc(self.token_symbol)}\n"

            f"🏦 <b>صرافی:</b> "
            f"{esc(self.exchange_name)}\n"

            f"📦 <b>مقدار:</b> "
            f"<code>"
            f"{self.amount_native:,.4f} "
            f"{esc(self.token_symbol)}"
            f"</code>\n"

            f"💰 <b>ارزش تراکنش:</b> "
            f"${self.amount_usd:,.0f}\n"

            f'🔗 <a href="{esc(link)}">'
            f"مشاهده تراکنش"
            f"</a>\n\n"

            f"{advice}\n"

            f"<i>"
            f"منبع: تحلیل آن‌چین کیف‌پول‌های شناخته‌شده "
            f"صرافی (On-chain Wallet Signal)"
            f"</i>"
        )