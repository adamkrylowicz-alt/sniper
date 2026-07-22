# Pomysły na v2 - sesja 2026-07-21

Zapisane z dyskusji, do dalszej oceny - NIC z tego nie jest jeszcze zaimplementowane
ani zdecydowane. Patrz `PROJECT_SPEC.md` dla tego co już działa (Warp Mode, Micro-Grid Bot).

## Podsumowanie projektu (surowe, od Adama)

**Nazwa / cel projektu:**
Webowa aplikacja (SaaS) + bot automatyczny do handlu akcjami przez Trading212.

**Kluczowe założenia:**
- Działa tylko na spółkach europejskich notowanych w euro (Euronext, Xetra itp.) - żeby uniknąć kosztów FX.
- API Trading212 jest słabe (mocne rate limity, ograniczenia na typy kont, brak dobrych danych wskaźnikowych).
- Dane rynkowe (OHLCV + wskaźniki) pobierane głównie z Finnhub API + lokalne liczenie wskaźników (TA-Lib).
- Aplikacja ma być intuicyjna i mało chaotyczna.
- Godziny handlu bota: preferowane 10:00-16:00/16:30.

### Główne elementy aplikacji
1. Dashboard
2. Ręczny handel
3. Bot Automatyczny (główny element)
4. Portfel / Pozycje
5. Historia
6. Analizy
7. Ustawienia

### Bot Automatyczny - logika

**1. Normalny tryb handlu (10:00-15:45/16:00)**

Warunek wejścia (bazowy):
- RSI(14) < 35
- Cena > MA(200)

Zarządzanie ryzykiem:
- Stop Loss: 1.8 x ATR(14)
- Take Profit: 3 x ATR lub trailing stop
- Ryzyko na transakcję: 0.75-1.5% kapitału (zależnie od presetu)

Builder strategii powinien pozwalać na:
- Blokowe budowanie warunków (AND/OR)
- Wskaźniki: RSI, MA, EMA, ATR, Volume itp.
- Gotowe presety ryzyka: Konserwatywny / Zrównoważony / Agresywny
- Kalkulator ryzyka na żywo (potencjalna strata w EUR i %)

**2. Specjalny moduł EOD (End of Day)**

Działa tylko pod koniec sesji (od ok. 16:00). Cel: szybka reakcja na nagłe, ostre
spadki (1-2 minuty), nie na powolne pełzanie w dół.

Logika triggera: bot co 1 minutę sprawdza świece 1-minutowe, reaguje na ostry
spadek w krótkim czasie (1-5 minut).

Przykładowa siatka dokupywania (ASML ~1800 EUR):

| Spadek w krótkim czasie | Wielkość pozycji     |
|-------------------------|-----------------------|
| -2.0% do -3.0%          | 0.5-0.7% kapitału     |
| -3.5% do -5.0%          | 0.9-1.2% kapitału     |
| -5.5%+                  | 1.3-1.8% kapitału     |

Po zakupie: natychmiastowy ciasny Take Profit (0.4-0.9% powyżej ceny zakupu lub
tuż poniżej high poprzedniej świecy 1-min), bardzo ciasny trailing stop
(0.3-0.5%). Pozycje z tego modułu NIE są przenoszone na następny dzień.

### Dodatkowe wymagania techniczne
- Rate limiting i cache dla API Trading212 i Finnhub.
- Obsługa limitów Trading212 (szczególnie przy składaniu zleceń).
- Możliwość wyboru listy "High Conviction" spółek, na których działa moduł EOD.
- Interfejs czytelny, nieprzeładowany.

## Ustalenia/zastrzeżenia z dyskusji (2026-07-21)

1. **To inna strategia wejścia, nie rozszerzenie Micro-Grid Bota.** Obecny bot
   (`app/services/bot_engine.py`) kupuje od razu każdy ticker z listy `BotAsset`,
   bez sygnału - czysta siatka DCA. RSI/MA/ATR to osobny mechanizm decyzyjny.
   Nierozstrzygnięte: zastąpić obecne wejście, czy dodać jako osobny tryb obok.

2. **Moduł EOD to w praktyce osobny silnik**, nie dodatek do `tick()` -
   inna granulacja (świece 1-min vs cena spot co 60s), inny trigger, inny
   sposób zarządzania wyjściem, inne godziny działania.

3. **Finnhub - realny problem zweryfikowany na żywo na kluczu z `.env`
   (2026-07-21):**
   - `/search` działa (widzi SAP.DE, ASML.AS).
   - `/quote` działa TYLKO dla US (AAPL OK, SAP.DE/ASML.AS ->
     `"error":"You don't have access to this resource."`).
   - `/stock/candle` (świece, DOWOLNA rozdzielczość, dzienna i 1-minutowa) ->
     ten sam błąd braku dostępu **nawet dla AAPL** (czysty US stock).
   - Wniosek: to NIE jest kwestia braku pokrycia Europy - obecny plan Finnhub
     w ogóle nie ma dostępu do endpointu świec, dla niczego. Candle stało się
     prawdopodobnie funkcją płatną.
   - Konsekwencja uboczna: `price_feed.py::get_mini_chart_ohlc` i tak w 100%
     przypadków ląduje na fallbacku Yahoo (nie na Finnhub jak sugerują
     komentarze w kodzie) - mini-wykresy działają, ale nie tak jak
     udokumentowano.
   - Moduł EOD (potrzebuje świec 1-min dla spółek EUR) jest DZIŚ niewykonalny
     przez Finnhub na obecnym planie. Jedyna realna opcja to nieoficjalne
     Yahoo intraday (`range=1d&interval=1m`, już używane gdzie indziej jako
     fallback) - bez SLA, może przestać działać bez ostrzeżenia.
   - Adam szuka tańszej alternatywy z realnym dostępem do świec (nie chce
     płacić "kokosów" za Finnhub premium ani Polygon).

4. **T212 nie pozwala na ręczne OCO z dwoma jednoczesnymi resting-orderami**
   (potwierdzone na żywo 2026-07-21, konto user1, SAPd_EQ) - trailing exit
   (`_manage_trailing_exit` w `bot_engine.py`) próbował trzymać RÓWNOCZEŚNIE
   LIMIT SELL (take-profit) i STOP (stop-loss) na te same udziały. Próba
   wystawienia STOP-a po tym jak LIMIT SELL już rezerwuje akcje kończy się
   błędem T212 `400 selling-equity-not-owned` - broker traktuje akcje jako
   już "zaklepane" przez pierwsze zlecenie, więc drugie resting-sell na tę
   samą ilość jest odrzucane. Backoff na tę ścieżkę dodany (żeby nie
   spamować logu co tick), ale sama mechanika wymaga przeprojektowania.
   Do przegadania (NIE zrobione jeszcze):
   - Syntetyczny stop-loss (bot sam pilnuje ceny, Market Sell przy przebiciu)
     - wada: nie chroni gdy bot offline (dokładnie problem który miał
       rozwiązać prawdziwy STOP).
   - Odwrócić mechanikę: trzymać TYLKO STOP, ale przesuwany w górę (klasyczny
     trailing stop od dołu) zamiast rosnącego LIMIT SELL od góry - jedno
     resting zlecenie, chroni zawsze, ale zamyka na cofnięciu ceny, nie na
     sztywnym +krok jak w pierwotnym pomyśle Adama.

## Otwarte pytania (do rozstrzygnięcia przed kodowaniem)
- Czy EOD ma sens bez stabilnego źródła świec 1-min? Jakie API rozważyć
  (Twelve Data, Alpha Vantage, Polygon, EOD Historical Data, IEX Cloud) -
  do sprawdzenia limitów/cen/pokrycia EUR.
- RSI/MA/ATR: zastępuje obecną strategię Micro-Grid czy działa równolegle?
- Builder strategii (AND/OR bloków) - osobny model danych, spore query
  wykonywane w tick() dla każdego aktywa - warto rozważyć wpływ na rate
  limit T212/nowego API świec.
