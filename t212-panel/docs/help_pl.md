# Snajper — instrukcja obsługi

Panel do tradingu na Trading 212: ręczne zlecenia (Warp Mode/Focus) + trzy automatyczne silniki (Bot, Sygnał, EOD), które same otwierają i pilnują pozycji.

## Logowanie i konto

- **Rejestracja** — `/auth/register`. Po rejestracji dostajesz **kod odzyskiwania** — pokazywany TYLKO RAZ, zapisz go od razu (menedżer haseł albo bezpieczne miejsce offline). Jeśli zgubisz i hasło, i ten kod, nikt (łącznie z adminem appki) nie odzyska Twoich zaszyfrowanych kluczy API.
- **Zapomniane hasło** — `/auth/recover`, potrzebny kod odzyskiwania z rejestracji.
- Klucze API do T212 są szyfrowane Twoim hasłem (zero-knowledge) — nikt poza Tobą nie może ich odczytać, nawet z dostępem do bazy danych.

## Pierwsze kroki (od rejestracji do działającego silnika)

1. **Zarejestruj się i zapisz kod odzyskiwania** (patrz wyżej) — bez tego nie da się odzyskać dostępu po zapomnianym haśle.

2. **Ustawienia → Klucze API → klucz T212** (wymagane do handlu) — wklej klucz i sekret z aplikacji T212 (Ustawienia → API (Beta) w apce T212), osobno dla konta demo i live. Przełącznik **DEMO/LIVE** w górnym pasku pokazuje i przełącza, na którym koncie aktualnie pracujesz — **LIVE = prawdziwe pieniądze**, czerwony i zawsze widoczny.

3. **Ustawienia → Klucze API → dane rynkowe (Finnhub/Alpaca)** — **to Twoje WŁASNE klucze, nie admina appki.** Darmowe konto na [finnhub.io](https://finnhub.io/register) i/albo [alpaca.markets](https://alpaca.markets) wystarczy. Appka działa i bez nich (spada na Yahoo Finance, które nie wymaga żadnego klucza), ale z własnymi kluczami ceny/wykresy są szybsze i pełniejsze, bez dzielenia limitu zapytań z innymi userami.

4. **IBKR (opcjonalnie, zaawansowane)** — to NIE jest zwykły klucz, tylko adres (host:port) Twojej WŁASNEJ, samodzielnie uruchomionej bramki IB Gateway (osobna aplikacja od Interactive Brokers, musi cały czas działać). Bez tego pola appka używa wspólnej bramki jak dotychczas — dotyczy wyłącznie wykresów świecowych na stronie instrumentu, nigdy decyzji tradingowych.

5. **Ustawienia → Watchlist** (opcjonalnie, do trybów ręcznych Warp/Focus) — wyszukaj interesujące Cię spółki i dodaj do ulubionych (max 9 do siatki Warp Mode).

6. **Uruchomienie silnika automatycznego** (Bot / Sygnał / EOD — ten sam schemat dla każdego):
   - Wejdź na stronę silnika (link w górnym pasku).
   - W sekcji **"Aktywa [silnika]"** dodaj przynajmniej jeden ticker + kwotę wejścia — bez tego aktywny silnik nie ma czego kupować.
   - (opcjonalnie) dostosuj **"Ustawienia ryzyka"** — domyślne wartości są bezpieczne do startu.
   - W sekcji **"Aktywacja"** wpisz swoje hasło i kliknij **"Uruchom"** — to WŁĄCZA silnik (na czas działania appki odszyfrowuje Twój klucz T212 w pamięci serwera, żeby mógł samodzielnie składać zlecenia).
   - Silnik zacznie działać od najbliższego cyklu (do ~60 sekund) — sprawdzisz to po statusie "AKTYWNY" w banerze u góry strony i nowych wpisach w **"Dzienniku"** na dole.

## Górny pasek nawigacji

Na telefonie menu jest jednym poziomym, przewijalnym paskiem — przesuń palcem w bok, żeby zobaczyć więcej zakładek. Przełącznik motywu (jasny/ciemny), język (PL/EN) i status połączenia z T212 zostają zawsze widoczne z prawej strony, niezależnie od przewijania.

## Tryby ręczne

### Warp Mode
Siatka 3×3 kafelków z Twoimi ulubionymi instrumentami — szybkie zlecenia KUP/SPRZEDAJ, LIMIT, STOP, STOP-LIMIT jednym kliknięciem. Cena aktualizuje się na żywo. Historia ostatnich zleceń i lista otwartych zleceń oczekujących widoczne z boku.

### Focus
To samo co Warp Mode, ale jeden kafelek na raz, większy — wygodniejsze na telefonie albo gdy chcesz się skupić na jednej pozycji. Przełączanie strzałkami/przyciskami między kafelkami z siatki.

### Aktywa
Pełny przegląd portfela z T212 — wszystkie pozycje (nie tylko botowe), wartość, zysk/strata, którym silnikiem zarządzana dana pozycja. Punkt odniesienia ("vs punkt startowy") można zresetować ręcznie, np. po zresetowaniu konta demo.

### Virtual Pie
Prywatne, wirtualne koszyki — kupujesz kilka spółek naraz z ustalonymi proporcjami (wagami) jednym kliknięciem, bez płacenia osobnej opłaty FX za każde zlecenie z osobna.

## Silniki automatyczne

Wszystkie trzy działają niezależnie, każdy ma **własną listę aktywów** i **własne ustawienia ryzyka**. Aktywacja wymaga podania hasła (żeby appka mogła odszyfrować klucz API) — po restarcie appki uruchamiają się ponownie same, bez pytania o hasło.

### Bot (Micro-Grid)
Pełna pętla: wejście → dokupowanie przy spadkach (DCA) → trailing take-profit i stop-loss, które podążają za ceną w górę i chronią zysk. Umie też przejąć zarządzanie pozycją kupioną ręcznie ("Przekaż botowi" na stronie instrumentu).

### Sygnał
Osobna strategia — wchodzi w pozycję, gdy wskaźnik RSI pokazuje wyprzedanie w trendzie wzrostowym. Jedno wejście na sygnał (bez dokupowania), stop-loss i take-profit liczone od razu przy wejściu.

### EOD (koniec sesji)
Reaguje na nagłe, ostre spadki ceny w ciągu kilku minut — im gwałtowniejszy spadek, tym większa pozycja. Może (opcjonalnie) wymuszać zamknięcie wszystkich pozycji pod koniec dnia handlowego.

**Zwolnij pozycję** — przy każdej pozycji zarządzanej przez silnik jest przycisk "Zwolnij"/"Cofnij" — udziały zostają na koncie, tylko dany silnik przestaje ich pilnować (np. gdy chcesz sam ręcznie sprzedać).

## Bezpieczeństwo

- Konto **demo** to wirtualne pieniądze, bezpieczne do testów.
- Konto **live** to prawdziwe pieniądze — czerwony wskaźnik LIVE w pasku jest zawsze widoczny, żeby nie było wątpliwości na jakim koncie działasz.
- Weekendowe zawieszenie stop-lossów (jeśli włączone) oznacza, że pozycje są bez ochrony od piątku 21:00 do poniedziałku 11:00 — świadomy wybór, nie błąd.
- Powiadomienia o błędach/ważnych zdarzeniach przychodzą na Telegram.

## Język i motyw

Przełącznik **PL/EN** w górnym pasku (obok ikony motywu) — działa nawet przed zalogowaniem. Wybór zapamiętywany jest w przeglądarce, a dla zalogowanego użytkownika też na koncie.
