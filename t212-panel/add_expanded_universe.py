"""
add_expanded_universe.py
==========================
Jednorazowy skrypt: rozszerza listy Sygnału i EOD do ~100 tickerów każda
(50 USA/50 EU), rozłącznych między sobą (Adam, 2026-08-05: "zmienmy logike
wyboru aktywow rozszerzmy ja do powiedzmy 100 aktywow, 50/50 usa i eu").

Tickery dobrane przez subagenta (S&P100/Nasdaq100 dla USA, DAX40/CAC40/
AEX25/IBEX35/FTSEMIB40/BEL20/OMX dla EU) i zweryfikowane 1:1 wobec realnej
tabeli `instruments` (istnienie + poprawna waluta) PRZED uruchomieniem tego
skryptu - patrz CLAUDE.md wpis z tego dnia. Skrypt i tak sam odrzuca każdy
ticker którego nie znajdzie w `instruments`, więc jest bezpieczny nawet
gdyby powyższa lista okazała się niekompletna.

Bezpieczne do uruchomienia wielokrotnie - pomija tickery już obecne na
danej liście (UniqueConstraint user_id+ticker i tak by to wymusił, ale
sprawdzamy jawnie żeby dać czytelny log zamiast wyjątku).

Uruchom RAZ, z katalogu t212-panel, wskazując username:

    python3 add_expanded_universe.py <username>
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"
DEFAULT_ENTRY_AMOUNT = 100.00

# --- EOD: priorytetowy silnik, CAŁA ta lista jest automatycznie zarezerwowana
# dla EOD (patrz market_hours.py::_eod_reserved_tickers) - Micro-Grid i
# Sygnał nie mogą na nią wejść.
EOD_TICKERS = [
    "PG_US_EQ", "UNH_US_EQ", "HD_US_EQ", "ABBV_US_EQ", "PFE_US_EQ", "PEP_US_EQ", "MRK_US_EQ", "CSCO_US_EQ",
    "TMO_US_EQ", "MCD_US_EQ", "ABT_US_EQ", "ACN_US_EQ", "ADBE_US_EQ", "DHR_US_EQ", "NKE_US_EQ", "LIN_US_EQ",
    "TXN_US_EQ", "NEE_US_EQ", "PM_US_EQ", "UPS_US_EQ", "UTX_US_EQ", "HON_US_EQ", "QCOM_US_EQ", "LOW_US_EQ",
    "INTC_US_EQ", "IBM_US_EQ", "AMGN_US_EQ", "CAT_US_EQ", "GE_US_EQ", "BA_US_EQ", "SBUX_US_EQ", "GS_US_EQ",
    "MDT_US_EQ", "LMT_US_EQ", "INTU_US_EQ", "AXP_US_EQ", "ISRG_US_EQ", "PLD_US_EQ", "PCLN_US_EQ", "SYK_US_EQ",
    "GILD_US_EQ", "MDLZ_US_EQ", "ADP_US_EQ", "CVS_US_EQ", "TJX_US_EQ", "MMC_US_EQ", "MO_US_EQ", "SPGI_US_EQ",
    "VRTX_US_EQ", "ADI_US_EQ",
    "ADSd_EQ", "DB1d_EQ", "DPWd_EQ", "FREd_EQ", "FMEd_EQ", "HENd_EQ", "MRKd_EQ", "MUV2d_EQ", "P911d_EQ", "CONd_EQ",
    "1COVd_EQ", "HNR1d_EQ", "HEId_EQ", "MTXd_EQ", "SRTd1_EQ", "SY1d_EQ", "ZALd_EQ", "BEId_EQ", "BNRd_EQ", "CBKd_EQ",
    "QIAd_EQ", "OMV_AT_EQ", "PUMd_EQ", "VIVp_EQ", "PUBp_EQ", "RIp_EQ", "DSYp_EQ", "LRp_EQ", "CAPp_EQ", "MLp_EQ",
    "ENp_EQ", "CAp_EQ", "ACAp_EQ", "GLEp_EQ", "ENGIp_EQ", "VIEp_EQ", "STMpp_EQ", "HOp_EQ", "AIp_EQ", "ELp_EQ",
    "RMSp_EQ", "RNOp_EQ", "STLAPp_EQ", "TEPp_EQ", "URWa_EQ", "WLNp_EQ", "EDENp_EQ", "ALOp_EQ", "SGOp_EQ", "SANp_EQ",
]

# --- Sygnał: ROZŁĄCZNA z listą EOD (żadnego wspólnego tickera - inaczej
# Sygnał miałby 0 realnie dostępnych kandydatów tam gdzie się pokrywa).
SIGNAL_TICKERS = [
    "REGN_US_EQ", "C_US_EQ", "SCHW_US_EQ", "ZTS_US_EQ", "CB_US_EQ", "SO_US_EQ", "DUK_US_EQ", "BSX_US_EQ",
    "TGT_US_EQ", "EOG_US_EQ", "PGR_US_EQ", "MU_US_EQ", "BDX_US_EQ", "APD_US_EQ", "ETN_US_EQ", "ITW_US_EQ",
    "AON_US_EQ", "CME_US_EQ", "NOC_US_EQ", "SLB_US_EQ", "USB_US_EQ", "WM_US_EQ", "EMR_US_EQ", "CSX_US_EQ",
    "FDX_US_EQ", "GD_US_EQ", "MET_US_EQ", "F_US_EQ", "GM_US_EQ", "T_US_EQ", "VZ_US_EQ", "DE_US_EQ",
    "AVGO_US_EQ", "AMD_US_EQ", "CMCSA_US_EQ", "PANW_US_EQ", "SNPS_US_EQ", "CDNS_US_EQ", "MRVL_US_EQ", "KLAC_US_EQ",
    "LRCX_US_EQ", "MELI_US_EQ", "ABNB_US_EQ", "WDAY_US_EQ", "CHTR_US_EQ", "NOW_US_EQ", "UBER_US_EQ", "BRK_B_US_EQ",
    "APH_US_EQ", "CTAS_US_EQ",
    "CSp_EQ", "HEIAa_EQ", "PHIAa_EQ", "WKLa_EQ", "RANDa_EQ", "NNa_EQ", "AGNa_EQ", "KPNa_EQ", "AKZAa_EQ", "DSMa_EQ",
    "ADYENa_EQ", "UMG1a_EQ", "LIGHTa_EQ", "MTa_EQ", "BBVAe_EQ", "ITXe_EQ", "REPe_EQ", "TEFe_EQ", "CABKe_EQ", "NTGYe_EQ",
    "AMSe_EQ", "CLNXe_EQ", "ELEe_EQ", "AENAe_EQ", "GRFe_EQ", "ANAe_EQ", "ACSe_EQ", "MAPe_EQ", "BKTe_EQ", "ENGe_EQ",
    "SABe_EQ", "MRLe_EQ", "IDRe_EQ", "ENI_BE_EQ", "IESd_EQ", "2FEd_EQ", "ASGd_EQ", "58Hd_EQ", "TW10d_EQ", "KBC_BE_EQ",
    "SOLB_BE_EQ", "AGS_BE_EQ", "UMI_BE_EQ", "COLR_BE_EQ", "ELI_BE_EQ", "PROX_BE_EQ", "APAMa_EQ", "MELE_BE_EQ", "ABI_BE_EQ", "UCB_BE_EQ",
]


def add_tickers(conn: sqlite3.Connection, table: str, user_id: int, tickers: list[str]) -> None:
    cur = conn.cursor()
    existing = {row[0] for row in cur.execute(f"SELECT ticker FROM {table} WHERE user_id=?", (user_id,))}
    added, skipped_existing, skipped_missing = 0, 0, 0
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for ticker in tickers:
        if ticker in existing:
            skipped_existing += 1
            continue
        row = cur.execute("SELECT currency_code FROM instruments WHERE ticker=?", (ticker,)).fetchone()
        if row is None:
            print(f"  POMINIĘTY (brak w instruments): {ticker}")
            skipped_missing += 1
            continue
        currency = row[0] or "USD"
        display_ticker = ticker.split("_")[0]
        cur.execute(
            f"INSERT INTO {table} (user_id, ticker, display_ticker, currency, entry_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, ticker, display_ticker, currency, DEFAULT_ENTRY_AMOUNT, now),
        )
        added += 1
    conn.commit()
    print(f"{table}: dodano {added}, pominięto (już były) {skipped_existing}, pominięto (brak w instruments) {skipped_missing}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Użycie: python3 add_expanded_universe.py <username>")
        return
    username = sys.argv[1]

    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        print(f"Nie znaleziono usera '{username}'.")
        conn.close()
        return
    user_id = row[0]
    print(f"user_id={user_id} ({username})\n")

    print("=== EOD (priorytetowy, rezerwowany) ===")
    add_tickers(conn, "eod_assets", user_id, EOD_TICKERS)
    print("\n=== Sygnał ===")
    add_tickers(conn, "signal_assets", user_id, SIGNAL_TICKERS)

    conn.close()


if __name__ == "__main__":
    main()
