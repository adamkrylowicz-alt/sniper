"""
app/services/sector_map.py
=============================
Statyczna mapa ticker -> sektor (GICS-podobny, 11 szerokich kategorii) dla
całego uniwersum tickerów Snajpera (Adam, 2026-08-10 - pomysł #4 z listy
usprawnień: "brak filtra korelacji/sektora - bot może wejść w 2 pozycje w
tym samym sektorze naraz, koncentracja ryzyka bez świadomej decyzji").
Znaleziony REALNY przykład na żywo tego samego wieczoru: /status pokazało
Sygnał z 6 otwartymi pozycjami, z czego 4 to spółki
półprzewodnikowe/technologiczne (ASML x2, ASM International, Nvidia).

CELOWO statyczna, nie z API (Yahoo quoteSummary teraz wymaga auth-crumb,
niedostępny bez sesji przeglądarki - sprawdzone na żywo 2026-08-10) - to
znany, ograniczony (~250 tickerów) uniwersum blue-chipów, klasyfikacja
ręczna jest szybsza i pewniejsza niż walka z niestabilnym API dla czegoś co
i tak rzadko się zmienia (spółka nie zmienia sektora z dnia na dzień).

Ticker BEZ wpisu w tej mapie = `get_sector()` zwraca None, zachowanie
fail-open (patrz sector_diversity.py) - nowy ticker dodany do uniwersum bez
aktualizacji tej mapy nigdy NIE blokuje wejścia, tylko traci ochronę
dywersyfikacji dla SIEBIE (nie dla innych).
"""

from __future__ import annotations

TICKER_SECTOR: dict[str, str] = {
    # Technology
    "AAPL_US_EQ": "Technology", "ACN_US_EQ": "Technology", "ADBE_US_EQ": "Technology",
    "ADI_US_EQ": "Technology", "ADYENa_EQ": "Technology", "AMD_US_EQ": "Technology",
    "AMSe_EQ": "Technology", "APH_US_EQ": "Technology", "ASGd_EQ": "Financials",
    "ASML_US_EQ": "Technology", "ASMLa_EQ": "Technology", "ASMa_EQ": "Technology",
    "AVGO_US_EQ": "Technology", "CAPp_EQ": "Technology", "CDNS_US_EQ": "Technology",
    "CRM_US_EQ": "Technology", "CSCO_US_EQ": "Technology", "DSYp_EQ": "Technology",
    "IBM_US_EQ": "Technology", "IDRe_EQ": "Technology", "IFXd_EQ": "Technology",
    "INTC_US_EQ": "Technology", "INTU_US_EQ": "Technology", "KLAC_US_EQ": "Technology",
    "LRCX_US_EQ": "Technology", "MELE_BE_EQ": "Technology", "MRVL_US_EQ": "Technology",
    "MSFT_US_EQ": "Technology", "MU_US_EQ": "Technology", "NOW_US_EQ": "Technology",
    "NVDA_US_EQ": "Technology", "ORCL_US_EQ": "Technology", "PANW_US_EQ": "Technology",
    "PRXa_EQ": "Technology", "QCOM_US_EQ": "Technology", "SAPd_EQ": "Technology",
    "SNPS_US_EQ": "Technology", "STMpp_EQ": "Technology", "TXN_US_EQ": "Technology",
    "UBER_US_EQ": "Technology", "WDAY_US_EQ": "Technology", "WLNp_EQ": "Technology",

    # Financials
    "ACAp_EQ": "Financials", "AGNa_EQ": "Financials", "AGS_BE_EQ": "Financials",
    "ALVd_EQ": "Financials", "AON_US_EQ": "Financials", "AXP_US_EQ": "Financials",
    "BAC_US_EQ": "Financials", "BBVAe_EQ": "Financials", "BKTe_EQ": "Financials",
    "BLK_US_EQ": "Financials", "BNPp_EQ": "Financials", "BRK_B_US_EQ": "Financials",
    "CABKe_EQ": "Financials", "CBKd_EQ": "Financials", "CB_US_EQ": "Financials",
    "CME_US_EQ": "Financials", "CSp_EQ": "Financials", "C_US_EQ": "Financials",
    "DB1d_EQ": "Financials", "DBKd_EQ": "Financials", "GLEp_EQ": "Financials",
    "GS_US_EQ": "Financials", "HNR1d_EQ": "Financials", "IESd_EQ": "Financials",
    "INGAa_EQ": "Financials", "IPOE_US_EQ": "Financials", "JPM_US_EQ": "Financials",
    "KBC_BE_EQ": "Financials", "MAPe_EQ": "Financials", "MA_US_EQ": "Financials",
    "MET_US_EQ": "Financials", "MMC_US_EQ": "Financials", "MUV2d_EQ": "Financials",
    "NNa_EQ": "Financials", "PGR_US_EQ": "Financials", "PYPL_US_EQ": "Financials",
    "SABe_EQ": "Financials", "SANe_EQ": "Financials", "SCHW_US_EQ": "Financials",
    "SPGI_US_EQ": "Financials", "USB_US_EQ": "Financials", "V_US_EQ": "Financials",

    # Health Care
    "ABBV_US_EQ": "Health Care", "ABT_US_EQ": "Health Care", "AMGN_US_EQ": "Health Care",
    "BAYNd_EQ": "Health Care", "BDX_US_EQ": "Health Care", "BSX_US_EQ": "Health Care",
    "CVS_US_EQ": "Health Care", "DHR_US_EQ": "Health Care", "ELp_EQ": "Health Care",
    "FMEd_EQ": "Health Care", "FREd_EQ": "Health Care", "GILD_US_EQ": "Health Care",
    "GRFe_EQ": "Health Care", "ISRG_US_EQ": "Health Care", "JNJ_US_EQ": "Health Care",
    "MDT_US_EQ": "Health Care", "MRKd_EQ": "Health Care", "MRK_US_EQ": "Health Care",
    "NOTd1_EQ": "Health Care", "PFE_US_EQ": "Health Care", "PHIAa_EQ": "Health Care",
    "QIAd_EQ": "Health Care", "REGN_US_EQ": "Health Care", "RHOd_EQ": "Health Care",
    "SANp_EQ": "Health Care", "SRTd1_EQ": "Health Care", "SYK_US_EQ": "Health Care",
    "TMO_US_EQ": "Health Care", "UCB_BE_EQ": "Health Care", "UNH_US_EQ": "Health Care",
    "VRTX_US_EQ": "Health Care", "ZTS_US_EQ": "Health Care",

    # Consumer Discretionary
    "2FEd_EQ": "Consumer Discretionary", "ABNB_US_EQ": "Consumer Discretionary",
    "ADSd_EQ": "Consumer Discretionary", "AMZN_US_EQ": "Consumer Discretionary",
    "BMWd_EQ": "Consumer Discretionary", "CONd_EQ": "Consumer Discretionary",
    "DAId_EQ": "Consumer Discretionary", "F_US_EQ": "Consumer Discretionary",
    "GM_US_EQ": "Consumer Discretionary", "HD_US_EQ": "Consumer Discretionary",
    "ITXe_EQ": "Consumer Discretionary", "KERp_EQ": "Consumer Discretionary",
    "LOW_US_EQ": "Consumer Discretionary", "MCD_US_EQ": "Consumer Discretionary",
    "MCp_EQ": "Consumer Discretionary", "MELI_US_EQ": "Consumer Discretionary",
    "MLp_EQ": "Consumer Discretionary", "NKE_US_EQ": "Consumer Discretionary",
    "P911d_EQ": "Consumer Discretionary", "PCLN_US_EQ": "Consumer Discretionary",
    "PUMd_EQ": "Consumer Discretionary", "RMSp_EQ": "Consumer Discretionary",
    "RNOp_EQ": "Consumer Discretionary", "SBUX_US_EQ": "Consumer Discretionary",
    "STLAPp_EQ": "Consumer Discretionary", "TGT_US_EQ": "Consumer Discretionary",
    "TJX_US_EQ": "Consumer Discretionary", "TSLA_US_EQ": "Consumer Discretionary",
    "VOWd_EQ": "Consumer Discretionary", "ZALd_EQ": "Consumer Discretionary",

    # Consumer Staples
    "58Hd_EQ": "Consumer Staples", "ABI_BE_EQ": "Consumer Staples", "ADa_EQ": "Consumer Staples",
    "BEId_EQ": "Consumer Staples", "BNp_EQ": "Consumer Staples", "CAp_EQ": "Consumer Staples",
    "COLR_BE_EQ": "Consumer Staples", "COST_US_EQ": "Consumer Staples", "HEIAa_EQ": "Consumer Staples",
    "HENd_EQ": "Consumer Staples", "KO_US_EQ": "Consumer Staples", "MDLZ_US_EQ": "Consumer Staples",
    "MO_US_EQ": "Consumer Staples", "NESRd1_EQ": "Consumer Staples", "ORp_EQ": "Consumer Staples",
    "PEP_US_EQ": "Consumer Staples", "PG_US_EQ": "Consumer Staples", "PM_US_EQ": "Consumer Staples",
    "RIp_EQ": "Consumer Staples", "UNIAa_EQ": "Consumer Staples", "WMT_US_EQ": "Consumer Staples",

    # Energy
    "ENI_BE_EQ": "Energy", "EOG_US_EQ": "Energy", "FPp_EQ": "Energy", "OMV_AT_EQ": "Energy",
    "REPe_EQ": "Energy", "SLB_US_EQ": "Energy", "TW10d_EQ": "Energy", "XOM_US_EQ": "Energy",

    # Industrials
    "ACSe_EQ": "Industrials", "ADP_US_EQ": "Industrials", "AENAe_EQ": "Industrials",
    "AIRp_EQ": "Industrials", "ALOp_EQ": "Industrials", "BA_US_EQ": "Industrials",
    "CAT_US_EQ": "Industrials", "CSX_US_EQ": "Industrials", "CTAS_US_EQ": "Industrials",
    "DE_US_EQ": "Industrials", "DGp_EQ": "Industrials", "DPWd_EQ": "Industrials",
    "EDENp_EQ": "Industrials", "EMR_US_EQ": "Industrials", "ENp_EQ": "Industrials",
    "ETN_US_EQ": "Industrials", "FDX_US_EQ": "Industrials", "GD_US_EQ": "Industrials",
    "GE_US_EQ": "Industrials", "HON_US_EQ": "Industrials", "HOp_EQ": "Industrials",
    "ITW_US_EQ": "Industrials", "LIGHTa_EQ": "Industrials", "LMT_US_EQ": "Industrials",
    "LRp_EQ": "Industrials", "MTXd_EQ": "Industrials", "NOC_US_EQ": "Industrials",
    "RANDa_EQ": "Industrials", "RHMd_EQ": "Industrials", "SAFp_EQ": "Industrials",
    "SIEd_EQ": "Industrials", "SPCX_US_EQ": "Industrials", "SUp_EQ": "Industrials",
    "TEPp_EQ": "Industrials", "UPS_US_EQ": "Industrials", "UTX_US_EQ": "Industrials",
    "WKLa_EQ": "Industrials", "WM_US_EQ": "Industrials",

    # Materials
    "1COVd_EQ": "Materials", "AIp_EQ": "Materials", "AKZAa_EQ": "Materials",
    "APAMa_EQ": "Materials", "APD_US_EQ": "Materials", "BASd_EQ": "Materials",
    "BNRd_EQ": "Materials", "DSMa_EQ": "Materials", "HEId_EQ": "Materials",
    "LIN_US_EQ": "Materials", "MTa_EQ": "Materials", "SGOp_EQ": "Materials",
    "SOLB_BE_EQ": "Materials", "SY1d_EQ": "Materials", "UMI_BE_EQ": "Materials",

    # Communication Services
    "CHTR_US_EQ": "Communication Services", "CLNXe_EQ": "Communication Services",
    "CMCSA_US_EQ": "Communication Services", "DIS_US_EQ": "Communication Services",
    "DTEd_EQ": "Communication Services", "FB_US_EQ": "Communication Services",
    "GOOGL_US_EQ": "Communication Services", "KPNa_EQ": "Communication Services",
    "NFLX_US_EQ": "Communication Services", "PROX_BE_EQ": "Communication Services",
    "PUBp_EQ": "Communication Services", "TEFe_EQ": "Communication Services",
    "T_US_EQ": "Communication Services", "UMG1a_EQ": "Communication Services",
    "VIVp_EQ": "Communication Services", "VZ_US_EQ": "Communication Services",

    # Utilities
    "ANAe_EQ": "Utilities", "DUK_US_EQ": "Utilities", "ELEe_EQ": "Utilities",
    "ELI_BE_EQ": "Utilities", "ENGIp_EQ": "Utilities", "ENGe_EQ": "Utilities",
    "ENL1d_EQ": "Utilities", "EOANd_EQ": "Utilities", "IBEe_EQ": "Utilities",
    "NEE_US_EQ": "Utilities", "NTGYe_EQ": "Utilities", "RWEd_EQ": "Utilities",
    "SO_US_EQ": "Utilities", "VIEp_EQ": "Utilities",

    # Real Estate
    "MRLe_EQ": "Real Estate", "PLD_US_EQ": "Real Estate", "URWa_EQ": "Real Estate",
}


def get_sector(ticker: str) -> str | None:
    """None dla tickerów spoza mapy - fail-open, patrz docstring modułu."""
    return TICKER_SECTOR.get(ticker)
