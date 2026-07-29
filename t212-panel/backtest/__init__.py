"""
backtest/
==========
Offline backtester dla strategii Sygnał (patrz plan "Backtester v1 - Sygnal
na danych historycznych", 2026-07-28). Odtwarza TĘ SAMĄ logikę decyzyjną co
produkcja (app/services/strategy/signal_strategy.py) na historycznych
świecach dziennych, żeby weryfikować zmiany strategii bez czekania na żywy
rynek.

CELOWO poza `app/` - to narzędzie offline, nie część serwowanej appki
Flask. Jedyny punkt styku z `app/` to import czystych funkcji strategii
(signal_strategy.py) i price_feed.get_mini_chart_ohlc() (tylko przy
pierwszym fetchu danych, potem czysty replay z lokalnego cache).
"""
