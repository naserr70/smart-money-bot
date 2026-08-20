"""
Typed market and exchange-flow signal objects.

Telegram messages are designed for quick scan on mobile:
clear header, compact metrics, actionable footer.
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


def _fmt_price(price: float) -> str:
    """Readable price: more decimals for cheap alts."""

    abs_p = abs(price)

    if abs_p >= 1000:
        return f"${price:,.2f}"
    if abs_p >= 1:
        return f"${price:,.4f}"
    if abs_p >= 0.01:
        return f"${price:.6f}"
    return f"${price:.8f}"


def _fmt_usd_flow(usd: float) -> str:

    abs_u = abs(usd)

    if abs_u >= 1_000_000:
        return f"${usd / 1e6:,.2f}M"
    if abs_u >= 1_000:
        return f"${usd / 1e3:,.1f}K"
    return f"${usd:,.0f}"


def _tradingview_url(symbol: str, source: str = "") -> str:

    clean = symbol.upper().replace("-", "").replace("/", "")

    exchange = {
        "binance": "BINANCE",
        "bybit": "BYBIT",
        "kucoin": "KUCOIN",
    }.get((source or "").lower(), "BINANCE")

    return (
        "https://www.tradingview.com/chart/"
        f"?symbol={exchange}%3A{clean}"
    )


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

    # Internal / optional display helpers
    source: str = ""
    # "smart_money_48" | "pump_dump_72h"
    path: str = ""

    def _baseline_label(self) -> str:

        if self.path == "pump_dump_72h":
            return "میانگین ۷۲ ساعت"

        if self.path == "smart_money_48":
            return "میانگین ۴ ساعت (۴۸ کندل)"

        return "میانگین اخیر"

    def _kind_meta(self) -> tuple:
        """
        Returns (emoji, title_fa, title_en, flow_label, advice).
        """

        is_inflow = self.direction == SignalDirection.INFLOW
        is_stat = self.trigger in (
            TriggerType.STATISTICAL,
            TriggerType.BOTH,
        )
        is_pump_path = self.path == "pump_dump_72h"

        if is_inflow:

            if is_pump_path or is_stat:
                return (
                    "🚀",
                    "پامپ شناسایی شد",
                    "PUMP",
                    "حجم مازاد",
                    "چارت ۱۵م را چک کنید؛ ورود پله‌ای و حد ضرر مشخص.",
                )

            return (
                "🟢",
                "ورود پول هوشمند",
                "SMART MONEY IN",
                "ورود پول خالص",
                "احتمال ورود جریان خرید؛ تأیید با ساختار قیمت.",
            )

        if is_pump_path or is_stat:
            return (
                "💥",
                "دامپ شناسایی شد",
                "DUMP",
                "حجم مازاد",
                "احتمال فشار فروش؛ احتیاط در نگهداری پوزیشن لانگ.",
            )

        return (
            "🔴",
            "خروج پول هوشمند",
            "SMART MONEY OUT",
            "خروج پول خالص",
            "احتمال توزیع؛ حجم فروش نسبت به میانگین بالاتر است.",
        )

    def to_telegram(self) -> str:

        emoji, title_fa, title_en, flow_label, advice = (
            self._kind_meta()
        )

        is_inflow = self.direction == SignalDirection.INFLOW
        price_emoji = "📈" if is_inflow else "📉"
        change_5m_txt = f"{self.change_5m:+.2f}%"
        change_24h_txt = f"{self.change_24h:+.2f}%"

        source_label = {
            "binance": "Binance",
            "bybit": "Bybit",
            "kucoin": "KuCoin",
        }.get((self.source or "").lower(), "")

        trigger_label = {
            TriggerType.STATIC: "حجمی",
            TriggerType.STATISTICAL: "آماری",
            TriggerType.BOTH: "حجمی + آماری",
        }.get(self.trigger, "—")

        lines = [
            f"{emoji} <b>{esc(title_fa)}</b>",
            f"<code>{esc(title_en)}</code>",
            "",
            f"🪙 <b>نماد:</b> #{esc(self.symbol)}",
            f"💵 <b>قیمت:</b> <code>{_fmt_price(self.price)}</code>",
            (
                f"{price_emoji} <b>۵ دقیقه:</b> "
                f"<code>{change_5m_txt}</code>"
            ),
            (
                f"📊 <b>۲۴ ساعت:</b> "
                f"<code>{change_24h_txt}</code>"
            ),
            "",
            (
                f"⚡ <b>جهش حجم:</b> "
                f"<code>{self.spike_multiplier:.2f}×</code>"
            ),
            (
                f"📐 <b>مرجع:</b> "
                f"{esc(self._baseline_label())}"
            ),
            (
                f"💰 <b>{esc(flow_label)}:</b> "
                f"<code>{_fmt_usd_flow(self.inflow_usd)}</code>"
            ),
        ]

        if self.zscore is not None:
            lines.append(
                f"📉 <b>Z-Score:</b> "
                f"<code>{self.zscore:+.2f}</code>"
            )

        lines.append(
            f"🧪 <b>نوع تشخیص:</b> {esc(trigger_label)}"
        )

        if source_label:
            lines.append(
                f"🌐 <b>منبع داده:</b> {esc(source_label)}"
            )

        tv = _tradingview_url(self.symbol, self.source)

        lines.extend(
            [
                "",
                (
                    f'🔗 <a href="{esc(tv)}">'
                    "چارت TradingView</a>"
                ),
                "",
                f"🎯 <b>نکته:</b> {esc(advice)}",
                "<i>نوبیتکس · تحلیل حجم ۵م</i>",
            ]
        )

        return "\n".join(lines)


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
            "TRON": "https://tronscan.org/#/transaction/",
        }.get(self.chain, "")

        link = (
            f"{explorer}{self.tx_hash}"
            if explorer
            else self.tx_hash
        )

        is_in = self.direction == SignalDirection.INFLOW

        if is_in:
            emoji = "📥"
            title_fa = "واریز نهنگ به صرافی"
            title_en = "EXCHANGE INFLOW"
            advice = (
                "افزایش احتمال فشار فروش؛ "
                "دارایی وارد کیف صرافی شده است."
            )
        else:
            emoji = "📤"
            title_fa = "برداشت نهنگ از صرافی"
            title_en = "EXCHANGE OUTFLOW"
            advice = (
                "کاهش عرضه در بازار محتمل؛ "
                "خروج به کیف شخصی/کلد."
            )

        chain_label = {
            "ETH": "Ethereum",
            "BSC": "BNB Chain",
            "TRON": "TRON",
        }.get(self.chain, self.chain)

        lines = [
            f"{emoji} <b>{esc(title_fa)}</b>",
            f"<code>{esc(title_en)}</code>",
            "",
            f"⛓ <b>شبکه:</b> <code>{esc(chain_label)}</code>",
            f"🪙 <b>دارایی:</b> #{esc(self.token_symbol)}",
            f"🏦 <b>صرافی:</b> {esc(self.exchange_name)}",
            (
                f"📦 <b>مقدار:</b> "
                f"<code>{self.amount_native:,.4f} "
                f"{esc(self.token_symbol)}</code>"
            ),
            (
                f"💰 <b>ارزش:</b> "
                f"<code>{_fmt_usd_flow(self.amount_usd)}</code>"
            ),
            "",
            f'🔗 <a href="{esc(link)}">مشاهده تراکنش</a>',
            "",
            f"🎯 <b>نکته:</b> {esc(advice)}",
            "<i>ردیابی آن‌چین · ولت‌های صرافی</i>",
        ]

        return "\n".join(lines)
