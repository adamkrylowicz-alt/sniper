# T212-Panel: Projekt "Warp Mode"

## Cel projektu
Stworzenie bezpiecznego, responsywnego panelu do scalpingu (Grid 3x3) dla Trading 212.

## Główne założenia (The Rules)
1. **Bezpieczeństwo przede wszystkim:** Brak scrapowania brokera. Oficjalne API do zleceń.
2. **Hybrid Data Layer:** - Execution (Kupno/Sprzedaż) -> Oficjalne API T212.
   - Visualization (Ceny/Wykresy) -> Publiczne API (np. Finnhub/Polygon) dla wizualizacji "na żywo".
3. **Architektura:** Flask (Backend) + WebSocket (Frontend).
4. **Wizualizacja:** Lightweight Charts w układzie 3x3.

## Struktura (Status: Zbudowana)
- `app/`: Serce aplikacji (Flask App Factory)
  - `services/`: Logika (Client T212, Risk Guard, Price Feed)
  - `routes/`: Endpointy (Auth, Scalping, Settings)
- `docs/`: Dokumentacja projektu