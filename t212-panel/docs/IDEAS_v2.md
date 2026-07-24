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
następny dzień" - USUNIĘTE wymuszone zamknięcie (`FORCE_CLOSE_TIME`/
`_force_close_real`), pozycje EOD zostają otwarte i zarządzane WYŁĄCZNIE
przez stop-loss/take-profit, dokładnie jak w Sygnale, bez sztywnej godziny
zamknięcia; (2) okno końca sesji wydłużone z 17:25 na **17:30** ("niech
normalnie próbuje złapać do 17:30").

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

1. **Switch "zarządzaj wszystkim"** ("idę spać, a ty handluj") - globalny
   przełącznik per user (albo per `RiskSettings`), który na czas jego
   włączenia pozwala botowi traktować WSZYSTKIE pozycje na koncie T212 (nie
   tylko te z własnego `ActiveTrade`) jako swoje do zarządzania wyjściem
   (trailing exit / DCA). Wymaga rozstrzygnięcia: co się dzieje przy wyłączeniu
   trybu - bot "oddaje" pozycje z powrotem, czy zostają już na stałe pod jego
   zarządzaniem? Ryzyko: bot nie zna historii/kontekstu ręcznego zakupu
   (dlaczego kupione, jaki cel), więc "zarządzanie" ograniczałoby się
   praktycznie do samego mechanizmu wyjścia (trailing stop), nie do wejścia.

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
