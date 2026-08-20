"""
CEX market-data providers.

IMPORTANT:
    Binance and KuCoin are treated as independent data sources.

    No candle history, volume baseline, or statistical history is shared
    between them.

Provider priority:
    1. Binance
    2. KuCoin fallback

Every failure is logged explicitly.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias
from candle_store import Candle

log = logging.getLogger("smart_money_bot.market_data")


BINANCE_ENDPOINTS = [
    "https://api1.binance.com/api/v3/ticker/24hr",
    "https://api2.binance.com/api/v3/ticker/24hr",
    "https://api3.binance.com/api/v3/ticker/24hr",
    "https://api.binance.com/api/v3/ticker/24hr",
]

BINANCE_KLINES_ENDPOINTS = [
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]

KUCOIN_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/allTickers"
)


class MarketDataProvider:
    def __init__(
        self,
        session: requests.Session,
        timeout: int = 8,
    ):
        self.session = session
        self.timeout = timeout

        self.last_binance_error = None
        self.last_kucoin_error = None

    # ---------------------------------------------------------
    # main ticker fetch
    # ---------------------------------------------------------

    def fetch(self) -> Tuple[Dict[str, dict], str]:
        """
        Returns:
            data, source
        """

        log.info("MARKET DATA | starting provider selection")

        binance_data = self._fetch_binance()

        if binance_data:
            log.info(
                "MARKET DATA | source=binance | symbols=%d",
                len(binance_data),
            )
            return binance_data, "binance"

        log.error(
            "MARKET DATA | Binance unavailable | switching to KuCoin | reason=%s",
            self.last_binance_error or "unknown",
        )

        kucoin_data = self._fetch_kucoin()

        if kucoin_data:
            log.warning(
                "MARKET DATA | source=kucoin | symbols=%d | Binance fallback active",
                len(kucoin_data),
            )
            return kucoin_data, "kucoin"

        log.error(
            "MARKET DATA | ALL PROVIDERS FAILED | Binance=%s | KuCoin=%s",
            self.last_binance_error,
            self.last_kucoin_error,
        )

        return {}, "none"

    # ---------------------------------------------------------
    # Binance ticker
    # ---------------------------------------------------------

    def _fetch_binance(self) -> Dict[str, dict]:
        self.last_binance_error = None

        for index, url in enumerate(BINANCE_ENDPOINTS, start=1):
            started = time.monotonic()

            try:
                res = self.session.get(
                    url,
                    timeout=self.timeout,
                )

                elapsed = time.monotonic() - started

                if res.status_code != 200:
                    reason = (
                        f"endpoint={index} "
                        f"status={res.status_code} "
                        f"elapsed={elapsed:.2f}s"
                    )

                    self.last_binance_error = reason

                    log.warning(
                        "BINANCE TICKER FAILED | %s",
                        reason,
                    )

                    continue

                try:
                    raw = res.json()
                except ValueError as e:
                    self.last_binance_error = (
                        f"endpoint={index} invalid_json={e}"
                    )

                    log.error(
                        "BINANCE TICKER JSON ERROR | endpoint=%d | error=%s",
                        index,
                        e,
                    )

                    continue

                if not isinstance(raw, list):
                    self.last_binance_error = (
                        f"endpoint={index} response_not_list"
                    )

                    log.error(
                        "BINANCE TICKER INVALID RESPONSE | endpoint=%d",
                        index,
                    )

                    continue

                filtered: Dict[str, dict] = {}

                invalid_items = 0

                for item in raw:
                    sym = item.get("symbol")

                    if sym not in TARGET_SYMBOLS:
                        continue

                    try:
                        normalized = resolve_alias(sym)

                        filtered[normalized] = {
                            "lastPrice": float(
                                item["lastPrice"]
                            ),
                            "quoteVolume": float(
                                item["quoteVolume"]
                            ),
                            "priceChangePercent": float(
                                item["priceChangePercent"]
                            ),
                        }

                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        invalid_items += 1

                if not filtered:
                    self.last_binance_error = (
                        f"endpoint={index} "
                        f"no_target_symbols"
                    )

                    log.error(
                        "BINANCE TICKER EMPTY | endpoint=%d | raw=%d | target=%d",
                        index,
                        len(raw),
                        len(TARGET_SYMBOLS),
                    )

                    continue

                log.info(
                    "BINANCE TICKER OK | endpoint=%d | symbols=%d | invalid=%d | elapsed=%.2fs",
                    index,
                    len(filtered),
                    invalid_items,
                    elapsed,
                )

                return filtered

            except requests.Timeout as e:
                self.last_binance_error = (
                    f"endpoint={index} timeout={e}"
                )

                log.error(
                    "BINANCE TICKER TIMEOUT | endpoint=%d | error=%s",
                    index,
                    e,
                )

            except requests.RequestException as e:
                self.last_binance_error = (
                    f"endpoint={index} request_error={e}"
                )

                log.error(
                    "BINANCE TICKER REQUEST ERROR | endpoint=%d | error=%s",
                    index,
                    e,
                )

            except Exception as e:
                self.last_binance_error = (
                    f"endpoint={index} unexpected={e}"
                )

                log.exception(
                    "BINANCE TICKER UNEXPECTED ERROR | endpoint=%d",
                    index,
                )

        return {}

    # ---------------------------------------------------------
    # Binance 5m candles
    # ---------------------------------------------------------

    def fetch_recent_5m_candles(
        self,
        binance_symbol: str,
        limit: int = 864,
    ) -> Optional[List[Candle]]:
        """
        Fetch closed 5m candles.

        The last currently-open candle is intentionally removed.

        This method is ONLY Binance data.
        It must never be used to populate KuCoin history.
        """

        limit = max(
            2,
            min(int(limit), 1000),
        )

        last_error = None

        for index, url in enumerate(
            BINANCE_KLINES_ENDPOINTS,
            start=1,
        ):
            try:
                res = self.session.get(
                    url,
                    params={
                        "symbol": binance_symbol,
                        "interval": "5m",
                        "limit": limit,
                    },
                    timeout=self.timeout,
                )

                if res.status_code != 200:
                    last_error = (
                        f"endpoint={index} "
                        f"status={res.status_code}"
                    )

                    log.warning(
                        "BINANCE KLINES FAILED | symbol=%s | %s",
                        binance_symbol,
                        last_error,
                    )

                    continue

                raw = res.json()

                if not isinstance(raw, list):
                    last_error = (
                        f"endpoint={index} invalid_response"
                    )
                    continue

                candles: List[Candle] = []

                now_ms = int(time.time() * 1000)

                for row in raw:
                    try:
                        candle = Candle.from_binance(row)

                        # Do NOT put open candle into closed history.
                        if candle.close_time >= now_ms:
                            continue

                        candles.append(candle)

                    except (
                        IndexError,
                        TypeError,
                        ValueError,
                    ):
                        continue

                if not candles:
                    last_error = (
                        f"endpoint={index} no_closed_candles"
                    )

                    log.warning(
                        "BINANCE KLINES EMPTY | symbol=%s",
                        binance_symbol,
                    )

                    continue

                log.info(
                    "BINANCE KLINES OK | symbol=%s | closed=%d",
                    binance_symbol,
                    len(candles),
                )

                return candles

            except requests.Timeout as e:
                last_error = (
                    f"endpoint={index} timeout={e}"
                )

                log.error(
                    "BINANCE KLINES TIMEOUT | symbol=%s | error=%s",
                    binance_symbol,
                    e,
                )

            except requests.RequestException as e:
                last_error = (
                    f"endpoint={index} request_error={e}"
                )

                log.error(
                    "BINANCE KLINES REQUEST ERROR | symbol=%s | error=%s",
                    binance_symbol,
                    e,
                )

            except ValueError as e:
                last_error = (
                    f"endpoint={index} invalid_json={e}"
                )

                log.error(
                    "BINANCE KLINES JSON ERROR | symbol=%s | error=%s",
                    binance_symbol,
                    e,
                )

        log.error(
            "BINANCE KLINES FAILED ALL ENDPOINTS | symbol=%s | reason=%s",
            binance_symbol,
            last_error,
        )

        return None

    # ---------------------------------------------------------
    # KuCoin ticker
    # ---------------------------------------------------------

    def _fetch_kucoin(self) -> Dict[str, dict]:
        self.last_kucoin_error = None

        started = time.monotonic()

        try:
            res = self.session.get(
                KUCOIN_ENDPOINT,
                timeout=self.timeout + 2,
            )

            elapsed = time.monotonic() - started

            if res.status_code != 200:
                self.last_kucoin_error = (
                    f"status={res.status_code}"
                )

                log.error(
                    "KUCOIN TICKER FAILED | status=%s | elapsed=%.2fs",
                    res.status_code,
                    elapsed,
                )

                return {}

            try:
                payload = res.json()
            except ValueError as e:
                self.last_kucoin_error = (
                    f"invalid_json={e}"
                )

                log.error(
                    "KUCOIN TICKER JSON ERROR | error=%s",
                    e,
                )

                return {}

        except requests.Timeout as e:
            self.last_kucoin_error = f"timeout={e}"

            log.error(
                "KUCOIN TICKER TIMEOUT | error=%s",
                e,
            )

            return {}

        except requests.RequestException as e:
            self.last_kucoin_error = f"request_error={e}"

            log.error(
                "KUCOIN TICKER REQUEST ERROR | error=%s",
                e,
            )

            return {}

        tickers = (
            payload
            .get("data", {})
            .get("ticker", [])
        )

        if not isinstance(tickers, list):
            self.last_kucoin_error = "ticker_not_list"

            log.error(
                "KUCOIN TICKER INVALID PAYLOAD"
            )

            return {}

        result: Dict[str, dict] = {}

        invalid = 0

        for ticker in tickers:
            raw_symbol = ticker.get(
                "symbol",
                "",
            )

            if not raw_symbol.endswith("-USDT"):
                continue

            normalized = raw_symbol.replace(
                "-",
                "",
            )

            if normalized not in TARGET_SYMBOLS:
                continue

            last_price = ticker.get("last")
            vol_value = ticker.get("volValue")
            change_rate = ticker.get("changeRate")

            if (
                last_price is None
                or vol_value is None
                or change_rate is None
            ):
                invalid += 1
                continue

            try:
                result[
                    resolve_alias(normalized)
                ] = {
                    "lastPrice": float(last_price),
                    "quoteVolume": float(vol_value),
                    "priceChangePercent": (
                        float(change_rate) * 100
                    ),
                }

            except (
                TypeError,
                ValueError,
            ):
                invalid += 1

        if not result:
            self.last_kucoin_error = (
                "no_target_symbols"
            )

            log.error(
                "KUCOIN TICKER EMPTY | target_symbols=%d",
                len(TARGET_SYMBOLS),
            )

            return {}

        log.info(
            "KUCOIN TICKER OK | symbols=%d | invalid=%d | elapsed=%.2fs",
            len(result),
            invalid,
            time.monotonic() - started,
        )

        return result