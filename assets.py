"""
Static list of Nobitex-listed assets and their symbol aliases across
external CEX APIs (Binance / KuCoin use slightly different tickers for
the same coin, e.g. MATIC -> POL, RNDR -> RENDER).
"""
from typing import Dict, List, Set

NOBITEX_ALL_ASSETS: List[str] = [
    # --- بیت‌کوین و ارزهای اصلی ---
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "TRXUSDT", "DOTUSDT", "LINKUSDT", "SHIBUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "UNIUSDT",
    "ETCUSDT", "XLMUSDT", "STXUSDT", "XMRUSDT", "FILUSDT", "ATOMUSDT", "EGLDUSDT", "ALGOUSDT",
    "VETUSDT", "ICPUSDT", "HBARUSDT", "THETAUSDT", "XTZUSDT", "EOSUSDT", "IOTAUSDT", "NEOUSDT",
    # --- لایه ۱، لایه ۲ و زیرساخت‌ها ---
    "APTUSDT", "SUIUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT", "POLUSDT", "FTMUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT", "STRKUSDT", "KASUSDT", "FLOWUSDT", "RONUSDT", "MANTRAUSDT", "MINAUSDT",
    "KAVAUSDT", "ASTRUSDT", "ANKRUSDT", "ROSEUSDT", "ZILUSDT", "IOTXUSDT", "ONEUSDT", "CKBUSDT",
    "GLMRUSDT", "MOVRUSDT", "STRAXUSDT", "KLAYUSDT", "CELOUSDT", "SKLUSDT", "QNTUSDT", "LDOUSDT",
    "METISUSDT", "MANTAUSDT", "ALTUSDT", "ZKUSDT", "EIGENUSDT", "SCRUSDT", "TAOUSDT",
    # --- میم‌کوین‌ها و اکوسیستم تلگرام/تون ---
    "PEPEUSDT", "FLOKIUSDT", "BONKUSDT", "WIFUSDT", "NOTUSDT", "DOGSUSDT", "HMSTRUSDT", "TONUSDT",
    "MEMEUSDT", "PEOPLEUSDT", "BOMEUSDT", "NEIROUSDT", "CATSUSDT", "MAJORUSDT", "PENGUUSDT",
    "POPCATUSDT", "BABYDOGEUSDT", "1000SATSUSDT", "TURBOUSDT", "MYROUSDT", "MEWUSDT", "BRETTUSDT",
    "DEGENUSDT", "SLERFUSDT", "MOGUSDT", "COQUSDT", "SMILEUSDT", "LUNCUSDT", "USTCUSDT",
    "PNUTUSDT", "ACTUSDT", "MOODENGUSDT", "GOATUSDT", "HIPPOUSDT",
    # --- دیفای، اوراکل و صرافی‌ها ---
    "AAVEUSDT", "GRTUSDT", "RUNEUSDT", "DYDXUSDT", "JUPUSDT", "PYTHUSDT", "PENDLEUSDT", "ENAUSDT",
    "ONDOUSDT", "OMUSDT", "RAYUSDT", "ORDIUSDT", "BLURUSDT", "ENSUSDT", "CRVUSDT", "MKRUSDT",
    "SNXUSDT", "COMPUSDT", "1INCHUSDT", "CAKEUSDT", "SUSHIUSDT", "CVXUSDT", "RPLUSDT", "BALUSDT",
    "FXSUSDT", "YFIUSDT", "KNCUSDT", "ZRXUSDT", "ALPHAUSDT", "BADGERUSDT", "REQUSDT", "DRIFTUSDT",
    "AEVOUSDT", "ETHFIUSDT", "MORPHOUSDT", "COWUSDT",
    # --- هوش مصنوعی، متاورس و گیمینگ ---
    "FETUSDT", "AGIXUSDT", "OCEANUSDT", "RENDERUSDT", "WLDUSDT", "ARKMUSDT", "JTOUSDT",
    "SANDUSDT", "MANAUSDT", "AXSUSDT", "CHZUSDT", "GALAUSDT", "GMTUSDT", "AUDIOUSDT", "SLPUSDT",
    "ILVUSDT", "ALICEUSDT", "MAGICUSDT", "HIGHUSDT", "YGGUSDT", "SUPERUSDT", "PIXELUSDT",
    "PORTALUSDT", "PRIMEUSDT", "IOUSDT", "ATHUSDT", "ASIUSDT", "CGPTUSDT",
    # --- سایر ---
    "JSTUSDT", "SUNUSDT", "LPTUSDT", "WOOUSDT", "HOTUSDT", "DENTUSDT", "RVNUSDT", "SPELLUSDT",
    "UMAUSDT", "IDUSDT", "MAVUSDT", "EDUUSDT", "SFPUSDT", "C98USDT", "TWTUSDT", "MASKUSDT",
    "API3USDT", "BANDUSDT", "TRBUSDT", "RSRUSDT", "STORJUSDT", "ARUSDT", "BNTUSDT", "NMRUSDT",
    "RADUSDT", "OXTUSDT", "BATUSDT", "ENJUSDT", "LRCUSDT", "SYSUSDT", "ZENUSDT", "QTUMUSDT",
    "TFUELUSDT", "GASUSDT", "PROMUSDT", "LOOMUSDT", "SSVUSDT", "WAXPUSDT", "STEEMUSDT",
]

# main_symbol -> list of alternate tickers used by other exchanges/APIs for the same asset.
SYMBOL_ALIASES: Dict[str, List[str]] = {
    "1000SATSUSDT": ["SATSUSDT", "1000SATS-USDT", "SATS-USDT"],
    "BABYDOGEUSDT": ["BABYDOGE-USDT", "1000000BABYDOGEUSDT"],
    "POLUSDT": ["MATICUSDT", "POL-USDT", "MATIC-USDT"],
    "FETUSDT": ["ASIUSDT", "FET-USDT", "ASI-USDT"],
    "RENDERUSDT": ["RNDRUSDT", "RENDER-USDT", "RNDR-USDT"],
    "SHIBUSDT": ["1000SHIBUSDT", "SHIB-USDT"],
    "BONKUSDT": ["1000BONKUSDT", "BONK-USDT"],
    "PEPEUSDT": ["1000PEPEUSDT", "PEPE-USDT"],
    "FLOKIUSDT": ["1000FLOKIUSDT", "FLOKI-USDT"],
}


def build_target_symbol_set() -> Set[str]:
    targets = set(NOBITEX_ALL_ASSETS)
    for aliases in SYMBOL_ALIASES.values():
        for alias in aliases:
            targets.add(alias.replace("-", ""))
    return targets


def resolve_alias(raw_symbol: str) -> str:
    """Map any known alias ticker back to its Nobitex canonical symbol."""
    clean = raw_symbol.replace("-", "")
    for main_symbol, aliases in SYMBOL_ALIASES.items():
        if clean == main_symbol or clean in [a.replace("-", "") for a in aliases]:
            return main_symbol
    return clean


TARGET_SYMBOLS: Set[str] = build_target_symbol_set()
