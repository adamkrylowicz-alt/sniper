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

**ZAIMPLEMENTOWANE (2026-07-24), wersja 1.** Patrz `services/eod_engine.py`,
`routes/eod.py`, `/eod/` w UI, oraz wpis w CLAUDE.md z tej daty po pełny
opis. Świadome uproszczenia v1 (do ew. rozbudowy po testach "w boju"):
tiery sizingu jako STAŁE mnożniki entry_amount (x0.6/x1.0/x1.5) zamiast
zakresu % kapitału z PRD (unika zależności od odpytywania wartości całego
konta na każdy tick); stop-loss/take-profit STAŁE od wejścia (nie trailing,
ta sama uproszczona filozofia co Sygnał).

**ZMIANA (2026-07-24, jeszcze tego samego dnia, na żywo w trakcie testów):**
Adam ODRZUCIŁ dwa punkty PRD powyżej: (1) "pozycje NIE są przenoszone na
następny dzień" - USUNIĘTE jako sztywne zachowanie, pozycje EOD domyślnie
zostają otwarte i zarządzane WYŁĄCZNIE przez stop-loss/take-profit, jak w
Sygnale; (2) okno końca sesji wydłużone z 17:25 na **17:30** ("niech
normalnie próbuje złapać do 17:30").

**DOPRECYZOWANIE (2026-07-24, kilka minut później):** Adam zapytał "gdzie
switch w EOD sell all przed końcem sesji" - chciał zachować MOŻLIWOŚĆ
wymuszonego zamknięcia, tylko jako świadomy wybór, nie sztywne zachowanie.
Przywrócone jako opcjonalny przełącznik `EODSettings.force_close_enabled`
(checkbox w Ustawieniach ryzyka na `/eod/`, domyślnie WYŁĄCZONY) -
`_force_close_real`/`FORCE_CLOSE_TIME`/17:20 wróciły do kodu, ale działają
TYLKO gdy user je świadomie włączy. Migracja `migrate_add_eod_force_close.py`
(kolumna dokładana do istniejącej tabeli `eod_settings`).

**ROZSZERZENIE NA USA (2026-07-24, po pierwszym dniu testów bez triggera na
EUR):** Adam: "zmień EOD na USA także, dodaj tam pozycje wg uznania (duża
płynność), niech robi cały czas aż do 22:00 lokalnego czasu". `EOD_WINDOW`
rozszerzone z 16:00-17:30 na **16:00-22:00** Amsterdamu - jeden zakres
obejmujący koniec sesji EUR (Euronext/Xetra) I całą popołudniową sesję USA
(NASDAQ/NYSE ~15:35-21:55 CEST), per-tickerowa bramka `market_hours.
is_market_open()` i tak filtruje właściwą giełdę dla waluty danego tickera.
`FORCE_CLOSE_TIME` przesunięty z 17:20 na 21:55 (nadal za `force_close_enabled`,
wyłączone domyślnie). Dodane 15 płynnych spółek USD do `EODAsset` (100 USD
bazowej kwoty, wybór "wg uznania" - mega-cap, wysoka płynność): Apple,
Microsoft, Nvidia, Amazon, Alphabet, Meta Platforms (`FB_US_EQ` - stary kod
T212 sprzed rebrandingu, ten sam `TICKER_MAP` fix co gdzie indziej w appce
poprawnie mapuje go na META), Tesla, JPMorgan Chase, Visa, Mastercard,
Johnson & Johnson, Walmart, Walt Disney, Netflix, Bank of America - razem z
15 istniejącymi EUR daje 30 tickerów na liście. Zweryfikowane realnym
`get_eod_intraday_1m` dla AAPL i FB (poprawnie zwrócił cenę Meta ~$609, nie
starą/martwą cenę FB).

**TAKE-PROFIT = POWRÓT DO POZIOMU SPRZED SPADKU (2026-07-28), zamiast sztywnego
0.4-0.9%:** Adam wyjaśnił faktyczną intencję strategii: "to ma działać na
zasadzie jebło w dół np 3-5% w ciągu 1-2min kupuje i liczę na szybkie odbicie
w okolice wcześniejszego poziomu np 3-4min wcześniej... nawet nie musi być
idealnie w punkt ale w okolice" - sztywny % z PRD/v1 (wpis 66-72 wyżej) był
oderwany od WIELKOŚCI spadku (5% spadek i tak celował tylko w +0.6%).
Naprawione: take-profit = `reference_price` (cena sprzed tylu minut ile dał
najgorszy spadek, już liczona wewnątrz detekcji spadku) zamiast
`price*(1+take_profit_pct)`. Stop-loss zostaje STAŁY (bez trailing) -
świadomie, Adam odrzucił trailing dla EOD po pytaniu wprost: "to ma działać
na zasadzie... nic ponadto" (w odróżnieniu od Sygnału, gdzie trailing SL
został dodany tego samego dnia - inna filozofia, EOD to krótki scalp na
odbicie, nie jazda z trendem). Przetestowane na sucho przed wdrożeniem
(symulacja minuta-po-minutę na realnych świecach 1-min z dzisiejszej sesji,
15 tickerów USA, zero zapisów/zleceń) - złapany jeden realny trigger (JNJ,
-3.25%, nowy TP +3.36% vs stary +0.6%). Pełny opis w CLAUDE.md z tej daty.

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
- **ROZSTRZYGNIĘTE (2026-07-24), źródło świec 1-min dla EOD:** sprawdzone
  wszystkie 5 kandydatów z listy - **IEX Cloud martwe** (zamknięte
  31.08.2024, nie istnieje w 2026); **Polygon.io** brak free tier, od
  $99/mies. (Adam już to odrzucił jako "kokosy"); **Alpha Vantage** free
  tier 25 zapytań/dzień (za mało), premium ~$29/mies. ale 1-min intraday
  udokumentowany głównie dla US, wsparcie EU niepotwierdzone; **Twelve
  Data** free/Basic za mało kredytów (8/min, 800/dzień), realtime EU dane
  dopiero w planie Pro **$229/mies.** (więcej niż Finnhub premium, też
  "kokosy"); **EOD Historical Data** free bez intraday w ogóle, płatny
  "EOD+Intraday" €29.99/mies. ale ich WŁASNA dokumentacja ogranicza
  "Other Markets" (czyli Europę) do **5-min/1h, NIE 1-min** (tylko USA
  NYSE/NASDAQ ma prawdziwe 1-min) - za grube dla modułu EOD zaprojektowanego
  pod wykrywanie ostrych spadków w 1-2 minuty.
  **Decyzja Adama: na razie nieoficjalne Yahoo intraday** (`range=1d&interval=1m`,
  ten sam mechanizm co istniejący fallback w `price_feed.py::get_mini_chart_ohlc`) -
  zero kosztu, świadomie akceptowane ryzyko braku SLA. **Docelowo (gdy Adam
  będzie miał dostęp do IBKR/Interactive Brokers API)**: IBKR TWS/Client
  Portal API potwierdzone (2026-07-24, web research) że wspiera realne
  świece 1-min dla europejskich giełd (Xetra, Euronext itd.) - wymaga
  osobnej subskrypcji danych rynkowych Level 1 PER GIEŁDA (zwykle niewielka
  opłata miesięczna, czasem obniżana/zerowa przy odpowiednim wolumenie
  prowizji u brokera) - i tak znacznie taniej niż Twelve Data Pro. Gdy IBKR
  dojedzie, to docelowe źródło danych dla EOD, Yahoo tylko na czas przejściowy.
  **ZROBIONE (2026-07-28)**: IBKR dojechał (Adam miał już konto paper) -
  kontener `ib-gateway` (`gnzsnz/ib-gateway-docker`, patrz
  `docker-compose.yml` w korzeniu `/volume1/docker/`) + `backtest/ibkr_data.py`
  (WYŁĄCZNIE do pobierania danych do backtestów, zero live tradingu przez
  IBKR - Yahoo w `eod_engine.py` na produkcji NIE zostało zamienione, to
  osobna, świadoma decyzja, patrz plan "IBKR jako źródło danych
  historycznych"). Realne uprawnienia rynkowe Adama: BRAK Xetra (IBIS) i
  BRAK pełnej skonsolidowanej taśmy NASDAQ (SMART) - obejście: europejskie
  spółki przez `TGATE` (Tradegate, pokrywa większość blue-chipów EU), USA
  przez `IEX` (jedna z 5 giełd w "US Real-Time Non Consolidated" bundle,
  który Adam ma za darmo). Praktyczny sufit niezawodności zapytań
  1-min: **20 dni w jednym zapytaniu działa stabilnie, 30/60 dni często
  wisi bez błędu** (prawdopodobnie limit odpowiedzi/tempo po stronie IBKR
  dla dużych zapytań 1-min, nie coś do naprawienia w kodzie) - `backtest/
  eod_runner.py` i `run_eod_backtest.py` (+ `app/services/strategy/
  eod_strategy.py`, wyciągnięta czysta logika sizing/trailing z
  `eod_engine.py`) już zbudowane i przetestowane na pełnej liście 30
  tickerów EOD. Pierwszy grid search (`stop_loss_pct`) na tej próbce dał
  słaby, niepewny sygnał (mała liczba transakcji, ~24-29) - wdrożony
  ostrożnie (0.4%→0.8%) i obserwowany live na koncie demo, NIE traktowany
  jako ostateczny wniosek.
- **ROZSTRZYGNIĘTE (2026-07-24): RSI/MA/ATR to OSOBNA strategia, DZIAŁA
  RÓWNOLEGLE do Micro-Grid Bota, NIE go zastępuje.** Micro-Grid (czysta
  siatka DCA bez sygnału, `bot_engine.py`, `BotAsset`/`ActiveTrade`) zostaje
  bez zmian - nowa strategia to osobny silnik obok, prawdopodobnie z
  WŁASNYMI tabelami/modelem (analogicznie do tego jak `BotAsset` jest
  CAŁKOWICIE niezależne od `PieAsset`, patrz `models.py::BotAsset`) zamiast
  rozbudowywania istniejącego. Konsekwencja: użytkownik będzie mógł mieć
  ten sam ticker jednocześnie w Micro-Grid i w nowej strategii RSI/MA/ATR,
  jako dwie NIEZALEŻNE pozycje/decyzje - do zaprojektowania jak to się ma
  do "jeden bot na pozycję" (patrz adopt/release_position wyżej, które
  zakładają JEDNĄ relację ticker->ActiveTrade na Micro-Grid).
- Builder strategii (AND/OR bloków) - osobny model danych, spore query
  wykonywane w tick() dla każdego aktywa - warto rozważyć wpływ na rate
  limit T212/nowego API świec. **ŚWIADOMIE ODŁOŻONE (2026-07-24)** - Adam:
  "koduj, będziemy sprawdzać w boju" - zamiast buildera, warunek wejścia na
  sztywno zakodowany (RSI(14)<próg ORAZ cena>SMA(200), patrz niżej). Builder
  zostaje jako możliwe rozszerzenie na później, gdyby sztywny warunek
  okazał się za mało elastyczny po testach na żywo.
- **ZAIMPLEMENTOWANE (2026-07-24): strategia sygnałowa RSI/MA/ATR, wersja 1.**
  Patrz `services/signal_engine.py`, `routes/signal.py`, `/signal/` w UI,
  oraz wpis w CLAUDE.md z tej daty po pełny opis mechaniki (wejście, wyjście,
  poświadczenia współdzielone z Micro-Grid). Świadome uproszczenia v1 (do
  ew. rozbudowy po pierwszych wynikach "w boju"): stop-loss/take-profit
  STAŁE od wejścia (bez trailingu, w odróżnieniu od Micro-Grid), zero DCA,
  brak buildera AND/OR (patrz punkt wyżej).

## Pomysł: tryb "zarządzaj wszystkim" + łączenie ręcznych zakupów z botem (2026-07-23)

Zgłoszone przez Adama wieczorem, po incydencie gdzie dev i prod (patrz
`feedback_snajper_bot_scope` / pamięć Claude o współdzielonym kluczu T212)
przez pomyłkę zarządzały tymi samymi realnymi udziałami naraz. Zasada "bot
obsługuje tylko to co sam kupił" (`app/services/bot_engine.py::_process_entries`
sprawdza WYŁĄCZNIE `ActiveTrade`, nigdy realnego portfela T212) jest słuszna
jako domyślna, ale bywa za sztywna, gdy Adam SAM chce oddać botowi kontrolę
nad czymś kupionym ręcznie - stąd dwa powiązane pomysły, żaden jeszcze nie
zaprojektowany ani nie zaimplementowany:

1. **ZROBIONE (2026-07-27). Switch "zarządzaj wszystkim"** ("idę spać, a ty
   handluj") - `RiskSettings.manage_all_positions` (nowa kolumna, checkbox w
   Ustawieniach ryzyka na `/bot/`). Rozstrzygnięcia z Adamem: (1) dotyczy
   WYŁĄCZNIE Micro-Grid (Sygnał/EOD mają stały SL/TP, nie trailing/DCA, więc
   "zarządzanie wyjściem" ma tam mniej sensu); (2) wyłączenie switcha
   automatycznie ZWALNIA pozycje przejęte WYŁĄCZNIE dzięki niemu
   (`ActiveTrade.auto_adopted=True`, nowa kolumna) - ręczna adopcja
   przyciskiem "Przekaż botowi" zawsze zostaje pod botem do ręcznego
   "Zwolnij", niezależnie od stanu switcha. DCA celowo WYŁĄCZONE dla pozycji
   przejętych switchem (`grid_anchor_price=0`, ten sam mechanizm co stare
   pozycje sprzed migracji) - bot nie zna kontekstu ręcznego zakupu, więc
   "zarządzanie" faktycznie ogranicza się do samego mechanizmu wyjścia
   (trailing stop), zgodnie z ryzykiem opisanym niżej. Patrz
   `bot_engine.py::_auto_adopt_foreign_positions`,
   `routes/bot.py::_release_auto_adopted_positions`, pełny opis w CLAUDE.md
   z 27.07.2026. Ryzyko z pierwotnego zapisu (bot nie zna historii/kontekstu
   ręcznego zakupu) zostaje jako ŚWIADOME ograniczenie, nie bug.

2. **ZROBIONE (2026-07-24). Ręczne "adoptowanie" pojedynczej pozycji** - akcja w UI (np. przy
   pozycji na stronie Aktywa) "przekaż botowi" / "połącz z botem", która
   tworzy dla wybranego tickera nowy wiersz `ActiveTrade` na podstawie
   REALNEJ ilości/średniej ceny z portfela T212 (analogiczny mechanizm do
   ręcznej korekty zrobionej dziś wieczorem przy incydencie z dev/prod -
   `average_price`/`quantity` wprost z `T212Client.get_portfolio()`,
   `dca_level=0`, `baseline_owned_quantity=0` żeby cała ilość liczyła się
   jako "botowa" od tego momentu). Bezpieczniejsze niż globalny switch (jedna
   pozycja na raz, świadoma decyzja), ale wymaga nowego endpointu +
   potwierdzenia w UI (żeby nie dało się tego zrobić przez przypadek).
   Zaimplementowane jako `POST /bot/asset/adopt` + przycisk "Przekaż botowi"
   na stronie Aktywa - patrz wpis w CLAUDE.md z 2026-07-24. Switch "zarządzaj
   wszystkim" (pomysł #1 poniżej) dalej NIEZROBIONY.

Żadne z powyższych NIE zmienia domyślnego zachowania (bot dalej ignoruje
nieznane mu pozycje) - to opt-in, per pozycja albo per switch czasowy.

## Pomysły z serii "Build Better Strategies" (financial-hacker.com, 2026-07-31)

Adam przeczytał 4-częściową serię o rozwoju strategii algorytmicznych i poprosił
o ocenę zastosowania do Snajpera. Nic z tego NIE jest zaimplementowane, same
pomysły do dalszej oceny:

1. **ZROBIONE (2026-07-31), na dev. Detektor "szoku" przed kolejną nogą DCA.**
   Micro-Grid to strukturalnie Grid Trader (Część 1 serii) - zakłada powrót
   ceny do średniej. `compute_exhausted_dca_floor` (`microgrid_strategy.py`)
   to dobra ostatnia linia obrony PO wyczerpaniu wszystkich poziomów DCA, ale
   brakowało PROAKTYWNEGO filtra "to nie zwykły dołek, to zmiana reżimu" (np.
   crash po wynikach finansowych) który zatrzymałby dalsze DCA ZANIM spalimy
   wszystkie poziomy. Dodane: `compute_max_recent_single_day_drop_pct()` +
   `is_shock()` w `microgrid_strategy.py` - odrzuca kolejną nogę DCA gdy
   największy JEDNODNIOWY spadek w ostatnich 3 sesjach przekracza
   `SHOCK_ATR_MULTIPLIER=3` razy ATR instrumentu (odróżnia gwałtowny,
   nieciągły ruch od zwykłego, stopniowego dryfu w dół dla którego grid
   jest zaprojektowany). Flaga `SHOCK_FILTER_ENABLED` (moduł
   `microgrid_strategy.py`) **domyślnie WYŁĄCZONA**. `run_microgrid_backtest.py`
   dostał `--shock-filter`.

   **Zweryfikowane na danych syntetycznych** przed jakimkolwiek uruchomieniem
   na realnych danych: stopniowy dryf -1%/dzień NIE oznaczony jako szok,
   nagły -15% w jednym dniu OZNACZONY poprawnie.

   **Wynik backtestu na realnych parametrach produkcyjnych**: detektor
   zweryfikowany jako AKTYWNY (29 dni-tickerów z realnym szokiem w całym
   400-dniowym/38-tickerowym zbiorze, w tym realny -20.3% w jeden dzień przy
   ATR ~4.7% - poprawnie wykryty), i wywołany 224 razy w realnych momentach
   decyzji o DCA (potwierdzone instrumentacją, nie zgadywane) - ale ANI RAZU
   nie zbiegł się w czasie z otwartą pozycją akurat czekającą na kolejny
   poziom siatki, więc efekt na wynik portfela w tym backteście wyniósł
   dokładnie zero (identyczne liczby z i bez filtra). **To nie błąd - rzadkie
   zdarzenie (szok) x rzadkie zdarzenie (akurat otwarta pozycja na granicy
   DCA) w tym konkretnym oknie historycznym.** Zostaje wyłączony domyślnie;
   to z natury "ubezpieczenie na czarnego łabędzia" (jak historia CHF/SNB z
   Części 1) - brak efektu w zwykłym backteście nie dowodzi że jest
   bezużyteczny, tylko że tego typu zdarzenie nie wystąpiło w tej konkretnej
   próbce. Nie ma dowodów ani za, ani przeciw włączeniu na produkcji.

2. **ZROBIONE (2026-07-31), na dev. Hurst Exponent jako DODATKOWY filtr
   regime'u w `bot_entry_filters.py`.** Po głębszej analizie: Hurst nie
   ZASTĘPUJE `_trend_ok()` (jak pierwotnie sformułowano wyżej) - to dwie różne
   skale czasowe/pytania: `_trend_ok` = "czy TERAZ jest dobry moment
   wejścia" (krótkie okno), Hurst = "czy ten instrument W OGÓLE zachowuje się
   jak kandydat do mean-reversion" (długie okno, ~120 dni - na 7-10 punktach
   Hurst to czysty szum, stąd potrzeba znacznie dłuższego okna niż
   TREND_LOOKBACK_DAYS). Nowe: `compute_hurst_exponent()` (metoda skalowania
   odchylenia std różnic cenowych po logu, bez numpy - go nie ma w
   requirements.txt), `_regime_ok()`, flaga `HURST_FILTER_ENABLED` (moduł
   `bot_entry_filters.py`, **domyślnie WYŁĄCZONA** - zero zmiany zachowania
   produkcji/dev dopóki ktoś świadomie nie włączy). `run_microgrid_backtest.py`
   dostał `--hurst-filter` do porównania A/B.

   **Znaleziony i naprawiony bug podczas testowania na danych syntetycznych**
   (random walk/mean-reversion/trend AR(1)): pierwsza wersja mnożyła
   nachylenie regresji razy 2 (pomylenie skalowania wariancji ze
   skalowaniem odchylenia standardowego) - dawało to Hurst~0.88 dla
   CZYSTEGO random walk (powinno być ~0.5). Po poprawce: random walk ~0.45,
   mean-reversion ~0.23, trend/momentum ~0.55 - poprawny porządek.

   **Wynik backtestu A/B na realnych parametrach produkcyjnych** (400 dni,
   38 tickerów, `dca_trigger_pct=0.03`/`take_profit_step_pct=0.0025`,
   `--test-days 60`): filtr REALNIE działa (1084 z 14262 ocenionych
   kandydatów odrzucone jako "trenduje", 7.6%), ale efekt na wynik portfela
   jest marginalny (in-sample: 314→304 transakcji, +1.46%→+0.84% return;
   out-of-sample: praktycznie bez zmian, 44 transakcje w obu wariantach) -
   bo przy 38 tickerach i jednym wolnym slocie dziennie odrzucenie
   kandydata zwykle oddaje miejsce następnemu w kolejności, nie blokuje
   wejścia całkowicie. **Wniosek: na tym uniwersum tickerów i z tym progiem
   (0.5) filtr nie szkodzi, ale też wyraźnie nie pomaga** - zostaje
   wyłączony domyślnie, brak podstaw żeby go włączać bez dalszych testów
   (np. innego okna/progu - ale to wymagałoby OSOBNEGO eksperymentu
   backtestowego, nie zgadywania, patrz [[feedback_snajper_backtest_before_tuning]]).

3. **ZROBIONE (2026-07-31), zsynchronizowane do prod. Walk-forward /
   out-of-sample split w `run_microgrid_backtest.py`.** Dodana flaga
   `--test-days N` - tnie ostatnie N dni jako out-of-sample test, uruchamiany
   osobno od treningu (świeży portfel), raporty osobno oznaczone
   "IN-SAMPLE"/"OUT-OF-SAMPLE (NIE używać do strojenia)". Bez flagi
   zachowanie identyczne jak wcześniej (tylko kosmetyczny nagłówek + kolumna
   `segment=full` w CSV). Pełny opis w CLAUDE.md z 2026-07-31. Pasuje do już
   istniejącej zasady [[feedback_snajper_backtest_before_tuning]].

   **Realnie się przydało tego samego dnia**: grid search
   `dca_trigger_pct`/`take_profit_step_pct` (siatka 7x7 + rozszerzenie w dół)
   złapał realny curve-fitting - kombinacja najlepsza in-sample wypadła
   out-of-sample gorzej niż "nudniejszy" kandydat. Znaleziony `dca_trigger_pct
   =0.02` (obecne prod: 0.03) - lepszy na OBU oknach i OBU wymiarach
   (return+drawdown). **Zastosowane na dev** (`risk_settings.dca_trigger_pct`),
   backtest po zmianie potwierdził identyczne liczby. Prod NIE dotknięty -
   czeka na dalszą obserwację/decyzję Adama. Pełne liczby w CLAUDE.md.

4. **ZROBIONE (2026-07-31), na dev. Money management - skalowanie √equity,
   OPT-IN.** Adam odwrócił wcześniejszą decyzję z tej listy ("bez zmian") i
   wybrał wprost skalowanie PIERWIASTKOWE (nie liniowe %equity - to
   dokładnie anti-pattern z Części 3; nie Kelly/OptimalF - odrzucone, baza
   ma tylko 41 zamkniętych transakcji z 9 dni historii, 56% bez znanego
   `close_price`, za mało/za zaszumione). **To NIE jest odwrócenie
   pierwotnej decyzji** - stały `BotAsset.entry_amount` zostaje BAZĄ, nowa
   funkcja to nieliniowa NAKŁADKA nad nim, domyślnie WYŁĄCZONA.

   Nowe: `RiskSettings.equity_sizing_enabled`/`equity_sizing_baseline`
   (migracja `migrate_add_equity_sizing.py`, baseline AUTO-CAPTURE przy
   włączeniu checkboxa w UI - `T212Client.get_cash()`, nie ręczne
   wpisywanie), `microgrid_strategy.compute_equity_scaled_amount()`
   (`effective_amount = base_amount * sqrt(current_equity/baseline_equity)`,
   z klamrą bezpieczeństwa 0.5x-3x przeciw glitchowi odczytu equity - ARBITRALNA,
   nie strojona). Equity liczone RAZ na tick (nie per-kandydat) - zero
   nowego kosztu API gdy flaga wyłączona. Checkbox w UI (`/bot/`) obok
   "zarządzaj wszystkim". Backtest: `run_microgrid_backtest.py` dostał
   `--equity-scaling` (baseline = `--starting-cash` przebiegu).

   **Zweryfikowane syntetycznie** przed jakimkolwiek backtestem: equal
   equity→brak zmiany, equity 4x→2x kwoty, equity 0.25x→dotyka klamry
   min=0.5x, equity 9x→dotyka klamry max=3x, equity 100x (glitch)→ograniczone
   do 3x zamiast 100x.

   **Backtest A/B na realnych parametrach produkcyjnych**: efekt marginalny
   (in-sample 1.46%→1.44%, out-of-sample 1.04%→1.05%) - **oczekiwane, nie
   błąd**: w krótkim 400-dniowym oknie equity portfela nie oddala się
   znacząco od kapitału startowego, więc mnożnik √(equity/baseline) zostaje
   bliski 1.0 przez cały backtest. Mechanizm celuje w horyzont wieloletni/
   duże zmiany kapitału, nie w pojedynczy krótki backtest - małe, niezerowe
   różnice potwierdzają że liczy poprawnie, po prostu nie ma tu jeszcze czego
   przeskalować. Zostaje WYŁĄCZONY domyślnie na obu środowiskach po sync -
   włączenie checkboxa to świadoma, osobna decyzja Adama.

5. **ML na `_score()` - niski priorytet, na później.** Obecny scoring to już
   strukturalnie regresja liniowa (ręcznie ważona suma cech). Mogłaby zostać
   dopasowaną regresją logistyczną na realnych wynikach transakcji, ale
   Część 4 serii ostrzega przed niestacjonarnością danych finansowych i
   przeuczeniem - robić dopiero przy dużej próbce zamkniętych transakcji I
   koniecznie z walidacją walk-forward (patrz punkt 3).

## ZROBIONE (2026-07-27): sortowanie w tabelach + panele przestawialne strzałkami

- **Aktywa** (`/warp/portfolio`): kolumna "Waluta" + sortowanie klikane w
  nagłówki tabeli, zapamiętywane w `localStorage` (przetrwa F5) i aplikowane
  natychmiast z cache (bez skoku/rozjazdu po załadowaniu) - patrz
  `portfolio.js::SORT_STORAGE_KEY`.
- **Bot/Sygnał/EOD** (`/bot/`, `/signal/`, `/eod/`): to samo sortowanie
  (w tym kolumna Waluta) dodane do tabel "Otwarte pozycje" -
  `common.js::initSortableTable`.
- **Bot/Sygnał/EOD - kolejność sekcji**: próba narzucenia jednej ustalonej
  z góry kolejności (Aktywa+Otwarte pozycje na górze) nie trafiła w
  oczekiwania Adama - zamiast tego każda z 5 sekcji (Aktywa/Otwarte
  pozycje/Aktywacja/Ustawienia ryzyka/Dziennik) to teraz osobny panel z
  przyciskami ▲/▼, kolejność ustawia sam user, zapamiętywana per strona w
  `localStorage` - patrz `common.js::initReorderablePanels`. Pełny opis
  mechaniki w CLAUDE.md, wpisy z 27.07.2026.

## Pomysły z podręcznika Zorro (`pliki/zorro.chm`, przejrzany 2026-07-31)

Adam poprosił o przejrzenie podręcznika platformy Zorro (ten sam autor co seria
"Build Better Strategies") pod kątem czegoś do zaadoptowania. Znalezione, NIE
zaimplementowane:

1. **DO ZROBIENIA W PRZYSZŁOŚCI: Monte Carlo confidence analysis w backteście.**
   Zorro zamiast liczyć drawdown/return z JEDNEJ historycznej kolejności
   transakcji, tasuje kolejność zamkniętych transakcji setki razy (Monte
   Carlo) i pokazuje rozkład ("przy 95% pewności drawdown wynosi X") zamiast
   pojedynczej liczby z jednej konkretnej sekwencji zdarzeń. To dokładnie
   "Montecarlo reality check" z Części 3 serii "Build Better Strategies" -
   czego NIE zrobiliśmy przy okazji walk-forward split (`--test-days`,
   punkt 3 wyżej). Nasz obecny `max_drawdown_pct` to tylko jedna, konkretna
   kolejność zdarzeń z historii - moglibyśmy trafić akurat na łagodną albo
   akurat na złośliwą sekwencję. Do zrobienia: nowa funkcja w
   `backtest/microgrid_runner.py`/`portfolio.py` - wziąć listę
   `closed_trades` z gotowego backtestu, tasować kolejność N razy (Zorro
   domyślnie N=200), przeliczać equity curve/max drawdown dla każdego
   tasowania, pokazać rozkład (np. percentyle 10/50/90/95). Adam: "zapisz nr
   2 na przyszłość" (2026-07-31) - świadomie odłożone, nie teraz.

2. **Znaleziona, NIE naprawiona luka: `is_shock()` nie odróżnia splitu akcji
   od realnego szoku.** Zorro ma osobny mechanizm (`Outlier`/`PriceJump`)
   rozróżniający "prawdziwy szok" od zwykłego splitu 2:1/4:1 (który wygląda
   jak nagły -50%/-75% w jeden dzień, ale to nie krach, tylko techniczna
   korekta ceny). Nasz `microgrid_strategy.is_shock()`
   (dodany 2026-07-31, patrz punkt 1 wyżej) tego NIE rozróżnia - split akcji
   (NVDA/TSLA robiły split w ostatnich latach) wyglądałby identycznie jak
   realny krach i błędnie zablokowałby DCA. Nieszkodliwe DOPÓKI
   `SHOCK_FILTER_ENABLED=False` (obecny stan), ale do naprawienia PRZED
   ewentualnym włączeniem tego filtra na produkcji.

## ZROBIONE (2026-08-02): walidacja logiki wejścia na danych spoza akcji (Forex/indeksy/metale/BTC)

Adam: "chodzi mi o trenowanie momentow wejscia glownie wiec bez znaczenia na
jakie instrumenty" - w folderze `pliki/historical data` (dane Zorro, 235
plików binarnych `.t6`, 2.3GB) znaleziono 14 instrumentów (AUDUSD, BTCUSD,
EURCHF, GBPUSD, GER30, NAS100, SPX500, UK100, US30, USDCAD, USDCHF, USDJPY,
XAGUSD, XAUUSD) ze świecami 1-minutowymi 2010-2026 - ZERO pokrycia z 38
akcjami US/EU które bot faktycznie handluje, ale idealny materiał do
sprawdzenia czy REGUŁY WEJŚCIA (dca_trigger_pct, filtr Hurst, detektor
szoku) to realny wzorzec cenowy, czy dopasowanie do naszej wąskiej, mocno
skorelowanej próbki dużych spółek (dokładnie ryzyko curve-fittingu z cz.3
serii "Build Better Strategies").

Metoda (skrypty w scratchpadzie, nie w repo - narzędzia jednorazowe, dane
źródłowe dostępne tylko lokalnie w `pliki/`, nie są częścią aplikacji):
`decode_zorro_t6.py` dekoduje binarny format T6 Zorro (struct: DATE double +
6x float High/Low/Open/Close/Val/Vol, potwierdzone ręcznie na
AUDUSD_2010/XAUUSD_2010 - ceny się zgadzają) i agreguje 1-min świece do
DZIENNYCH w formacie `{"o","h","l","c"}` identycznym jak
`price_feed.get_mini_chart_ohlc()`. `zorro_entry_validation.py` odpala
ISTNIEJĄCY silnik (`backtest/microgrid_runner.run_microgrid_backtest`,
`_split_candles` z walk-forward) na każdym z 14 instrumentów osobno, z
DOKŁADNIE produkcyjnymi parametrami (dca_trigger_pct=0.02,
take_profit_step_pct=0.0025, max_dca_levels=5, stop_loss_pct=0.02,
zweryfikowane w bazie 2026-08-02), test_days=250 (~rok out-of-sample, ~15 lat
trening), w trzech wariantach: BASE (jak dziś), +HURST, +SHOCK.

**Wyniki (14 instrumentów × 3 warianty, pełne dane w
`zorro_validation_results.csv` w scratchpadzie):**

- **Win rate konsekwentnie wysoki (85-97%) na WSZYSTKICH instrumentach**,
  nie tylko akcjach - potwierdza że profil "dużo małych wygranych + rzadkie
  duże straty" to cecha STRUKTURALNA samej architektury grid-DCA + ciasny
  take-profit, a nie coś specyficznego dla naszych 38 spółek.
- **Indeksy (GER30/NAS100/SPX500/UK100/US30) zyskowne in-sample** (+1.0% do
  +3.4%) - spójne z ich długim trendem wzrostowym 2010-2024 (bull market),
  "kupuj dołek + DCA w dół + trailing take-profit" naturalnie zarabia w
  trendzie wzrostowym z korektami.
- **Forex mieszany/lekko ujemny** (-0.03% do -1.94%), **metale i BTC wyraźnie
  ujemne in-sample** (XAGUSD -3.69%, BTCUSD -8.49%) - te instrumenty miały
  wieloletnie silne trendy SPADKOWE (srebro 2011-2015, AUD 2011-2015) albo
  ekstremalną zmienność (BTC -70/-80% w 2018/2022) które grid-DCA bez
  twardego stopu przegrywa.
- **Filtr Hurst: konsekwentnie ZMNIEJSZA liczbę wejść** (3115 vs 3842 w
  agregacie, -19%) na każdym instrumencie. Efekt na wynik MIESZANY - wyraźnie
  POMAGA tam gdzie in-sample wynik był najgorszy (XAGUSD -3.69%→-1.30%,
  BTCUSD -8.49%→-7.77%), ale wyraźnie SZKODZI na zyskownych indeksach
  (NAS100 +3.40%→+2.74%, SPX500 +2.26%→+1.41%, GER30 +1.78%→+0.58%) - bo
  filtr blokuje TRENDUJĄCE instrumenty niezależnie od kierunku, a tu
  najbardziej zyskowną "cechą" był akurat trwały trend WZROSTOWY z korektami
  (idealne środowisko dla dip-buyingu, błędnie odrzucane przez Hurst jako
  "nie mean-reversion").
- **Detektor szoku: efekt w agregacie bliski zeru** (avg_ret -0.54%→-0.56%
  in-sample), tak jak wcześniej na akcjach - ale na BTCUSD (najbardziej
  zmienny instrument w zestawie) wyraźnie POGARSZA wynik (-8.49%→-10.09%)
  zamiast chronić, więc "ochrona przed szokiem" na razie nie ma pokrycia w
  danych na ŻADNYM przetestowanym instrumencie.
- **Agregat 14 instrumentów (nierówna waga, FX/indeksy silnie skorelowane
  wewnątrz grupy, to nie 14 niezależnych prób):** BASE train avg_ret=-0.54%,
  test avg_ret=+0.09%; +HURST train=-0.52%/test=+0.06%; +SHOCK
  train=-0.56%/test=+0.09%. **Żaden filtr nie daje jasnej, spójnej korzyści
  w agregacie** - to POTWIERDZA (na dużo większej, całkowicie niezależnej od
  akcji próbce) wcześniejszy wniosek z 2026-07-31 ("zero usprawnień") - obie
  flagi zostają domyślnie WYŁĄCZONE, to nie był przypadek dopasowania do
  wąskiej próbki 38 spółek.

**Ważne zastrzeżenie:** to test generalizacji REGUŁ WEJŚCIA (progi/filtry),
NIE dowód że Snajper mógłby bezpiecznie handlować Forex/metalami/BTC -
symulacja tu NIE modeluje realnej dźwigni CFD na tych instrumentach (tylko
naiwne $100/nogę jak przy akcjach), więc rzeczywisty profil ryzyka na Forex
byłby inny (zwykle wyższa dźwignia w praktyce). Wniosek dotyczy tylko: "czy
dca_trigger_pct/Hurst/Shock to uniwersalny wzorzec cenowy" - odpowiedź: DCA
i wysoki win-rate tak, Hurst i Shock filter nie (w obecnej postaci).

## ZROBIONE (2026-08-02, ciąg dalszy): "czy Snajper mógłby grać na forex zamiast akcji?" - odpowiedź: NIE

Adam: "spróbuj to samo z całą historią (bez test_days, pełne 16 lat) to moze
grac na forex zamiast na akcjach?" - dwa dodatkowe przebiegi (skrypty w
scratchpadzie, `zorro_full_history.py` i `zorro_shared_portfolio.py`),
DOKŁADNIE produkcyjne parametry, bez podziału train/test (cała dostępna
historia na raz).

1. **14 instrumentów osobno (własny $10k portfel każdy, ~13-16 lat)**:
   tylko indeksy (GER30/NAS100/SPX500/UK100/US30) zyskowne, ale marginalnie
   - najlepszy NAS100 CAGR=+0.21%/rok. Forex prawie zero (USDCAD 0.00%,
   USDJPY +0.04%), metale/BTC/AUD/EUR wyraźnie ujemne (BTCUSD -0.69%/rok,
   XAGUSD -0.20%/rok). Średnia po 14 instrumentach: **CAGR=-0.04%/rok**.

2. **Uczciwsze porównanie - te same 14 instrumentów jako JEDEN wspólny
   portfel $10k** (mechanicznie identyczny test do backtestu akcji: kandydaci
   konkurują o sloty, nie 14 osobnych pul kapitału) vs analogiczny wspólny
   portfel 38 akcji (`run_microgrid_backtest.py --days 400`, też dokładnie
   produkcyjne parametry): **akcje: +4.71% za ~400 dni (CAGR≈+4.2%/rok),
   max_dd=3.28%. Forex/indeksy/metale/BTC razem: -8.17% za ~16 lat
   (CAGR≈-0.4%/rok), max_dd=13.46%** - 4x gorszy drawdown, ujemny zwrot.
   Rozbicie per instrument: straty skoncentrowane w XAGUSD (-541 USD sumy
   pnl), BTCUSD (-621), EURCHF (-216), AUDUSD (-182), XAUUSD (-136),
   GBPUSD/USDCHF (lekko ujemne) - TYLKO indeksy (GER30/NAS100/SPX500/UK100/
   US30) i USDCAD/USDJPY dodatnie, ale zbyt mało żeby przebić straty reszty.

**Wniosek: obecna logika Micro-Gridu (dip-buy + DCA + ciasny trailing
take-profit) NIE nadaje się do Forex/metali/krypto** - te instrumenty albo
są zbyt zmienne bez trwałego trendu (BTC, srebro), albo zbyt płaskie/
zakresowe (większość par FX - strategia projektowana pod "kupowanie dołków w
trendzie wzrostowym", a FX z natury nie ma takiego trwałego dryfu jak akcje/
indeksy). JEDYNA grupa która zachowuje się podobnie do akcji (i mogłaby
teoretycznie być kandydatem na rozszerzenie, NIE zamiennik) to indeksy CFD
(GER30/NAS100/SPX500/UK100/US30) - dodatnie w obu testach, ale wciąż dużo
słabsze niż akcje same w sobie. Zastrzeżenie jak poprzednio: symulacja nie
modeluje realnej dźwigni CFD, więc rzeczywisty profil ryzyka na koncie live
byłby inny (prawdopodobnie gorszy niż tu pokazane). Żadna zmiana kodu -
czysta analiza.

## KOREKTA (2026-08-02, ciąg dalszy): poprzednie porównanie forex vs akcje NIE było na tym samym oknie czasowym

Adam słusznie zapytał: "sprawdź czy backtest indeksów CFD ma pokrycie z
historią akcji" - odkryte: NIE miał. Poprzedni wniosek ("Forex/metale/BTC
razem -8.17% za ~16 lat" vs "akcje +4.71% za ~400 dni") porównywał 16 lat
danych Zorro (2010-2026, obejmujące realnie złe reżimy - krach BTC 2018/2022,
bessę srebra 2011-2015, deprecjację AUD 2011-2015) z zaledwie ~400 dniami
akcji (cache fetchowany 2026-07-30, czyli luty 2025 - lipiec 2026 - łagodny,
niedawny okres). To mieszało RÓŻNE reżimy rynkowe, nie tylko różne
instrumenty - metodologicznie nieuczciwe.

**Poprawka - ostatnie 400 dostępnych dni danych Zorro** (kończą się
2026-05-29 - akcje fetchowane 2026-07-30, więc ~2 miesiące na końcu się nie
pokrywają, ale poza tym ta sama "epoka" zamiast 16 lat), te same
produkcyjne parametry, wspólny portfel 14 instrumentów:

- **Akcje (38 tickerów, --days 400)**: +4.71%, max_dd=3.28%, 383 transakcji, winrate 97.1%
- **Forex/indeksy/metale/BTC (14 instr., ostatnie 400 dni)**: **+1.51%**,
  max_dd=**1.50%**, 248 transakcji, winrate 98.0%

**Wniosek skorygowany:** w TYM SAMYM, niedawnym oknie forex/indeksy/metale/
BTC też są zyskowne - mniej niż akcje (+1.51% vs +4.71%, ok. 1/3), ale przy
O POŁOWĘ mniejszym obsunięciu (1.50% vs 3.28%). Poprzedni mocno negatywny
wniosek był w dużej mierze artefaktem porównania różnych okresów - forex/
metale/BTC MIAŁY naprawdę złe wieloletnie reżimy w swojej 16-letniej
historii (to nadal prawda i wciąż ważne dla oceny długoterminowego ryzyka -
patrz sekcja wyżej z 2026-08-02), ale w aktualnym środowisku rynkowym
wypadają rozsądnie, tylko słabiej niż akcje. Właściwy wniosek: **akcje
pozostają lepszym wyborem w obecnym środowisku, ale twierdzenie "forex
zdecydowanie nie działa" było zbyt mocne** - poprzednia sekcja z tego samego
dnia pozostaje ważna jako ostrzeżenie o długoterminowej zmienności tych
instrumentów, nie jako ostateczny wyrok o ich nieprzydatności.

## ZROBIONE (2026-08-02, ciąg dalszy): filtr Hurst i detektor szoku na tym samym, dopasowanym oknie (ostatnie 400 dni)

Adam: "sprawdź to samo z filtrem Hurst i Shock na tym oknie" - te same 14
instrumentów, ostatnie 400 dni (dopasowane do okna akcji z sekcji wyżej),
wspólny portfel $10k, trzy warianty BASE/+HURST/+SHOCK.

**Wynik (wspólny portfel 14 instrumentów):**
- BASE: +1.51%, max_dd=1.50%, 248 transakcji, winrate 98.0%
- +HURST: +1.62%, max_dd=1.51%, 224 transakcji (-24, -10%), winrate 98.2%
- +SHOCK: +1.89%, max_dd=1.49%, 248 transakcji, winrate 98.4%

W TYM konkretnym, krótkim (400-dniowym) oknie oba filtry delikatnie
POPRAWIAJĄ wynik. **Ale - ważne zastrzeżenie**: poprawa jest skoncentrowana
głównie w JEDNYM instrumencie (UK100: BASE pnl_sum=-4.39 -> +HURST +39.94 /
+SHOCK +33.96 - to praktycznie cała różnica), reszta instrumentów miesza się
w obie strony (Hurst pogarsza BTCUSD -43.53->-51.08 i GER30 52.78->31.16,
poprawia NAS100 40.17->44.67; Shock nie zmienia liczby transakcji wcale poza
UK100). To wygląda na pojedynczy uniknięty/zmieniony wynik jednej transakcji
na jednym instrumencie w krótkim oknie, NIE systematyczną przewagę - dokładnie
ten rodzaj szumu małej próby przed którym ostrzega seria "Build Better
Strategies" (efekt znika/odwraca się gdy poszerzyć okno, patrz sekcja wyżej
z tego samego dnia - 14 instrumentów x 16 lat walk-forward NIE pokazało
spójnej korzyści z żadnego filtra).

**Wniosek: nie zmienia to wcześniejszej decyzji.** Duża próbka (16 lat,
14 instrumentów, walk-forward) pozostaje dużo mocniejszym dowodem niż jedno
krótkie, niedawne okno zdominowane przez wynik jednego instrumentu. Obie
flagi (`HURST_FILTER_ENABLED`, `SHOCK_FILTER_ENABLED`) zostają domyślnie
WYŁĄCZONE - to nowe, krótkoterminowe odkrycie jest ciekawostką do
zanotowania, nie podstawą do zmiany.

## ZROBIONE (2026-08-02, ciąg dalszy): to samo na dłuższym oknie (1000 dni)

Adam: "sprawdź to samo na dłuższym oknie, np. 1000 dni" - te same 14
instrumentów, wspólny portfel, ostatnie 1000 dostępnych dni Zorro (kończą
się 2026-05-29, więc to ~2023-2026).

**Wynik:**
- BASE: +2.67%, max_dd=1.95%, 605 transakcji, winrate 97.0%
- +HURST: **+3.30%**, max_dd=**1.63%** (wyraźnie niżej), 549 transakcji
  (-9.3%), winrate 97.3%
- +SHOCK: +2.98%, max_dd=1.95% (bez zmiany), 603 transakcji, winrate 97.2%

W odróżnieniu od okna 400-dniowego (gdzie poprawa Hurst była praktycznie
w całości zasługą JEDNEGO instrumentu, UK100), tu poprawa jest SZERSZA -
AUDUSD (23->47), UK100 (9->50), XAGUSD (55->107) i BTCUSD (strata
zmniejszona -118->-88) wszystkie się poprawiają, kosztem GER30 (100->58) i
kilku mniejszych pogorszeń. Zwrot wyżej ORAZ drawdown wyraźnie niżej -
bardziej przekonujący obraz niż okno 400-dniowe. Detektor szoku nadal w
większości efektu skupiony na UK100 (9->48), reszta bez zmian.

**Ważne zastrzeżenie metodologiczne**: to pojedynczy przebieg BEZ podziału
train/test (in-sample na całym oknie 1000 dni) - słabszy dowód niż
walk-forward. Duży test z tego samego dnia wyżej (14 instrumentów x PEŁNE
~16 lat, Z walk-forwardem train/test) pokazał praktycznie ZEROWY efekt
Hurst w agregacie (test avg_ret 0.09%->0.06%). Trzy okna razem (400d/1000d/
16 lat) układają się w wzorzec: **im nowsze/krótsze okno, tym silniejszy
pozorny efekt Hurst** - to klasyczny sygnał albo zależności od reżimu
(np. inne zachowanie FX/metali w erze zerowych stóp 2010-2021 vs
podwyżek 2022-2026), albo zwykłego przeuczenia do niedawnej historii - te
dwie hipotezy NIE są tu rozróżnione, wymagałoby to walk-forwardu
SPECYFICZNIE na oknie 1000-dniowym (osobny podział train/test wewnątrz tych
1000 dni), nie zrobione jeszcze.

**Wniosek: nadal za mało żeby zmienić decyzję.** Ciekawy, warty
odnotowania sygnał, ale bez walk-forwardu na tym oknie i bez rozstrzygnięcia
hipotezy reżimu vs przeuczenia obie flagi zostają domyślnie WYŁĄCZONE -
zgodnie z zasadą "nigdy nie zgaduj nowego parametru ryzyka, dowód > intuicja".

## ZROBIONE (2026-08-02, finał sesji): walk-forward na 1000 dniach + pełna weryfikacja na 5-letniej historii akcji

Adam: "zrób walk-forward split na oknie 1000 dni ogolnie testuj jak chcesz
byleś coś polepszył w strategii i zyskach" - seria końcowych testów,
podsumowanie całej dzisiejszej pracy.

**1. Walk-forward na forex/indeksy/metale/BTC, okno 1000 dni (train 750/test
250)** - domyka pytanie zostawione otwarte wcześniej tego dnia (in-sample na
1000 dniach wyglądał obiecująco dla Hurst). Wynik: **Hurst TRAIN +2.82%
(lepszy niż BASE +1.85%) ale TEST +0.53% (GORSZY niż BASE +0.68%)** -
podręcznikowy przykład przeuczenia z Części 3 serii "Build Better
Strategies". Shock: identyczny wynik jak BASE na teście (+0.68% oba) - zero
efektu. **To DRUGI niezależny walk-forward (obok testu na pełnych 16
latach) który odrzuca oba filtry - decyzja WYŁĄCZONE jest teraz oparta na
dwóch, nie jednym, rygorystycznym dowodzie.**

**2. Prawdziwa 5-letnia historia akcji (Yahoo, 1300 dni zamiast
dotychczasowych 400) z walk-forward (test_days=300)** - dużo większa próbka
niż dotychczasowe testy tego samego dnia. `dca_trigger_pct=0.02` (obecna
produkcja): TRAIN +3.43%/max_dd=**6.81%**, TEST +1.82%/max_dd=2.49%,
winrate 96.5%. **Ważne: prawdziwy historyczny drawdown (6.81%) jest ok. 2x
wyższy niż sugerował wcześniejszy krótki test (3.28% na 400 dniach)** - 5
lat łapie ostrzejsze okresy rynkowe (prawdopodobnie 2022) których krótkie
okno nie widziało. Realniejsza, mniej optymistyczna ocena ryzyka - nie błąd,
tylko krótkie okno miało mniej pecha.

**3. Grid search dca_trigger_pct (0.015-0.03) na tych samych 5 latach** -
sprawdzenie czy 0.02 nadal jest najlepszym/najbardziej odpornym wyborem na
dużo większej próbce. Wynik: **niemonotoniczny i szumiący** - train nie
koreluje sensownie z test (np. `0.025` miało NAJGORSZY train (+0.50%) ale
NAJLEPSZY test (+2.68%); `0.03` miało najlepszy train (+8.09%) ale środkowy
test). Brak czystej, szerokiej "górki" jak w poprzednim, krótszym grid
searchu (31.07, 400 dni) - na dłuższym, bardziej zaszumionym oknie sygnał
się rozmywa. **Świadomie NIE zmieniamy parametru na podstawie tego wyniku**
- wybór na podstawie samego testu (np. przeskoczenie na 0.025 bo miało
najlepszy TEST) byłby dokładnie błędem przed którym ostrzega Część 3 (dobór
po out-of-sample zamiast tylko potwierdzenie nim) - `dca_trigger_pct=0.02`
zostaje, potwierdzone jako rozsądny wybór, nie odrzucone.

**4. Equity scaling (√equity) na tych samych 5 latach** - efekt nadal
marginalny (train +3.32% vs +3.43% bez, test identyczny +1.82%) - equity w
tym oknie urosło tylko ~3.3% (10000->10332), za mało żeby mnożnik
odchylił się zauważalnie od 1.0. Potwierdza wcześniejszy wniosek: mechanizm
śpi dopóki nie będzie dużo większych zmian kapitału, to oczekiwane
zachowanie, nie błąd.

**PODSUMOWANIE CAŁEJ SESJI (2026-08-02): żadna NOWA zmiana parametru nie
jest uzasadniona dzisiejszymi testami** ponad to co już wdrożono
(`dca_trigger_pct=0.02`, już live). Wartość dzisiejszej pracy to NIE nowy
parametr, tylko dużo WYŻSZA PEWNOŚĆ istniejących decyzji: Hurst i Shock
odrzucone dwoma niezależnymi walk-forwardami zamiast jednym; obecny
`dca_trigger_pct=0.02` zweryfikowany na 5 latach realnych danych zamiast
400 dni; prawdziwy poziom ryzyka (drawdown ~6.8%, nie ~3.3%) lepiej
poznany; forex/indeksy/metale/BTC dokładnie sprawdzone i odrzucone jako
alternatywne uniwersum dla obecnej logiki. Wszystko udokumentowane, zero
zmian w kodzie produkcyjnym.

## ZROBIONE (2026-08-02, ciąg dalszy): sprawdzenie pozostałych parametrów (stop_loss_pct, max_dca_levels) na 5-letniej historii akcji

Adam: "spr inne parametry mowilem ci juz" - dotąd sprawdzone tylko
`dca_trigger_pct`/`take_profit_step_pct`. Grid na tych samych 5 latach akcji
z walk-forward (test_days=300), reszta parametrów = produkcyjne.

**1. `stop_loss_pct` (0.015-0.04): ZERO efektu, identyczne liczby dla każdej
wartości.** Znalezione wyjaśnienie w kodzie (`microgrid_strategy.py::
compute_trailing_stop`/`compute_exhausted_dca_floor`) - `stop_loss_pct` to
tylko FALLBACK używany gdy ATR jest niedostępny; w tym backteście (i
praktycznie zawsze w normalnej pracy bota, gdzie tickery mają już
wystarczającą historię) ATR jest zawsze policzalny, więc floor liczony jest
z ATR*1.8, nigdy z `stop_loss_pct`. **Wniosek: `stop_loss_pct` jest w
praktyce parametrem martwym/wegetatywnym** dla dojrzałych tickerów - realną
ochronę daje ATR. Ma znaczenie tylko dla świeżo dodanych tickerów bez
wystarczającej historii do policzenia ATR (rzadki przypadek). Nie
zmieniany - nie ma czego stroić.

**2. `max_dca_levels` (3-10): REALNY, POWTARZALNY sygnał.** W odróżnieniu od
`dca_trigger_pct` (train/test się kłóciły), tu train i test się ZGADZAJĄ:

| max_dca | TRAIN ret | TRAIN dd | TEST ret | TEST dd |
|---|---|---|---|---|
| 5 (było) | +3.43% | 6.81% | +1.82% | 2.49% |
| 6 | +0.96% | 8.62% | +1.32% | 3.49% |
| 7 | +9.00% | 5.14% | +2.15% | 4.02% |
| **8** | **+11.09%** | 6.71% | **+2.83%** | 3.87% |
| 9 | +13.16% | 6.20% | +1.93% | 4.76% |
| 10 | +11.21% | 6.86% | +1.27% | 5.27% |

Prawdziwa "górka" (nie ucieczka w nieskończoność) - test rośnie do poziomu
8, potem ODWRACA SIĘ (9→10: +1.93%→+1.27%, dd dalej rośnie) - dokładnie
kształt wzorca opisanego w Części 3 jako oznaka realnego efektu, nie
przypadku. Poziom 8 najlepszy całościowo: zwrot/drawdown (miara
ryzyko-skorygowana) = 0.73 - IDENTYCZNIE jak obecne 5 (1.82/2.49=0.73) -
czyli to przeskalowanie w górę przy tej samej jakości, nie "więcej zysku
kosztem gorszej jakości". Otwarte pozycje na koniec okna bez zmian (6
train/1 test) na każdym poziomie - nie artefakt utkniętych niezamkniętych
strat.

**Koszt: 60% więcej kapitału zamrożonego w najgorszym scenariuszu**
(8×100$=800$ vs 5×100$=500$ na ticker, ×38 tickerów = teoretyczne max
$30400 vs $19000) - musi być zestawione z realnym saldem konta przed
włączeniem na produkcji. `dca_scenario="1,1,1,1,1"` NIE wymaga zmiany przy
`max_dca_levels=8` - `_dca_multiplier()` (`bot_engine.py`) automatycznie
powtarza ostatni mnożnik (1) dla poziomów poza listą, sprawdzone w kodzie.

**Zastosowane NA RAZIE TYLKO NA DEV** (Adam wybrał przez AskUserQuestion:
"Zastosuj 8 na dev") - `risk_settings.max_dca_levels` zmienione w bazie dev
(user_id=1) z 5 na 8. Backtest po zmianie reprodukowalny (identyczne liczby
z grid searchem: +11.09%/6.71%dd train, +2.83%/3.87%dd test). **Prod NIE
dotknięty** - czeka na decyzję Adama, biorąc pod uwagę zwiększoną
ekspozycję kapitałową względem realnego salda konta demo.
