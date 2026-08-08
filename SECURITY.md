# Bezpieczeństwo — co jest w tym repo, a czego na pewno nie ma

Krótki przewodnik dla kogoś, kto dostaje dostęp do tego kodu (collaborator na
GitHubie, audyt, code review) i pyta "czy to bezpieczne".

## 1. Zero sekretów w repo, sprawdzone w całej historii

- `.env` (klucze SMTP, `SECRET_KEY` Flaska) i `instance/` (baza SQLite,
  `bot_autostart_keys.json`) są w `.gitignore` od zawsze w tym repo.
- To nie jest tylko deklaracja — **cała historia gita (238 commitów)
  przeskanowana narzędziem [gitleaks](https://github.com/gitleaks/gitleaks),
  zero wykrytych sekretów.** `.gitignore` chroni tylko przyszłe commity;
  sam skan historii jest jedynym sposobem żeby być pewnym, że nic nie
  wyciekło zanim `.gitignore` powstał.
- Rekomendacja dla każdego kto tu coś zmienia: uruchom `gitleaks git
  --log-opts="--all"` przed publicznym udostępnieniem repo, nie ufaj samemu
  brakowi `.env` w bieżącym katalogu roboczym.

## 2. Hasła userów — nigdy plaintext

`werkzeug.security.generate_password_hash`/`check_password_hash`
(`app/routes/auth.py`) — standardowe, solone hashowanie. Baza nigdy nie
przechowuje hasła w żadnej odwracalnej formie.

## 3. Klucze API T212 — zero-knowledge, nie samo hashowanie hasła

To jest część, która realnie chroni pieniądze użytkownika, więc warto ją
zrozumieć dokładnie (`app/cipher.py`, `app/routes/auth.py`):

- Przy rejestracji generowany jest losowy **master_key** (klucz szyfrujący,
  nigdy nie trafia do bazy w postaci jawnej).
- Master_key jest **zawijany (wrapped)** hasłem usera → `wrapped_master_key_
  by_password` w bazie. Osobno, ten sam master_key jest **dodatkowo** zawijany
  jednorazowym **kodem odzyskiwania** (recovery code, pokazany userowi RAZ
  przy rejestracji) → `wrapped_master_key_by_recovery`. Każdy z dwóch ma
  własną sól (`salt_password`/`salt_recovery`).
- Przy logowaniu: hasło odblokowuje `wrapped_master_key_by_password` →
  odzyskany master_key trzyma się w pamięci procesu/sesji, **nigdy w bazie**.
  Tym master_key szyfrowane/odszyfrowywane są prawdziwe klucze API T212.
- **Konsekwencja praktyczna:** ktoś z dostępem do samej bazy danych (backup,
  wyciek pliku `sniper.db`, dostęp admina do serwera) **nie ma jak
  odszyfrować kluczy API T212** bez hasła usera ALBO jego kodu odzyskiwania.
  Nie ma "backdoora" ani klucza głównego, którym admin mógłby to obejść —
  to świadomy wybór architektury (dokumentowany wprost w kodzie jako
  "Zero-Knowledge key wrapping").
- Jeśli user zgubi **oba** — hasło i kod odzyskiwania — jego zaszyfrowane
  klucze API są nieodzyskiwalne. To celowy kompromis (bezpieczeństwo >
  wygoda), opisany userowi wprost w UI przy rejestracji.

## 4. Warstwy dostępu do samej appki

- Cała appka (poza publicznym `/auth/register`) wymaga zalogowania.
- Rejestracja jest dwustopniowo bramkowana: link aktywacyjny na email
  (`email_verified`) + ręczne zatwierdzenie przez admina w panelu
  (`is_active`) — nowe konto nie może się zalogować, dopóki obie bramki nie
  przejdą.
- Dodatkowa warstwa przed samą appką: HTTP Basic Auth na poziomie reverse
  proxy (Nginx Proxy Manager), więc nawet ekran logowania appki nie jest
  publicznie widoczny bez dodatkowego hasła.
- HTTPS wymuszony (Let's Encrypt, auto-odnawiane certyfikaty) na obu
  publicznych subdomenach (prod/dev).

## 5. Co NIE jest tu twierdzone

To nie jest formalny audyt bezpieczeństwa ani pentest — to podsumowanie
architektury dla kogoś oceniającego czy warto dać dostęp do kodu. Rzeczy
poza zakresem tego dokumentu: bezpieczeństwo samego NAS-a/hosta, zależności
pip/npm (brak automatycznego skanowania CVE), rate-limiting na endpointach
poza rejestracją.
