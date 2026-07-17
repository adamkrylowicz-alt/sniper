# upload_sniper.ps1
# ===================
# Wysyla cala zawartosc app/ (backend + templates + static) z TEGO projektu
# na NAS przez scp - BEZ kopiowania czegokolwiek do osobnego folderu.
#
# UZYCIE:
# 1. Otworz PowerShell w folderze t212-panel (tam gdzie lezy ten plik).
# 2. Uruchom: .\upload_sniper.ps1
# 3. Jesli Windows zapyta o polityke wykonywania skryptow, wpisz najpierw
#    (jednorazowo): Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = "Continue"

# --- KONFIGURACJA ---
# Klucz SSH lezy w projekcie, w folderze "pliki" obok "t212-panel" (siostrzany
# folder) - patrz sniper\pliki\sshkey.txt.
$key      = Join-Path (Split-Path $PSScriptRoot -Parent) "pliki\sshkey.txt"
$destHost = "adamnas@62.250.26.111"
$destRoot = "/volume1/docker/t212-panel/app"
$port     = "8888"
$localRoot = Join-Path $PSScriptRoot "app"

# USTAW NA $true TYLKO gdy chcesz WYCZYSCIC baze na NAS-ie (utrata wszystkich
# kont/watchlist/kluczy API) - normalnie NIE trzeba tego robic, bo Flask sam
# tworzy BRAKUJACE tabele przy starcie (db.create_all() w app/__init__.py).
# Domyslnie $false = baza NIE jest ruszana.
$wipeDatabase = $false

Write-Host "=== Tworzenie brakujacych folderow na NAS-ie ===" -ForegroundColor Cyan
ssh -p $port -i $key $destHost "mkdir -p $destRoot/routes $destRoot/services $destRoot/templates/auth $destRoot/static/css $destRoot/static/js"

# Sciezki WZGLEDEM app/ - te same lokalnie i zdalnie, wiec zadnego mapowania
# flat->nested nie trzeba juz robic (w odroznieniu od starszych wersji tego
# skryptu, ktore wymagaly wczesniejszego skopiowania plikow do plaskiego
# folderu na Desktopie).
$relFiles = @(
    "__init__.py"
    "cipher.py"
    "config.py"
    "extensions.py"
    "models.py"
    "utils.py"
    "routes\api_keys.py"
    "routes\auth.py"
    "routes\scalping.py"
    "routes\settings.py"
    "routes\report.py"
    "routes\pie.py"
    "routes\bot.py"
    "services\instrument_cache.py"
    "services\logo_cache.py"
    "services\risk_guard.py"
    "services\session_store.py"
    "services\t212_client.py"
    "services\mailer.py"
    "services\price_feed.py"
    "services\bot_credentials.py"
    "services\bot_engine.py"
    "services\finnhub_client.py"
    "static\css\style.css"
    "static\js\common.js"
    "static\js\warp.js"
    "static\js\watchlist.js"
    "static\js\pie.js"
    "static\js\bot.js"
    "static\js\focus.js"
    "templates\focus.html"
    "templates\api_keys.html"
    "templates\auth\login.html"
    "templates\auth\recover.html"
    "templates\auth\recovery_code.html"
    "templates\auth\register.html"
    "templates\base.html"
    "templates\history.html"
    "templates\settings_index.html"
    "templates\warp.html"
    "templates\watchlist.html"
    "templates\pie_list.html"
    "templates\pie_detail.html"
    "templates\bot.html"
)

Write-Host ""
Write-Host "=== Wysylanie $($relFiles.Count) plikow ===" -ForegroundColor Cyan

$ok = 0
$failed = @()

foreach ($rel in $relFiles) {
    $local = Join-Path $localRoot $rel
    $remote = $rel -replace '\\', '/'

    if (-not (Test-Path $local)) {
        Write-Host "POMINIETO (brak lokalnie): $rel" -ForegroundColor Yellow
        $failed += $rel
        continue
    }

    Write-Host "-> $rel  ...  " -NoNewline
    scp -O -P $port -i $key $local "${destHost}:${destRoot}/${remote}" 2>&1 | Out-Null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK" -ForegroundColor Green
        $ok++
    } else {
        Write-Host "BLAD (kod $LASTEXITCODE)" -ForegroundColor Red
        $failed += $rel
    }
}

Write-Host ""
Write-Host "=== Podsumowanie ===" -ForegroundColor Cyan
Write-Host "Wyslano poprawnie: $ok / $($relFiles.Count)"

if ($failed.Count -gt 0) {
    Write-Host "Nieudane lub pominiete pliki:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
} else {
    Write-Host "Wszystkie pliki wyslane poprawnie." -ForegroundColor Green
}

Write-Host ""
if ($wipeDatabase) {
    Write-Host "=== Usuwanie starej bazy danych (WYMUSZONE - wipeDatabase=true) ===" -ForegroundColor Cyan
    ssh -p $port -i $key $destHost "rm -f /volume1/docker/t212-panel/instance/sniper.db"
    Write-Host "Baza usunieta - zostanie utworzona na nowo przy starcie appki." -ForegroundColor Green
    Write-Host "UWAGA: wszystkie konta/watchlisty/klucze API trzeba bedzie dodac od nowa." -ForegroundColor Yellow
} else {
    Write-Host "Baza danych NIETKNIETA (wipeDatabase=false) - konta i ustawienia zostaja." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Wysylanie skryptow migracji (jednorazowe, idempotentne) ===" -ForegroundColor Cyan
$migScripts = @("migrate_add_pie_id.py", "migrate_add_is_leveraged.py", "migrate_add_bot_entry_amount.py", "migrate_add_bot_assets.py", "migrate_add_focus_tiles.py", "migrate_add_active_trade_is_paper.py")
foreach ($mig in $migScripts) {
    $migLocal = Join-Path $PSScriptRoot $mig
    if (Test-Path $migLocal) {
        Write-Host "-> $mig  ...  " -NoNewline
        scp -O -P $port -i $key $migLocal "${destHost}:/volume1/docker/t212-panel/$mig" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "OK" -ForegroundColor Green } else { Write-Host "BLAD (kod $LASTEXITCODE)" -ForegroundColor Red }
    } else {
        Write-Host "POMINIETO - brak lokalnie $mig" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== GOTOWE ===" -ForegroundColor Cyan
Write-Host "Zaloguj sie teraz przez SSH i uruchom appke recznie (zebys widzial log na zywo):" -ForegroundColor Yellow
Write-Host "  ssh -p $port -i `"$key`" $destHost"
Write-Host "  cd /volume1/docker/t212-panel"
Write-Host ""
Write-Host "Skrypty migracji SA BEZPIECZNE do uruchomienia wielokrotnie (sprawdzaja same," -ForegroundColor Yellow
Write-Host "czy jest co robic) - jesli nie jestes pewien co juz odpalales, po prostu odpal" -ForegroundColor Yellow
Write-Host "oba ponizej jeszcze raz, nic nie popsuja:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. Klucze API w .env na NAS-ie (jesli jeszcze nie dodane):" -ForegroundColor Yellow
Write-Host "     nano .env" -ForegroundColor Yellow
Write-Host "     # FINNHUB_API_KEY=...   (mini-wykresy w Smart Virtual Pie)" -ForegroundColor Yellow
Write-Host "     # LOGO_DEV_API_KEY=...  (logotypy spolek - Clearbit jest martwe)" -ForegroundColor Yellow
Write-Host ""
Write-Host "  2. Odpal appke RAZ i od razu przerwij (Ctrl+C) - to tylko po to," -ForegroundColor Yellow
Write-Host "     zeby db.create_all() utworzylo ewentualne nowe tabele:" -ForegroundColor Yellow
Write-Host "     python3 run.py" -ForegroundColor Yellow
Write-Host "     # poczekaj na log startowy, potem Ctrl+C" -ForegroundColor Yellow
Write-Host ""
Write-Host "  3. Uruchom skrypty migracji:" -ForegroundColor Yellow
Write-Host "     python3 migrate_add_pie_id.py" -ForegroundColor Yellow
Write-Host "     python3 migrate_add_is_leveraged.py" -ForegroundColor Yellow
Write-Host "     python3 migrate_add_bot_entry_amount.py" -ForegroundColor Yellow
Write-Host "     python3 migrate_add_bot_assets.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "  4. Jesli APScheduler jeszcze nie zainstalowany (Micro-Grid Bot):" -ForegroundColor Yellow
Write-Host "     python3 -m pip install --user APScheduler==3.10.4" -ForegroundColor Yellow
Write-Host ""
Write-Host "  5. Dopiero teraz uruchom appke normalnie:" -ForegroundColor Yellow
Write-Host "     python3 run.py"
