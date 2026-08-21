"""Typed signal models and Telegram rendering."""

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
    p = abs(price)
    if p >= 1000:
        return f"${price:,.2f}"
    if p >= 1:
        return f"${price:,.4f}"
    if p >= 0.01:
        return f"${price:.6f}"
    return f"${price:.8f}"


def _fmt_usd_flow(usd: float) -> str:
    value = abs(usd)
    if value >= 1_000_000:
        return f"${usd / 1e6:,.2f}M"
    if value >= 1_000:
        return f"${usd / 1e3:,.1f}K"
    return f"${usd:,.0f}"


def _tradingview_url(symbol: str, source: str = "") -> str:
    clean = symbol.upper().replace("-", "").replace("/", "")
    exchange = {"binance": "BINANCE", "bybit": "BYBIT", "kucoin": "KUCOIN"}.get(source.lower(), "BINANCE")
    return f"https://www.tradingview.com/chart/?symbol={exchange}%3A{clean}"


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
    source: str = ""
    path: str = ""

    def _kind_meta(self) -> tuple:
        incoming = self.direction == SignalDirection.INFLOW
        anomaly = self.path in {"pump_dump_72h", "volume_anomaly_48"}
        if incoming and self.path == "pump_dump_72h":
            return "🚀", "پامپ/فشار خرید غیرعادی شناسایی شد", "PUMP / BUYING ANOMALY", "حجم مازاد", "این سیگنال بر پایه حجم و قیمت است؛ ساختار بازار را تأیید کنید."
        if not incoming and self.path == "pump_dump_72h":
            return "💥", "دامپ/فشار فروش غیرعادی شناسایی شد", "DUMP / SELLING ANOMALY", "حجم مازاد", "فشار فروش غیرعادی دیده شده؛ مدیریت ریسک را رعایت کنید."
        if incoming:
            return "🟢", "ناهنجاری حجم صعودی", "BULLISH VOLUME ANOMALY", "حجم مازاد", "حجم معامله نسبت به baseline بالاتر است؛ ورود پول خالص اثبات نشده است."
        return "🔴", "ناهنجاری حجم نزولی", "BEARISH VOLUME ANOMALY", "حجم مازاد", "حجم معامله نسبت به baseline بالاتر است؛ فشار فروش باید با قیمت تأیید شود."

    def to_telegram(self) -> str:
        emoji, title_fa, title_en, flow_label, advice = self._kind_meta()
        direction_emoji = "📈" if self.direction == SignalDirection.INFLOW else "📉"
        zscore_text = f"<code>{self.zscore:+.2f}</code>" if self.zscore is not None else "<code>—</code>"
        tv = _tradingview_url(self.symbol, self.source)
        return "\n".join([
            f"{emoji} <b>{esc(title_fa)}</b>",
            f"<code>{esc(title_en)}</code>", "",
            f"🪙 <b>نماد:</b> #{esc(self.symbol)}",
            f"💵 <b>قیمت:</b> <code>{_fmt_price(self.price)}</code>",
            f"{direction_emoji} <b>۵ دقیقه:</b> <code>{self.change_5m:+.2f}%</code>",
            f"📊 <b>۲۴ ساعت:</b> <code>{self.change_24h:+.2f}%</code>", "",
            f"⚡ <b>جهش حجم:</b> <code>{self.spike_multiplier:.2f}×</code>",
            f"💰 <b>{esc(flow_label)}:</b> <code>{_fmt_usd_flow(self.inflow_usd)}</code>",
            f"📐 <b>Robust Z-Score:</b> {zscore_text}",
            f"🌐 <b>منبع:</b> <code>{esc(self.source)}</code>", "",
            f'🔗 <a href="{esc(tv)}">چارت TradingView</a>', "",
            f"🎯 <b>نکته:</b> {esc(advice)}",
        ])


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
        explorer = {"ETH": "https://etherscan.io/tx/", "BSC": "https://bscscan.com/tx/", "TRON": "https://tronscan.org/#/transaction/"}.get(self.chain, "")
        link = f"{explorer}{self.tx_hash}" if explorer else self.tx_hash
        incoming = self.direction == SignalDirection.INFLOW
        return "\n".join([
            f"{'📥' if incoming else '📤'} <b>{'واریز نهنگ به صرافی' if incoming else 'برداشت نهنگ از صرافی'}</b>",
            f"<code>{'EXCHANGE INFLOW' if incoming else 'EXCHANGE OUTFLOW'}</code>", "",
            f"⛓ <b>شبکه:</b> <code>{esc(self.chain)}</code>",
            f"🪙 <b>دارایی:</b> #{esc(self.token_symbol)}",
            f"🏦 <b>صرافی:</b> {esc(self.exchange_name)}",
            f"📦 <b>مقدار:</b> <code>{self.amount_native:,.4f} {esc(self.token_symbol)}</code>",
            f"💰 <b>ارزش:</b> <code>{_fmt_usd_flow(self.amount_usd)}</code>", "",
            f'🔗 <a href="{esc(link)}">مشاهده تراکنش</a>', "",
            f"🎯 <b>نکته:</b> {'افزایش احتمال فشار فروش؛ دارایی وارد کیف صرافی شده است.' if incoming else 'کاهش عرضه در بازار محتمل؛ دارایی از کیف صرافی خارج شده است.'}",
        ])
