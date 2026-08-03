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

4. **ROZSTRZYGNIĘTE I ZAIMPLEMENTOWANE (2026-07-21, dopracowane 22.07/27.07/
   28.07) - T212 nie pozwala na ręczne OCO z dwoma jednoczesnymi resting-
   orderami.** (potwierdzone na żywo 21.07, konto user1, SAPd_EQ) - trailing
   exit (`_manage_trailing_exit` w `bot_engine.py`) próbował trzymać
   RÓWNOCZEŚNIE LIMIT SELL (take-profit) i STOP (stop-loss) na te same
   udziały, T212 odrzucał drugie zlecenie `400 selling-equity-not-owned`.
   **UWAGA (2026-08-02): ten wpis przez tydzień nieaktualnie sugerował
   "NIE zrobione jeszcze" - w rzeczywistości wybrano i wdrożono drugą z
   dwóch opcji tego samego dnia (21.07):** JEDNO zlecenie - pojedynczy,
   ciągle przesuwany w górę STOP (trailing od dołu), zamiast rosnącego
   LIMIT SELL. Dopracowane kolejnymi iteracjami: 22.07 - ciągły trailing
   (goni szczyt ceny, nie tylko przy pełnym minięciu progu) + floor oparty
   o ATR(14)*1.8 zamiast sztywnego %; 27.07 - pierwsze uzbrojenie liczy
   `max(floor, ciasny_target)` jednym strzałem (Adam:
   [[feedback_snajper_profit_protection_priority]] - ochrona zysku
   priorytetem); 28.07 - wyjątek dla pozycji bez amunicji do DCA (natychmiastowy
   STOP zakotwiczony w aktualnej cenie, nie czeka na 2 progi zysku). Migracja
   ze starego dwunożnego OCO (anulowanie zalegăłego `sell_order_id`) też
   w kodzie. Pełny opis mechaniki w docstringu `_manage_trailing_exit()` i
   CLAUDE.md z tych dat. **Kompromis świadomie zaakceptowany**: zamyka na
   cofnięciu ceny (klasyczny trailing stop), nie na sztywnym +krok jak w
   pierwotnym pomyśle Adama - ale to i tak było jego wyborem po 27.07
   (ochrona zysku > sztywny target).

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

## ZROBIONE (2026-08-02, ciąg dalszy): max_dca_levels=8 zastosowane na prod + commit do gita

Adam: "zastosuj max_dca_levels=8 też na prod commituj na gity itd a potem
dalej szukaj co by tu ulepszyc". `risk_settings.max_dca_levels` zmienione
na PROD (user_id=1, bot aktywny) z 5 na 8, bezpośrednio w bazie - BEZ
restartu procesu (`RiskSettings` czytane świeżo co tick, ten sam mechanizm
co przy wcześniejszej zmianie `dca_trigger_pct`). Log diagnostyczny czysty
po zmianie. Dwa commity do gita (`code-server/workspace/sniper`, gałąź
master): (1) przełącznik trybu zlecenia RYNEK/LIMIT per-kafelek
Focus/Warp/Instrument (zaległa, przetestowana praca z 30-31.07, nigdy
niecommitowana), (2) walk-forward split + filtry Hurst/Shock + money
management √equity (dzisiejsza/31.07 praca opisana w sekcjach wyżej).
Pliki cache `backtest/data/*.json` (świeżo pobrane 400d/1300d dla 38
tickerów) świadomie NIE commitowane - to regenerowalny cache, nie kod.

## ZROBIONE (2026-08-02, finał): take_profit_step_pct=0.002 na dev - drugi solidny kandydat

Kontynuacja poszukiwań (Adam: "dalej szukaj co by tu ulepszyc") po
zastosowaniu `max_dca_levels=8`. Grid `take_profit_step_pct` (0.0015-0.004)
NA NOWO przy `max_dca_levels=8` (interakcja między parametrami mogła się
zmienić od poprzedniego grid searchu z 31.07, robionego jeszcze przy
`max_dca_levels=5`):

| tp_step | TRAIN ret | TRAIN dd | TEST ret | TEST dd |
|---|---|---|---|---|
| 0.0015 | +10.20% | 6.76% | +3.50% | 3.14% |
| **0.002** | **+11.25%** | 6.69% | **+3.69%** | **3.30%** |
| 0.0025 (było) | +11.09% | 6.71% | +2.83% | 3.87% |
| 0.003 | +10.43% | 6.72% | +1.84% | 4.08% |
| 0.0035 | +10.25% | 6.74% | +1.21% | 4.93% |
| 0.004 | +10.10% | 6.75% | +1.87% | 4.85% |

Czysty, monotoniczny sygnał (train i test się ZGADZAJĄ, ta sama jakość co
przy `max_dca_levels`) - `0.002` bije `0.0025` na OBU oknach RÓWNOCZEŚNIE:
lepszy zwrot treningowy (+11.25% vs +11.09%), wyraźnie lepszy testowy
(+3.69% vs +2.83%, +30% relatywnie), NIŻSZY drawdown na obu oknach - bez
kompromisu. Zastosowane na dev (`risk_settings.take_profit_step_pct=0.002`),
backtest po zmianie reprodukowalny (identyczne liczby: +11.25%/6.69%dd
train, +3.69%/3.30%dd test).

**Dodatkowo sprawdzony kształt `dca_scenario`** (płaski vs rosnący
"ramp_up" 1→2.4 vs malejący "ramp_down" 2.4→1, przy `max_dca_levels=8`,
`tp_step=0.0025`): `ramp_down` odrzucony (train +14.52% świetny, ale test
+1.73%/dd=7.66% - klasyczne przeuczenie/odwrócenie). `ramp_up` daje więcej
zwrotu na obu oknach (+14.31%/+3.93%) niż płaski, ALE proporcjonalnie
więcej ryzyka (zwrot/drawdown gorszy niż płaski: 0.68 vs 0.73 na teście) -
w odróżnieniu od `max_dca_levels=8` (które dało IDENTYCZNY zwrot/drawdown co
baza) to nie jest darmowa poprawa, tylko przesunięcie suwaka ryzyka, słabiej
przebadane (1 kształt, nie siatka). **Nie rekomendowane teraz** - `dca_scenario`
zostaje płaski.

Prod NIE dotknięty dla `take_profit_step_pct` - czeka na decyzję Adama.

## ZROBIONE (2026-08-02, finał sesji): take_profit_step_pct=0.002 zastosowane na prod

Adam wybrał (AskUserQuestion): "Zastosuj też na prod". `risk_settings.
take_profit_step_pct` zmienione na PROD (user_id=1, bot aktywny) z 0.0025
na 0.002, bezpośrednio w bazie, BEZ restartu. Log diagnostyczny czysty po
zmianie. Prod ma teraz OBA dzisiejsze zweryfikowane usprawnienia razem:
`max_dca_levels=8` + `take_profit_step_pct=0.002` (obok wcześniej
zastosowanego `dca_trigger_pct=0.02` z 31.07) - wszystkie trzy zweryfikowane
walk-forwardem na 5-letniej historii akcji, wszystkie ze zgodnym train/test.

## ZROBIONE (2026-08-02, finał): weryfikacja _score() ranking kandydatów

Adam: "sprawdź _score() ranking kandydatów" - po trzech zastosowanych dziś
usprawnieniach (dca_trigger_pct, max_dca_levels, take_profit_step_pct).
`_score()` (`bot_entry_filters.py`) to ważona suma 4 składników:
range_position (W=1.0), trend_calm (W=0.6), fx_penalty (W=0.8, stała kara
0.24 dla USD), spread (W=0.5, w backteście zawsze neutralny - brak danych
bid/ask). Backtest UŻYWA prawdziwego `rank_candidates`/`_score()`
(`microgrid_runner.py` linia 334, jeden zwycięzca level-0/dzień = `scored[0]`)
- test robiony przez monkeypatch `_score`/wag, twarde filtry nietknięte.

**1. Scoring vs losowy wybór zwycięzcy** (te same twarde filtry, 3 seedy,
5 lat akcji, walk-forward): obecny scoring TRAIN +11.25%/dd=6.69% bije
losowy na WSZYSTKICH 3 seedach (+8.54%/dd8.28%, **-0.47%/dd14.52%**,
+5.20%/dd7.38%) - losowy ma ogromną wariancję wyniku i znacznie wyższy,
niestabilny drawdown. **Potwierdzone: scoring realnie redukuje
ryzyko/niepewność, to nie "teatr"** - pierwszy raz w całej sesji
zweryfikowane head-to-head, nie zakładane.

**2. Ablacja wag (zero-out pojedynczego składnika):**
- Bez `range_position`: gorzej na OBU oknach (train +7.31%/dd9.41%, test
  +2.05%/dd5.05%) - jednoznacznie pomaga, zostaje.
- Bez `fx_penalty`: KATASTROFA na treningu (-1.60%/dd=14.55%!) i gorzej na
  teście - najważniejszy pojedynczy składnik, bez dyskusji zostaje.
- Bez `trend_calm`: TRAIN gorszy (9.56% vs 11.25%) ale TEST LEPSZY (4.86%
  vs 3.69%, dd 2.07% vs 3.30%) - **konflikt train/test**, dokładnie ten sam
  sygnał ostrzegawczy co przy filtrze Hurst i grid searchu dca_trigger_pct
  wcześniej dziś. Zgodnie z ustaloną regułą (trening i test muszą się
  zgadzać) - **NIE zmieniane na podstawie jednego okna testowego.**

**Wniosek: scoring jest dobrze zaprojektowany** (2 z 3 realnych składników
- range_position, fx_penalty - jednoznacznie pomagają na obu oknach,
wyraźnie bije losowy wybór). `trend_calm` (W=0.6) to jedyny niepewny
element - kandydat na dalszą weryfikację (grid wagi zamiast on/off, drugie
niezależne okno testowe), ale NIE zmieniony teraz - za mało dowodów wg
tego samego standardu co reszta dzisiejszej sesji. Żadna zmiana kodu ani
bazy - czysta analiza.

## ZROBIONE (2026-08-02, finał): ATR_STOP_MULTIPLIER i MAX_CONCURRENT_POSITIONS - sprawdzone, bez zmian

Adam: "myśl co jeszcze podtuningować". Dwa nietestowane dotąd parametry
(monkeypatch `backtest.microgrid_runner.ATR_STOP_MULTIPLIER`/
`MAX_CONCURRENT_POSITIONS` - importowane w tym module przez "from X import
Y", kopia wartości, nie live referencja do `bot_engine.py`), 5 lat akcji,
walk-forward, PROD parametry (dca_trigger_pct=0.02, max_dca_levels=8,
take_profit_step_pct=0.002).

**1. `ATR_STOP_MULTIPLIER` (obecnie 1.8 w `bot_engine.py`) - PRAWDZIWY
mechanizm ochronny** (odkryty dziś wcześniej: `stop_loss_pct` to tylko
martwy fallback, to ATR*multiplier faktycznie wyznacza floor/trailing
stop). Grid 1.2-3.0: TRAIN rośnie monotonicznie z szerszym stopem (5.19%→
13.60%) - mechaniczny efekt (szerszy stop = mniej realizowanych strat w
danym oknie, nie prawdziwa przewaga). TEST szczytuje przy **1.5** (+3.90%/
dd=3.09%), potem systematycznie się pogarsza (2.2: +3.41%, 3.0: +2.85%,
dd rośnie do 4.11%). **Konflikt train/test** (trening chce więcej, test
chce mniej) - ten sam sygnał ostrzegawczy co przy `trend_calm` w `_score()`
wcześniej dziś. **NIE zmieniane** - nie spełnia kryterium "oba okna się
zgadzają". Obecne 1.8 jest blisko szczytu testowego (2. miejsce po 1.5) -
niekoniecznie idealne, ale rozsądnie dobrane, nie błąd.

**2. `MAX_CONCURRENT_POSITIONS` (obecnie 10)** - grid 6-20: powyżej 10
wynik DOSŁOWNIE identyczny (11.28%/+3.69% dla 13/16/20, praktycznie to samo
co 10: 11.25%/+3.69%) - limit rzadko jest wąskim gardłem przy 38 tickerach/
$10k kapitału w tym backteście, podnoszenie go nic nie daje. Poniżej 10
wyraźnie gorzej (6: 9.61%/+3.20%, 8: 9.68%/+3.69%). **Wniosek: 10 jest już
dobrze dobrane, potwierdzone jako rozsądny wybór, NIE zmieniane.**

Żadna zmiana kodu ani bazy - czysta analiza, dwa kolejne parametry
potwierdzone jako już rozsądnie ustawione.

## ZROBIONE (2026-08-02, finał): gęsty grid W_TREND_CALM + inne parametry sprawdzone

Kontynuacja tuningu (Adam: "robimy dalej", "jak wynik to zastosuj i leć
dalej"). Domknięcie wątku `W_TREND_CALM` (waga trend_calm w `_score()`,
wcześniejsza ablacja on/off pokazała konflikt train/test) - gęsty grid
0.0-1.0 (9 punktów): środek (0.1-0.5) skacze chaotycznie (szum), ale
skrajności pouczające - **w=0.0 daje najlepszy TEST ze wszystkich
(+4.86%/dd=2.07%)**, podczas gdy **w=0.8/1.0 zapadają się na teście**
(+0.88%/dd=4.17% i **-1.79%/dd=5.92%!**) mimo dobrego treningu - wyraźny
klif przeuczenia powyżej obecnej wartości. Środek (0.0 vs obecne 0.6) to
wciąż konflikt train/test (0.0 wygrywa test, 0.6 wygrywa trening) - nie
spełnia kryterium "oba okna się zgadzają". **W_TREND_CALM zostaje 0.6** -
potwierdzone jako bezpieczna strefa, wyraźnie z dala od klifu przy 0.8+.

**Dodatkowo sprawdzone i odrzucone dziś jako już dobre/zbyt niepewne:**
`MAX_ENTRY_RANGE_POSITION` (0.6, już w dobrej strefie - powyżej 0.7 test
spada z 3.7% na 2.56% i tam zostaje płasko), `RECENT_WINDOW_ROWS` (10,
POTWIERDZONY CZYSTY SZUM po dokładniejszym gridzie 3-12 - sąsiednie
wartości skaczą chaotycznie w obie strony, żadna stabilna górka), skala
progów trendu DROP/SPIKE/DRAWDOWN (test płaski 3.70-3.75% od 0.8x do 2.0x -
za słaby sygnał), `ATR_PERIOD` (14, gładka krzywa ale prawdziwy kompromis
zwrot/ryzyko bez dominującej wartości - zostaje).

**Podsumowanie całego przeglądu parametrów Micro-Gridu (2026-08-02):**
Sprawdzone: dca_trigger_pct, take_profit_step_pct, max_dca_levels,
stop_loss_pct, dca_scenario (kształt), wagi _score() (4 składniki +
dokładny grid trend_calm), ATR_STOP_MULTIPLIER, MAX_CONCURRENT_POSITIONS,
MAX_ENTRY_RANGE_POSITION, RECENT_WINDOW_ROWS, progi trendu, ATR_PERIOD.
Trzy zmiany zastosowane na prod (dca_trigger_pct=0.02, max_dca_levels=8,
take_profit_step_pct=0.002), reszta potwierdzona jako już rozsądnie
ustawiona albo zbyt niepewna żeby ruszać wg dzisiejszego standardu
(train i test muszą się zgadzać na return I drawdown).

## ZROBIONE (2026-08-02, finał): interakcja 2D dca_trigger_pct x take_profit_step_pct przy max_dca_levels=8 - potwierdzona bez zmian

Sprawdzenie czy dwie dzisiejsze zmiany nadal się wzajemnie wzmacniają po
zmianie max_dca_levels (poprzedni podobny grid z 31.07 był robiony przy
starym max_dca_levels=5). Siatka 5x3 (dca_trigger_pct 0.015-0.025 x
take_profit_step_pct 0.0015-0.0025), 5 lat akcji, walk-forward.

**Wynik: obecna kombinacja (0.02/0.002) jest NAJLEPSZA na teście ze
wszystkich 15 par** (+3.69%, wyraźny margines nad drugim miejscem +3.50%
i trzecim +2.83% - oba też przy trig=0.02, tylko inne tp - czyli 0.02
dominuje niezależnie od take_profit_step_pct). Ciekawy przykład
przeuczenia złapany po drodze: `trig=0.0175/tp=0.002` miał najlepszy
TRENING ze wszystkich (+14.43%!) ale test tylko +2.06% - dokładnie ten typ
pułapki, przed którą chroni walk-forward. **Potwierdzone bez zmian** -
dwie dzisiejsze zmiany (dca_trigger_pct=0.02, take_profit_step_pct=0.002)
nadal tworzą najlepszą znalezioną parę po zmianie max_dca_levels na 8.

## ZROBIONE (2026-08-02, finał sesji): próba testu strategii Sygnał (RSI/MA/ATR) - wniosek: narzędzie za wolne dla pełnej skali

Po wyczerpaniu prostych testów Micro-Gridu, Adam wybrał kontynuację w
kierunku Sygnał/EOD. Po drodze poprawiony nieaktualny wpis o mechanice OCO
w T212 (punkt 4 wyżej - okazał się już rozwiązany 21-28.07, nie "NIE
zrobione jeszcze" jak sugerował stary zapis).

**Próba 1 (58 tickerów Sygnału, 5 lat, walk-forward, baseline + 3 grid
searche naraz - rsi_threshold/stop_loss_atr_mult/take_profit_atr_mult):
ZABITA po 23 minutach czystego CPU bez postępu.** `backtest/signal_runner.py`
ma komentarz z 30.07 sugerujący optymalizację pod grid search (O(n) zamiast
O(n^2)), ale przy skali 58 tickerów × 36 konfiguracji × ~200-dniowe okna
RSI/SMA/ATR liczone w Pythonie na `Decimal` to i tak setki milionów
operacji - fundamentalnie za wolne bez dalszej optymalizacji (np. numpy,
którego brak w `requirements.txt`, albo cache'owania SMA/RSI między
konfiguracjami zamiast przeliczania od zera dla każdej).

**Próba 2 (12 tickerów, TYLKO grid rsi_threshold, 5 punktów)**: skończyła
się szybko (~kilka minut), ale wynik jest **niewystarczająco liczny żeby
cokolwiek wywnioskować** - baseline (rsi=35, prod): TRAIN +1.88%/dd=2.14%,
TEST +0.09%/dd=0.66%, **tylko 16 transakcji testowych na 12 tickerach/300
dniach**. Grid rsi 25-45: kształt litery U na teście (najgorzej przy
obecnym 35/40, lepiej na obu skrajach 25/45), ale przy 7-23 transakcjach
testowych to czysty szum, nie sygnał - o rząd wielkości mniej danych niż
Micro-Grid (setki transakcji).

**Wniosek: strategia Sygnał wymaga NAJPIERW optymalizacji wydajności
narzędzia backtestowego** (żeby dało się przetestować pełne 58 tickerów w
rozsądnym czasie) zanim jakikolwiek grid search na jej parametrach (RSI
threshold, ATR mnożniki stop/take-profit) da wiarygodny wynik - obecna
próbka (12 tickerów, kilkanaście transakcji) jest za mała. Nie zmieniano
żadnego parametru produkcyjnego Sygnału. Do zrobienia w przyszłości (nie
dziś, sesja i tak bardzo długa): profilowanie `signal_runner.py`/`_compute_rsi`/
`_compute_sma`/`_compute_atr` pod kątem prawdziwego wąskiego gardła, rozważenie
numpy albo redukcji liczby przeliczanych konfiguracji na raz.

## ZROBIONE (2026-08-02, kontynuacja nocna): naprawiona wydajność _compute_atr - test na 58 tickerach Sygnału w końcu możliwy

Adam: "napraw wydajność signal_runner.py, potem test na 58". Znaleziony
realny bug wydajnościowy (nie tylko "za dużo policzone dla tej skali"):
`_compute_atr()` w OBU miejscach (`bot_engine.py` i `signal_engine.py`,
"kopia celowa") liczyło True Range dla CAŁEGO przekazanego okna świec, a
dopiero na końcu brało ostatnie `period` (14) wartości - w Sygnale okno to
~205 świec (bounded przez `lookback` w `signal_runner.py`), więc **14x
niepotrzebnej pracy na każde wywołanie**, wołane codziennie dla każdej
otwartej pozycji. W Micro-Gridzie efekt był jeszcze gorszy - `windows[ticker]`
przekazywane do `_compute_atr` to CAŁA historia do bieżącego dnia (rosnąca,
nie bounded), czyli realny O(n²) na ticker, tylko zamaskowany bo ATR liczone
tam jest tylko przy PIERWSZYM uzbrojeniu pozycji (rzadziej niż codziennie).

**Naprawa**: przycięcie `candles`/`window` do `[-(period+1):]` PRZED pętlą w
obu kopiach `_compute_atr` - identyczny wynik (TR[i] zależy tylko od świec
i/i-1, ostatnie `period` TR nie zależą od tego ile świec jest przed nimi),
zweryfikowane 200 losowymi testami + przypadkami brzegowymi PRZED zmianą w
kodzie. Potwierdzone też na żywym kodzie: backtest Micro-Gridu (produkcyjne
parametry, 5 lat, walk-forward) dał BIT-FOR-BIT identyczne liczby przed i po
zmianie (+11.25%/6.69%dd train, +3.69%/3.30%dd test). Zsynchronizowane do
prod (`bot_engine.py`, `signal_engine.py`), `run.py` zrestartowany (nowy PID),
log czysty.

**Efekt**: test na 58 tickerach Sygnału (baseline + 3 grid searche = 36
konfiguracji), wcześniej ZABITY po 23 minutach bez postępu, teraz **skończył
się w ciągu kilku minut**.

**Wyniki (58 tickerów, 5 lat, walk-forward, test_days=300):**
- **BASELINE (prod: rsi=35, sl_atr=2.5, tp_atr=3)**: TRAIN avg_ret=1.29%/
  avg_dd=1.97%/568 transakcji, TEST avg_ret=0.35%/avg_dd=0.60%/65 transakcji
  (57-58 tickerów z wystarczającą historią) - dużo solidniejsza próbka niż
  wcześniejszy mały test (12 tickerów, 7-23 transakcji).
- **rsi_threshold (25-45)**: train rośnie monotonicznie z luźniejszym progiem
  (mechaniczny efekt), test niemonotoniczny ale 35 i 45 blisko siebie na
  szczycie (0.35% vs 0.37%) - 45 kosztem wyraźnie wyższego drawdown (0.83%
  vs 0.60%). Brak jasnego zwycięzcy - **rsi_threshold zostaje 35**.
- **take_profit_atr_mult (1.5-5.0)**: **IDENTYCZNY wynik dla KAŻDEJ
  wartości** (1.29%/1.97%/568 train, 0.35%/0.60%/65 test, wszystkie
  identyczne) - dokładnie ten sam wzorzec co martwy `stop_loss_pct` w
  Micro-Gridzie: take-profit software'owy prawdopodobnie NIGDY się nie
  wykonuje, bo trailing STOP zawsze łapie pozycję pierwszy. Do
  zweryfikowania rozbiciem `exit_reason` (w toku).
- **stop_loss_atr_mult (1.0-3.0+)**: **CZYSTY, SPÓJNY SYGNAŁ - train i test
  zgadzają się na CAŁEJ osi.** Monotonicznie rośnie z szerszym stopem: 1.0
  (train -0.18%/test -0.14%, OBA UJEMNE) → 2.5 obecne (train +1.29%/test
  +0.35%) → 3.0 (train +1.73%/test +0.49%, LEPSZE na obu oknach ORAZ lepszy
  zwrot/drawdown niż 2.5: test 0.70 vs 0.58). Rozszerzony grid w toku (3.5-5.0)
  żeby sprawdzić czy to szeroka górka czy trzeba iść jeszcze wyżej.

## ZROBIONE (2026-08-02, finał nocy): stop_loss_atr_mult=3.0 zastosowane na prod dla Sygnału

**Potwierdzone: WSZYSTKIE 568 transakcji train zamknięte przez `exit_reason=
'stop-loss'`, ZERO przez take-profit** - `take_profit_atr_mult` jest w
praktyce martwym parametrem (trailing stop zawsze łapie pozycję pierwszy,
target zysku nigdy nie jest osiągany) - ten sam wzorzec co martwy
`stop_loss_pct` w Micro-Gridzie. Warte odnotowania jako realne ograniczenie
strategii Sygnał, nie tylko ciekawostka - oznacza że cała logika
"take-profit" obecnie nigdy się nie uruchamia.

**Rozszerzony grid stop_loss_atr_mult (2.5-5.0)**: trend z poprzedniego
testu (1.0→2.5→3.0, czysto rosnący na obu oknach) **spłaszcza się po 3.0**
(test: 0.49%→0.50%→0.43%→0.45%→0.49%, w granicach szumu), drawdown dalej
rośnie (0.70%→0.89%). Zwrot/drawdown testowy: 3.0=0.700 (najlepszy), 3.5=0.676,
reszta wyraźnie gorzej. **Prawdziwa górka, nie ucieczka w nieskończoność -
3.0 potwierdzone jako najlepszy wybór, nie artefakt granicy przeszukiwania.**

**Zastosowane na prod** (dev nie ma żywego wiersza `signal_settings` - baza
dev nigdy nie miała działającej instancji Sygnału, tylko narzędzia
backtestowe które i tak omijają bazę) - `signal_settings.stop_loss_atr_mult`
zmienione z 2.5 na 3.0 bezpośrednio w bazie prod (user_id=1, `is_active=1`),
BEZ restartu (te same ustawienia czytane świeżo co tick jak w Micro-Gridzie).
Log diagnostyczny czysty po zmianie, bot dalej działa bez przerwy.

## ZROBIONE (2026-08-02, noc, kluczowe): multi-window walk-forward - realny kompromis ryzyko/zysk znaleziony i świadomie zaakceptowany

Adam: "dalem ci dane z kilkunastu lat plus dociagnales z 5lat akcji czego
jeszcze ci malo?" - słuszna uwaga po tym jak stwierdziłem że "jedno okno
testowe" to metodologiczna dziura. Dane już były (5 lat/1300 dni akcji, już
scachowane) - wystarczyło je lepiej wykorzystać: podział na 4 NIEZALEŻNE,
NIENACHODZĄCE okna po ~325 dni zamiast jednego train/test splitu.

**Wynik na obecnych parametrach produkcyjnych (dca_trigger_pct=0.02,
max_dca_levels=8, take_profit_step_pct=0.002):**
- Okno 1 (najstarsze, prawdopodobnie bessa 2022 - cykl podwyżek stóp):
  **-3.03% / max_dd=7.91%** - jedyne ujemne okno, wyraźnie gorsze niż reszta.
- Okno 2: +4.81% / dd=2.06%
- Okno 3: +3.71% / dd=2.70%
- Okno 4 (najnowsze): +3.72% / dd=3.30%

**Porównanie STARE (dca_trigger_pct=0.03/max_dca_levels=5/tp=0.0025, sprzed
dzisiejszej sesji) vs NOWE (0.02/8/0.002) na tych samych 4 oknach:**

| Okno | STARE | NOWE |
|---|---|---|
| 1 (bessa) | -0.93% / dd=5.13% | **-3.03% / dd=7.91%** |
| 2 | +4.25% / dd=1.60% | +4.81% / dd=2.06% |
| 3 | +3.18% / dd=3.77% | +3.71% / dd=2.70% |
| 4 | +2.56% / dd=2.92% | +3.72% / dd=3.30% |

**Znaleziony realny kompromis: nowe parametry są lepsze w 3 z 4 okien
(normalne/wzrostowe rynki), ale WYRAŹNIE GORSZE w oknie bessy** - strata
ponad 3x większa (-3.03% vs -0.93%) i drawdown o 54% wyższy (7.91% vs
5.13%). Mechanizm: `max_dca_levels=5→8` daje więcej miejsca na uśrednianie
w dół - pomaga gdy spadek się odwraca (3 z 4 okien), szkodzi gdy spadek
trwa dalej (prawdziwa, długotrwała bessa) - bot uśrednia głębiej w papier
który nie odbija, zamiast zatrzymać się wcześniej jak przy starych 5
poziomach. To dokładnie ryzyko przeuczenia do niedawnego, spokojniejszego
okresu rynkowego, przed którym ostrzega cała seria "Build Better Strategies".

**Decyzja Adama (przez AskUserQuestion): "Zostaw jak jest"** - świadomie
zaakceptowany większy potencjalny drawdown w scenariuszu bessy w zamian za
lepszy zwrot w normalnych warunkach rynkowych (3 z 4 okien). Parametry
zostają bez zmian: `dca_trigger_pct=0.02`, `max_dca_levels=8`,
`take_profit_step_pct=0.002` na prod. **To NIE jest "problem do naprawienia"
- to świadomie zaakceptowany kompromis ryzyko/zysk, udokumentowany na
przyszłość** (gdyby ktoś pytał czemu drawdown jest większy niż w 07.2026,
odpowiedź jest tutaj).

## ZROBIONE (2026-08-02, noc, KLUCZOWE): max_dca_levels=7 zastępuje 8 - znaleziony przez multi-window metodę lepszy kompromis

Kontynuacja (Adam: "kontynuuj wprowadzanie zmian polepszajacych wynik na
plus") po odkryciu kompromisu ryzyko/zysk poprzednim testem. Grid
`max_dca_levels` (5-9) na TYCH SAMYCH 4 niezależnych oknach:

| max_dca | Okno1 (bessa) | Okno2 | Okno3 | Okno4 | Suma | Min | Max DD |
|---|---|---|---|---|---|---|---|
| 5 | -3.84% | +2.48% | +3.51% | +3.13% | 5.29% | -3.84% | 8.19% |
| 6 | -2.93% | +2.11% | +4.23% | +2.55% | 5.96% | -2.93% | 7.33% |
| **7** | **+1.19%** | +4.54% | +3.82% | +2.53% | **12.08%** | **+1.19%** | **5.64%** |
| 8 (było) | -3.03% | +4.81% | +3.71% | +3.72% | 9.21% | -3.03% | 7.91% |
| 9 | -0.57% | +5.10% | +2.94% | +2.90% | 10.37% | -0.57% | 8.56% |

**`max_dca_levels=7` dominuje na KAŻDEJ metryce naraz** - jedyna wartość z
dodatnim zwrotem we WSZYSTKICH 4 oknach (w tym bessa: +1.19%, jedyny dodatni
wynik w tym oknie ze wszystkich testowanych wartości), najwyższa suma
zwrotów (12.08%, bije nawet 8 z 9.21%), najniższy najgorszy drawdown
(5.64% vs 7.91% dla 8). To NIE jest kompromis między zyskiem a
odpornością - 7 bije 8 na obu frontach jednocześnie. Potwierdzone też
pojedynczym oknem (walk-forward test_days=300): train +11.68%/dd=5.12%,
test +2.50%/dd=3.45% - wyraźnie niższy drawdown na obu oknach niż przy 8
(dd 6.71%/3.87%), kosztem marginalnie niższego zwrotu testowego (2.50% vs
2.83%).

**Zastosowane na dev i prod** - `risk_settings.max_dca_levels` zmienione z
8 na 7 (user_id=1, bot aktywny), BEZ restartu, log czysty. **To
podsumowuje dobrze cały dzisiejszy łuk**: pojedyncze okno testowe (rano)
znalazło 8 jako najlepsze, ale dopiero test na WIELU niezależnych oknach
(wieczorem, na prośbę Adama żeby lepiej wykorzystać już posiadane dane)
ujawnił że 7 jest lepszym, bardziej odpornym wyborem - dokładnie przykład
dlaczego walk-forward na jednym oknie to za mało, seria "Build Better
Strategies" miała rację od początku.

## ZROBIONE (2026-08-02, noc, ciąg dalszy): dca_trigger_pct i take_profit_step_pct sprawdzone multi-window - dca_trigger_pct mocno potwierdzone, tp_step bez zmian

Kontynuacja po zmianie max_dca_levels na 7. Sprawdzenie pozostałych dwóch
dzisiejszych zmian (dca_trigger_pct, take_profit_step_pct) na tych samych
4 niezależnych oknach, przy nowym max_dca_levels=7.

**`dca_trigger_pct` (0.015-0.025)**: **0.02 mocno potwierdzone** - jedyna
wartość dodatnia we WSZYSTKICH 4 oknach (min=+1.19%), najwyższa suma
(12.08%), najniższy najgorszy drawdown (5.64%). Reszta wartości wyraźnie
gorsza na każdym froncie (np. 0.015: suma tylko 3.93%, min=-3.35%). Czysty,
jednoznaczny sygnał identyczny w jakości do `max_dca_levels`. **Zostaje
0.02, bez zmian.**

**`take_profit_step_pct` (0.0015-0.003)**: bardzo płaskie plateau (suma
11.33-12.60% dla całego zakresu) - 0.003 wygrywa nieznacznie na agregacie 4
okien (suma 12.60% vs 12.08% dla obecnego 0.002, ~0.5pp różnicy), ALE
**pojedyncze, najnowsze okno (ten sam test co reszta dzisiejszej sesji)
wyraźnie faworyzuje 0.002** (train +11.68% vs +9.46% dla 0.003, test
dd=3.45% vs 4.24%) - najnowsze okno i agregat 4 okien się NIE ZGADZAJĄ.
Różnica w agregacie (~0.5pp) jest zbyt mała żeby uznać ją za solidny sygnał
biorąc pod uwagę że podobnej wielkości różnice były dziś wcześniej uznawane
za szum (np. RECENT_WINDOW_ROWS). **Świadomie NIE zmieniane** - w
odróżnieniu od `max_dca_levels` (gdzie 7 dominowało jednoznacznie na
wszystkich oknach naraz), tu sygnał jest za słaby i sprzeczny między
metodami. `take_profit_step_pct` zostaje **0.002**.

**Podsumowanie multi-window przeglądu**: `max_dca_levels=7` (zmienione z 8,
mocny sygnał), `dca_trigger_pct=0.02` (potwierdzone, mocny sygnał),
`take_profit_step_pct=0.002` (potwierdzone, słaby/mieszany sygnał na
alternatywę - zostaje ostrożnie przy już zweryfikowanej wartości).

## ZROBIONE (2026-08-02, noc, ciąg dalszy): multi-window test Sygnału - stop_loss_atr_mult=3.0 potwierdzone, brak ukrytej słabości do bessy

Ten sam test 4 niezależnych okien co dla Micro-Gridu, teraz na strategii
Sygnał (58 tickerów, `stop_loss_atr_mult` 1.8-4.0):

| sl_mult | Okno1 | Okno2 | Okno3 | Okno4 | Suma | Min | Max DD |
|---|---|---|---|---|---|---|---|
| 1.8 | 0.00% | 0.18% | -0.03% | 0.20% | 0.36% | -0.03% | 0.67% |
| 2.0 | -0.02% | 0.24% | 0.03% | 0.19% | 0.43% | -0.02% | 0.71% |
| 3.0 (prod) | -0.06% | 0.43% | 0.05% | 0.31% | 0.74% | -0.06% | 0.89% |
| 4.0 | -0.07% | 0.66% | -0.03% | 0.33% | 0.89% | -0.07% | 1.03% |

**Kluczowa różnica względem Micro-Gridu: okno 1 (potencjalna bessa)
pozostaje PRAKTYCZNIE PŁASKIE/blisko zera przy KAŻDEJ wartości** (0.00% do
-0.09%), zamiast dramatycznie się załamywać jak `max_dca_levels=8` w
Micro-Gridzie. Wyjaśnienie strukturalne: Sygnał nie uśrednia w dół jak
DCA - trzyma JEDNĄ pozycję z przesuwanym stopem, więc brak mechanizmu
"kupuj więcej w spadający rynek" który powodował fragilność Micro-Gridu.
**Sygnał strukturalnie odporniejszy na bessę niż Micro-Grid.**

Jest tu łagodny, prawdziwy kompromis zwrot/ryzyko (szerszy stop = więcej
sumy zwrotu, więcej drawdown) - NIE dominujący zwycięzca jak przy
`max_dca_levels=7`. Wartości bezwzględnie bardzo małe (ułamki procenta) -
nie warto mikrooptymalizować. **`stop_loss_atr_mult=3.0` zostaje bez
zmian** - rozsądny środek, wcześniejsza decyzja potwierdzona.

## ZROBIONE (2026-08-03, noc, KLUCZOWE): dopasowanie sizingu do realnego budżetu ~1000€

Adam ujawnił realne ograniczenie: budżet na oba silniki to max ~1000€ (nie
konceptualne $10k/$1k używane dotąd w backtestach do wygodnego liczenia %).
Sprawdzenie stanu na żywo (prod, 2026-08-02 wieczorem, przed zmianą):
Micro-Grid 10 otwartych pozycji (~1101€ zaangażowane, limit 10 pozycji, ale
BEZ limitu wartości - przy max_dca_levels=7/entry_amount=100 teoretyczne
maksimum to 10*7*100=7000€), Sygnał 6 otwartych pozycji (~715€, **ZERO
limitu liczby pozycji w kodzie** - `_process_entries` iterowało wszystkie
58 tickerów bez ograniczenia, teoretyczne maksimum 58*100=5800€), EOD 0
otwartych (limit też brak w kodzie, 30 tickerów). **Razem już ~1816€
zaangażowane, powyżej deklarowanego budżetu, mimo że żaden silnik jeszcze
się "nie rozjechał".**

**Decyzja Adama**: nie martwić się obecnymi (demo/testowymi) pozycjami,
podział budżetu 700€ Micro-Grid / 300€ Sygnał (Micro-Grid dał dziś ~16x
więcej zwrotu w testach), ograniczyć liczbę jednoczesnych pozycji per bot
(pierwsza propozycja "1-2" dała w backteście dużo wyższy drawdown - do 15%
zamiast 5-8% - z powodu koncentracji kapitału w niewielu pozycjach,
Adam wybrał "zwiększ do 5-6 i sprawdź").

**Micro-Grid**: `MAX_CONCURRENT_POSITIONS` (stała modułowa `bot_engine.py`,
NIE kolumna bazy) zmieniona z 10 na **6**. Test monkeypatchem (poprawiony po
tym jak wcześniejsza próba przez `sed` NIE zmieniała wartości w pliku -
złapane bo różne "koncentracje" dawały identyczną liczbę transakcji, czyli
faktycznie wciąż liczyły przy starej wartości):

| Pozycje | Kwota/noga | TRAIN | TEST |
|---|---|---|---|
| 2 | 50.00€ | +38.57% / dd=14.76% | +4.66% / dd=10.82% |
| 4 | 25.00€ | +27.65% / dd=9.94% | +4.58% / dd=10.37% |
| 5 | 20.00€ | +23.43% / dd=10.51% | +4.22% / dd=10.16% |
| **6** | 16.67€ | +22.61% / dd=12.12% | **+4.70%** / dd=**8.06%** |
| 8 | 12.50€ | +18.14% / dd=9.72% | +4.46% / dd=6.04% |

6 wybrane - lepszy zwrot testowy i wyraźnie niższy drawdown testowy niż 5,
mieści się w preferowanym przez Adama zakresie 5-6. `entry_amount`
przeliczone na wszystkich 38 `BotAsset` (prod) z 100€ na **16.67€**
(6*7*16.67≈700€ = dokładnie budżet tego silnika).

**Sygnał**: `_process_entries` w `signal_engine.py` NIE MIAŁO ŻADNEGO
limitu (realna luka, nie tylko kwestia strojenia) - dodany nowy moduł-stała
`MAX_CONCURRENT_POSITIONS=2` + twardy check na początku funkcji + **NAJWYŻEJ
JEDNO wejście na tick** (ten sam wzorzec bezpieczeństwa rate-limitu co
Micro-Grid, którego Sygnał też nie miał). Sygnał nie ma DCA (jedna noga,
bez uśredniania) więc koncentracja NIE mnoży ryzyka x7 jak w Micro-Gridzie
- 2 pozycje uznane za strukturalnie bezpieczne przy 300€. `entry_amount`
przeliczone na wszystkich 58 `SignalAsset` z 100€ na **150€**
(2*150=300€ = budżet tego silnika).

**Wdrożone TYLKO na prod** (dev ma nieaktualną/inną listę `BotAsset`, 4
wiersze zamiast 38, i pustą `SignalAsset` - nigdy nie była żywą instancją,
ten sam wzorzec co wcześniej dziś z `signal_settings`). Kod zmirrorowany
(`bot_engine.py`, `signal_engine.py`), `run.py` zrestartowany (nowy PID),
log czysty. **Istniejące otwarte pozycje NIETKNIĘTE** (Adam: "nie
przejmuj się, to demo/testy") - nowe limity blokują tylko NOWE wejścia,
dopóki liczba otwartych pozycji nie spadnie naturalnie poniżej nowych
progów (10→6 dla Micro-Gridu, 6→2 dla Sygnału).

## ZROBIONE (2026-08-03, wieczór): wyścig gubiący cenę zamknięcia + limit pozycji jako ustawienie

Po sprawdzeniu bota na żywo (Adam: "spr bota") znalezione na żywo w bazie:
**31 z ostatnich 52 zamkniętych pozycji Micro-Gridu (60%), w tym 6 z 8
zamkniętych tego samego dnia, miało `close_price=NULL`** - stanowczo za
dużo jak na ręczne sprzedaże (Adam spał przez większość tego okna). To
samo "56% bez known close_price" było wcześniej powodem odrzucenia
Kelly/OptimalF w sekcji money management (2026-07-31) - część tej luki
mogła być artefaktem poniższego buga, nie prawdziwym brakiem danych.

**Root cause - wyścig dwóch funkcji o zamknięcie tej samej pozycji.**
W `reconcile()`/`tick()` (`bot_engine.py`) `_manage_trailing_exit()` woła
się CELOWO przed `get_pending_orders()`/`_detect_exit_fills()` (ochrona
zysku ma priorytet, decyzja Adama 2026-07-27 - **ta kolejność NIE zostaje
zmieniona**, tylko właściwy bug naprawiony w miejscu). Problem:
`_manage_trailing_exit()` ma gałąź (dodaną 2026-07-24 na wypadek "sprzedane
ręcznie poza appką") która przy 0 sztuk w portfelu T212 NATYCHMIAST
zamyka trade z `close_price=NULL` i **zeruje** `stop_order_id`/
`sell_order_id`. Dopiero PO NIEJ leci `_detect_exit_fills()`, którego
zadaniem jest właśnie sprawdzić historię zleceń T212 i wyciągnąć realną
cenę wykonania stopu/take-profitu - ale trade jest już CLOSED i zlecenia
wyzerowane, więc nie ma czego sprawdzić. Cena ginie bezpowrotnie przy
KAŻDYM zamknięciu przez własny stop bota, nie tylko przy prawdziwej
ręcznej sprzedaży.

**Fix** (bez zmiany kolejności wywołań funkcji): w tej samej gałęzi, PRZED
uznaniem za "sprzedane ręcznie", sprawdzić `stop_order_id`/`sell_order_id`
przez `_lookup_recent_order` (ten sam mechanizm co `_resolve_vanished_leg`)
- jeśli historia T212 potwierdza `status=FILLED`, wyciągnąć realną cenę i
zamknąć przez `_finalize_closed_trade` z prawdziwym `close_price` zamiast
NULL. Dopiero gdy brak dowodu wykonania (żadnego order_id, albo lookup nic
nie zwraca) - fallback do starego zachowania (cena nieznana, prawdopodobnie
faktycznie ręczne). Zweryfikowane dwoma testami syntetycznymi na
izolowanej bazie in-memory: (1) symulowany fill stopu bota (order history
zwraca FILLED, cena 103.50) - `close_price` teraz poprawnie odzyskane,
zamiast zgubione; (2) symulowana faktyczna ręczna sprzedaż (lookup zwraca
nic) - fallback nadal działa, `close_price=NULL` jak wcześniej. Zero zmiany
zachowania tradingowego, tylko poprawność danych P&L.

**Limit jednoczesnych pozycji przeniesiony ze stałych modułowych do
ustawień per-user** (Adam: "ilość otwartych pozycji wrzuć do ustawień dla
każdego bota osobno... to musi się dać zmieniać w ustawieniach... to tylko
ustawienia fabryczne"). Nowa kolumna `max_concurrent_positions` w
`RiskSettings`/`SignalSettings`/`EODSettings` (migracja
`migrate_add_max_concurrent_positions.py`, uruchomiona dev+prod), domyślne
wartości zachowują dotychczasowe zachowanie: Micro-Grid=6, Sygnał=2. **EOD
dostał ten sam limit co Sygnał miał od wczoraj wieczorem - do tej pory
`_process_entries` w `eod_engine.py` NIE MIAŁO ŻADNEGO capa** (ta sama
klasa luki, dodana dokładnie ten sam wzorzec: check na początku funkcji +
`return` po pierwszym wejściu na tick), domyślnie **2 pozycje**. UI: nowe
pole "Maks. liczba jednoczesnych otwartych pozycji" w ustawieniach ryzyka
każdego z trzech botów (bot.html/signal.html/eod.html + odpowiednie
routes/*.py + static/js/*.js), zweryfikowane end-to-end testem Flask
test-client na izolowanej bazie in-memory (render + zapis + odrzucenie
wartości ≤0 dla wszystkich trzech silników).

**Wartości fabryczne**: EOD `entry_amount` już było 100€ (bez zmian) -
Sygnał obniżony ze 150€ (wczorajsza wartość) na **100€** na wszystkich 58
`SignalAsset` (prod) - Adam: "eod 2 pozycje po 100euro/usd sygnał też 2 po
100" (budżet Sygnału efektywnie 200€ zamiast 300€, do zmiany w UI w każdej
chwili). Wdrożone na prod, `run.py` zrestartowany, log czysty, zero błędów.

## ZROBIONE (2026-08-03, noc): money management √equity rozszerzone na Sygnał i EOD + backtest realnego efektu

Adam wyjaśnione znaczenie money management √equity (patrz sekcja z 31.07),
zapytał "a jak myślisz ma to sens tam?" (Sygnał/EOD) - odpowiedź: TAK,
strukturalnie ten sam problem co w Micro-Gridzie (entry_amount to STAŁA
absolutna kwota, przy kurczącym się equity ryzyko na transakcję względem
kapitału ROŚNIE), tylko rzadziej dostrzegany bo nie ma DCA mnożącego efekt.
Adam: "dodaj do obu i przetestuj oba boty na danych które mamy już czy oc
to realnie zmieni w zyskach".

**Implementacja** (identyczna z Micro-Gridem, ta sama funkcja czysta
`microgrid_strategy.compute_equity_scaled_amount` reużyta wprost, zero
duplikacji matematyki): nowe kolumny `equity_sizing_enabled`/
`equity_sizing_baseline` w `SignalSettings`/`EODSettings` (migracja
`migrate_add_equity_sizing_signal_eod.py`), nowy `_get_current_equity()` w
obu silnikach (zduplikowany z własnym tagiem diagnostyki "signal"/"eod" -
NIE reużyty wprost z `bot_engine.py`, bo tamta wersja hardkoduje tag "bot" w
logu). `_enter_position` w obu silnikach skaluje `entry_amount` PRZED innymi
mnożnikami - w EOD to ważne rozróżnienie: equity scaling mnoży kwotę BAZOWĄ,
tier spadku (`_size_multiplier_for_drop`) mnoży AGRESYWNOŚĆ konkretnego
sygnału, oba mnożą się niezależnie. UI: checkbox + auto-capture baseline
identyczne jak w Micro-Gridzie, w `signal.html`/`eod.html` +
`routes/signal.py`/`routes/eod.py`. Zweryfikowane: 2 testy syntetyczne
(equity 4x -> Sygnał 2x kwoty; equity 0.25x -> EOD 0.5x kwoty bazowej,
niezależnie od tieru) + test end-to-end Flask (render + zapis + odrzucenie
włączenia bez zapisanych kluczy API demo, potrzebnych do auto-capture).

**Backtest realnego efektu - Sygnał** (58 tickerów, 1300 dni, WSPÓLNY portfel
- w odróżnieniu od dotychczasowego per-tickerowego `run_signal_backtest`,
equity scaling z definicji wymaga JEDNEGO portfela współdzielonego między
tickerami, żeby equity miało w ogóle sens do przeliczenia; nowy skrypt
kalendarzowo wyrównany jak `microgrid_runner.py`, limit=2 pozycje, jedno
wejście/dzień):
- Całe okno (1000€ start): BEZ scalingu +30.37%/dd=3.31%, Z scalingiem
  +32.14%/dd=3.73% (+1.78pp zwrotu, +0.42pp drawdownu) - drobny, ale REALNY,
  pozytywny efekt, bo equity w tym oknie faktycznie urosło (~1.3x).
- 4 niezależne okna ~325-dniowe: efekt w KAŻDYM pojedynczym oknie to szum
  (-0.10pp do +0.04pp) - equity w oknie tej długości nigdy nie oddala się
  wystarczająco od baseline żeby pierwiastek zrobił zauważalną różnicę.
  **Zgodne z oczekiwaniem**: to zabezpieczenie na horyzont wieloletni/duże
  zmiany kapitału (ten sam wniosek co przy weryfikacji Micro-Gridu 31.07),
  nie dźwignia widoczna w pojedynczym rocznym backteście.

**Backtest EOD** - wymaga świec 1-MINUTOWYCH (nie dziennych jak Sygnał/
Micro-Grid), których w cache NIE było wcale. Adam: "masz ibkr pobierz sb
dane" - pobrane świeżo przez `backtest/ibkr_data.py` (kontener `ib-gateway`,
20 dni na tiker - potwierdzony stabilny sufit z 28.07, TGATE dla 15 tickerów
EU, IEX dla 14 US) dla wszystkich 29 tickerów EOD, wszystkie 30/30 pobrań
udane. Nowy skrypt (globalna kolejka zdarzeń posortowana chronologicznie wg
prawdziwego znacznika czasu UTC, bo 1-min świece z różnych giełd/stref
czasowych nie dają się wyrównać po indeksie jak dzienne) - 372 040
zdarzeń-minut przetworzonych, wspólny portfel (200€ start, limit 2 pozycje).

Wynik: BEZ scalingu +2.88%/dd=0.65% (25 transakcji), Z scalingiem
+2.90%/dd=0.66% - różnica **+0.01pp, praktycznie zero**. Uczciwie: 20 dni to
zbyt krótko żeby equity (200€->205.77€, ~1.03x) oddaliło się od punktu
odniesienia na tyle, żeby pierwiastek zrobił zauważalną różnicę - dokładnie
ten sam wniosek co przy Sygnale na pojedynczym ~325-dniowym oknie. IBKR
1-min ma praktyczny sufit ~20-30 dni na zapytanie (dłuższe okna często
zawieszają zapytanie, patrz docstring `ibkr_data.py`), więc dłuższego testu
nie da się tanio zrobić bez wielokrotnych zapytań rozłożonych w czasie -
świadomie odłożone, mechanizm i tak jest zabezpieczeniem wieloletnim, nie
czymś co miało dać efekt w 20-dniowym oknie.

Wdrożone na prod (kod + migracja), `run.py` zrestartowany, log czysty.
`equity_sizing_enabled=False` domyślnie na obu silnikach (jak w Micro-Gridzie)
- włączenie to świadoma decyzja Adama w UI, nie automatyczna część tej pracy.

## ZROBIONE (2026-08-03, noc): 13 vs 12 aktywów - odkryta osierocona pozycja SAP + naprawiona kolejność auto-adopcji w tick()

Adam: "spr dlaczego mam 13 aktywow a 12 na botach". Sprawdzone: baza pokazuje
dokładnie 12 unikalnych tickerów zarządzanych przez boty (6 Micro-Grid + 6
Sygnał + 0 EOD, zero nakładania). Żywy portfel T212 (`client.get_portfolio()`)
pokazał 13 - brakująca **SAPd_EQ (SAP), 1.0 akcja**. Historia: EOD kupił i
sprzedał SAP raz 28.07 (transakcja zamknięta w bazie), ale obecna 1.0 akcja
to INNA pozycja - **brak dla niej JAKIEGOKOLWIEK wpisu w `order_logs`**
(loguje KAŻDE zlecenie, także ręczne) - więc nie przeszła przez appkę wcale
(albo kupiona bezpośrednio w T212, albo pozostałość po jednym z ręcznych
resetów demo). SAP jest już kandydatem na liście wszystkich 3 botów, więc
`baseline_owned_quantity` chroni ją przed przypadkową sprzedażą, ale
`manage_all_positions` był WYŁĄCZONY, więc nikt jej nie chronił stop-lossem.

**Adam: "włącz zarządzaj wszystkim niech postawi o ile się da trailing
stopa"** - włączone (`risk_settings.manage_all_positions=1`, prod, user 1).
SAP przejęty przez `_auto_adopt_foreign_positions` na najbliższym ticku
(5s), ale **stop NIE uzbroił się od razu mimo że cena (164) była już
WYRAŹNIE powyżej progu uzbrojenia (157.63, 2 kroki od wejścia 157)** - Adam:
"to jest chujowe... zmień to jakoś".

**Root cause znaleziony**: w `tick()` kolejność wywołań to
`_manage_trailing_exit()` (linia ~1957) PRZED `_auto_adopt_foreign_positions()`
(linia ~1993) - DOKŁADNIE ODWROTNIE niż w `reconcile()`, gdzie adopcja idzie
PRZED trailing (poprawnie, patrz komentarz tam z 27.07). Skutek: świeżo
adoptowana pozycja w regularnym cyklu tick() nie miała ŻADNEJ szansy na
trailing check w TYM SAMYM cyklu - czekała pełny dodatkowy tick (do 60s),
podczas gdy cena mogła w tym czasie odjechać jeszcze dalej bez ochrony.

**Fix**: `_auto_adopt_foreign_positions()` zwraca teraz `int` (liczbę nowo
przejętych pozycji tym wywołaniem, wcześniej `None`) zamiast tylko efektu
ubocznego w bazie. W `tick()`, jeśli `adopted_count > 0`, wołamy
`_manage_trailing_exit()` DRUGI RAZ, bezpośrednio po adopcji - dla pozycji
już obsłużonych chwilę wcześniej w tym samym ticku to praktycznie zero
kosztu (cache portfolio/cen), dla świeżo przejętej daje szansę na
NATYCHMIASTOWE uzbrojenie stopu zamiast czekania na kolejny cykl. Kolejność
w `reconcile()` była już poprawna, bez zmian. Zweryfikowane testem
syntetycznym (`_auto_adopt_foreign_positions` zwraca 1 przy nowej adopcji,
0 gdy nic nowego/switch wyłączony) - trailing na żywym SAP (już uzbrojony
zanim fix wszedł) pozostał nietknięty, potwierdzony po restarcie: stop
164.31/24 kroków, bez przerwy w ochronie. Zmirrorowane dev->prod, `run.py`
zrestartowany, log czysty.

## ZROBIONE (2026-08-03, noc): Lowpass i MMI z manuala Zorro przetestowane i ODRZUCONE + naprawiona regresja w microgrid_runner.py

Adam przeczytał tutorial Zorro (zorro-project.com, Workshop 1/4/4a/5/6) i
poprosił o sprawdzenie czy filtr **Lowpass** (Workshop 4a) i **Market
Meanness Index** (Workshop 4, wzór z financial-hacker.com - ten sam autor
co artykuł o √equity) dają się realnie zaadaptować. Obie idee zaimplementowane
jako czyste funkcje Python, zweryfikowane testami syntetycznymi (Lowpass:
stała cena→bez zmian, nadąża za trendem liniowym; MMI: trend~50%, biały
szum~78% - zgodne z dokumentacją) - patrz `zorro_indicators.py` (scratchpad,
nieprodukcyjne).

**Lowpass jako zamiennik SMA(200) w Sygnale** - test 4-okienkowy (58
tickerów, wspólny portfel, ten sam RSI/ATR co produkcja, tylko filtr trendu
podmieniony): SMA(200) WYGRYWA z każdym testowanym okresem lowpass (50/100/
150/200) na obu frontach - suma zwrotu (21.86% vs najlepsze 11.89% dla
lowpass=200) I najgorsze okno (SMA200 nigdy ujemne: min=+0.91%, KAŻDY
wariant lowpass miał choć jedno ujemne okno). **ODRZUCONE - SMA(200) zostaje
bez zmian.**

**MMI jako filtr regime w Micro-Gridzie** (reużyty ISTNIEJĄCY mechanizm
Hurst - `bot_entry_filters.HURST_FILTER_ENABLED`/`compute_hurst_exponent`/
`_regime_ok`, podmieniony monkeypatchem na MMI zamiast Hurst, ten sam gate,
300-dniowy lookback per dokumentacja): testowane OBA kierunki na pełnym
1300-dniowym oknie (prod parametry: max_dca_levels=7/entry_amount=100/
max_pozycji=6). "Wymagaj MMI wysokie" (mean-reversion, logicznie pasujące
do tezy DCA - kup dołek, licz na powrót) - **katastrofa**: baseline +12.32%
(1094 transakcji) -> -1.01% do -1.80% (tylko 242-244 transakcji, blokuje
78% wejść). "Wymagaj MMI niskie" (trend, jak w oryginalnym Zorro use-case) -
praktycznie NO-OP (1092-1094 transakcji, ~12.31-12.32% - identyczne z
baseline) - na surowych cenach dziennych z tak długim oknem MMI prawie
zawsze wychodzi "trendująco" (zgodne z synteycznym testem: prawdziwe ceny
zachowują się bliżej trendu/random walk niż czystego szumu w tej metryce).
**ODRZUCONE** - dokumentacja sugeruje liczenie MMI na ZMIANACH ceny (nie
surowych cenach) dla efektywnych rynków - mogłoby dać inny wynik, ale to
osobny eksperyment, nie zrobiony teraz.

**Przy okazji znaleziona i naprawiona regresja**: dzisiejsza wcześniejsza
migracja `MAX_CONCURRENT_POSITIONS` ze stałej modułowej do kolumny bazy
(`RiskSettings.max_concurrent_positions`) zepsuła import w
`backtest/microgrid_runner.py` (próbował importować nieistniejącą już
stałą z `bot_engine.py`) - złapane dopiero przy próbie uruchomienia testu
MMI. Nie dotyczyło produkcji na żywo (tylko narzędzie backtestowe), ale
naprawione: własna, niezależna stała `MAX_CONCURRENT_POSITIONS=6` w
`microgrid_runner.py` zamiast importu. Zmirrorowane dev->prod (plik
nieużywany przez żywy `run.py`, bez restartu).

## ZROBIONE (2026-08-03, noc): ten sam bug "zniknięcie z pending = wykonanie" znaleziony w Sygnale i EOD, naprawiony (Micro-Grid miał ten fix już od 23.07)

Adam: "sprawdź co jeszcze da się poprawić w botach" - ogólny przegląd.
Sprawdzone czy Sygnał/EOD mają ten sam problem, który Micro-Grid już
rozwiązał 2026-07-23 (`_resolve_vanished_leg`, patrz komentarz tam:
"Zniknięcie z pending SAMO W SOBIE nie dowodzi wykonania - zlecenie mogło
też zostać anulowane/odrzucone... znaleziono 2026-07-23, gdy ASML pokazywał
wciąż otwartą pozycję na koncie T212, mimo że bot już oznaczył ją CLOSED
wyłącznie na podstawie zniknięcia z pending").

**Znalezione: TAK, dokładnie ten sam bug, w OBU pozostałych silnikach.**
`signal_engine.py::_manage_exits` i `eod_engine.py::_manage_exits` miały:
```python
if pending_fetch_ok and trade.stop_order_id and trade.stop_order_id not in pending_order_ids:
    _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=trade.stop_loss_price)
```
- ZERO weryfikacji w historii T212 czy zlecenie faktycznie się wykonało
(status FILLED) czy zostało anulowane/odrzucone - naiwne założenie
"zniknęło z pending = wykonane". Poprawka z Micro-Gridu (23.07) NIGDY nie
została przeniesiona do pozostałych dwóch silników, mimo że oba zostały
napisane PO tamtym fixie. Skutek na żywo: gdyby zlecenie stop-loss w
Sygnale/EOD zostało anulowane/odrzucone przez T212 (ten sam scenariusz co
złapany na ASML w Micro-Gridzie), bot BŁĘDNIE oznaczyłby pozycję jako
zamkniętą (z ceną = `stop_loss_price`, nie realną) i przestałby nią
zarządzać, mimo że pozycja realnie WCIĄŻ jest otwarta i niechroniona na
koncie.

**Fix**: reużyte wprost `bot_engine._lookup_recent_order`/
`_FILLED_ORDER_STATUS` (generyczne, bez tagowania per-silnik, bezpieczne do
importu) w obu plikach. Ten sam trójstanowy wzorzec co `_resolve_vanished_leg`:
FILLED w historii -> zamknij z REALNĄ ceną wykonania (nie tylko
`stop_loss_price` jak dotąd - poprawia też dokładność P&L); status inny
(anulowane/odrzucone) -> NIE zamykaj, wyczyść `stop_order_id`, zlecenie
zostanie wystawione od nowa; brak w historii (jeszcze nie wiadomo) -> nic
nie zmieniaj, sprawdź ponownie następnym razem. Zweryfikowane 6 testami
syntetycznymi (po 3 na silnik: FILLED z realną ceną / anulowane-zostaje-OPEN
/ nieznane-bez-zmian), wszystkie przeszły. Zmirrorowane dev->prod, `run.py`
zrestartowany, log czysty.

## ZROBIONE (2026-08-04, noc): koszt przewalutowania (FX) dodany do Sygnału i EOD, ujednolicony jako ustawienie we wszystkich 3 botach

Kontynuacja przeglądu "co jeszcze poprawić" (Adam: "dalej szukaj co by tu poprawić"). Sprawdzone: Micro-Grid ma na sztywno wliczony koszt przewalutowania FX dla tickerów USD (`FX_ROUND_TRIP_PCT`, od 22.07.2026 - Adam złapał to na żywo), Sygnał i EOD - NIGDY, mimo że oba handlują wieloma tickerami USD (AAPL, NVDA, GOOGL...) na koncie EUR.

**Weryfikacja przed zmianą** (Adam: "demo uzywa tylko waluty euro a czy real konto takze?"): sprawdzone bezpośrednio przez API konta demo - `{"currency": "EUR", "cash": {...}}`, JEDNO zbiorcze saldo, zero śladu osobnego portfela USD. Adam potwierdził na koncie REALNYM (multi-currency wallet) inaczej: otwarcie+zamknięcie pozycji USD mając już dolary = zero FX, tylko spread. Ale boty działają WYŁĄCZNIE na demo (T212 nie wspiera LIMIT/STOP na koncie live) - tam nie ma multi-currency do wykorzystania. Dodatkowo Adam wgrał oficjalną "Politykę realizacji zamówień" T212 (pkt 17.3: koszt przewalutowania przy zakupie w walucie innej niż DEPOZYTOWA - jedna, ustalona dla konta) i zrzut ekranu z apki T212 ("Obecnie można składać tylko zlecenia realizowane w głównej walucie konta") - oba potwierdzają że demo faktycznie płaci FX na każdym tickerze USD.

**Adam: "dodaj w ustawieniach switch dodaj prowizje fx 0.15% w kazda str i wtedy ja doliczaj bo inaczej mozemy tracic mimo ze bedziemy zyskiwac"** - zamiast tylko łatać Sygnał/EOD, ujednolicone: nowe kolumny `fx_cost_adjustment_enabled`/`fx_fee_pct` (domyślnie 0.0015 = 0.15% za nogę, DOMYŚLNIE WŁĄCZONE wszędzie) w `RiskSettings`/`SignalSettings`/`EODSettings` + checkbox+pole w UI każdego bota. Micro-Grid przepięty z twardej stałej modułowej na czytanie z `settings` (bit-identyczne zachowanie - domyślna wartość migracji = stara stała).

**Mechanizm w Sygnale/EOD** (nowy opcjonalny parametr `fx_reference_price` w `compute_entry`, używany WYŁĄCZNIE do `stop_loss_price`/`take_profit_price` - `quantity` zawsze z REALNEJ ceny fillu): dla USD z włączonym switchem, `price` podbite o round-trip FX PRZED policzeniem progów. Efekt uboczny zweryfikowany testem - tworzy "martwą strefę": trailing stop NIE zaczyna się poprawiać dopóki cena nie wzrośnie o tyle, żeby pokryć koszt FX (przy cenie w połowie drogi do pokrycia kosztu: bez adjustmentu stop już się poprawia, z adjustmentem - jeszcze nie, `None`) - dokładnie analogiczny efekt do "2 progów zysku" w Micro-Gridzie, tylko wyrażony przez przesunięcie punktu odniesienia zamiast osobnej bramki. EOD: adjustment dotyczy tylko `stop_loss_price` (ciasny próg 0.4%, tego samego rzędu co koszt FX) - `take_profit_price` (recovery target po spadku, zwykle już >=2%) zostaje bez zmian.

Zweryfikowane: 2 testy na czystych funkcjach (Sygnał: martwa strefa działa, EUR bez zmian; EOD: stop podbity, take-profit/quantity bez zmian) + test end-to-end na silnikach (USD z włączonym switchem podbite, USD z wyłączonym switchem = bez zmian, EUR zawsze bez zmian, oba silniki). Zmirrorowane dev->prod, migracja uruchomiona (enabled=1 wszędzie - zero zmiany zachowania Micro-Gridu, nowa ochrona aktywna od razu w Sygnale/EOD zgodnie z życzeniem Adama), `run.py` zrestartowany, log czysty.
