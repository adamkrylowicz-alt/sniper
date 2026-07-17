# upload_sniper.ps1
# ===================
# Automatyzuje wyslanie wszystkich plikow projektu Sniper na NAS przez scp.
#
# UZYCIE:
# 1. Umiesc ten skrypt w TYM SAMYM folderze co pobrane pliki (.py/.html/.js/.css)
# 2. Otworz PowerShell, zrob cd do tego folderu, wpisz: .\upload_sniper.ps1
# 3. Jesli Windows zapyta o polityke wykonywania skryptow, wpisz najpierw
#    (jednorazowo): Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = "Continue"

# --- KONFIGURACJA - dostosuj tylko te sciezke, jesli klucz lezy gdzie indziej ---
$key  = "C:\Users\adamk\Desktop\_claudiusz\15 july\sshkey.txt"
$dest = "adamnas@62.250.26.111:/volume1/docker/t212-panel/app"
$port = "8888"

# USTAW NA $true TYLKO gdy wiesz, ze zmienil sie models.py (nowe/zmienione
# tabele) - inaczej stracisz wszystkie konta i dane bez potrzeby (jak juz
# sie kilka razy zdarzylo). Domyslnie $false = baza NIE jest ruszana.
$wipeDatabase = $false

Write-Host "=== Tworzenie brakujacych folderow na NAS-ie ===" -ForegroundColor Cyan
ssh -p $port -i $key adamnas@62.250.26.111 "mkdir -p /volume1/docker/t212-panel/app/routes /volume1/docker/t212-panel/app/services /volume1/docker/t212-panel/app/templates/auth /volume1/docker/t212-panel/app/static/css /volume1/docker/t212-panel/app/static/js"

# Mapa: plik lokalny -> sciezka docelowa (wzgledem $dest, czyli .../app/)
$files = @{
    "__init__.py"          = "__init__.py"
    "cipher.py"            = "cipher.py"
    "config.py"            = "config.py"
    "extensions.py"        = "extensions.py"
    "models.py"            = "models.py"
    "utils.py"             = "utils.py"
    "api_keys.py"          = "routes/api_keys.py"
    "auth.py"              = "routes/auth.py"
    "scalping.py"          = "routes/scalping.py"
    "settings.py"          = "routes/settings.py"
    "report.py"            = "routes/report.py"
    "instrument_cache.py"  = "services/instrument_cache.py"
    "logo_cache.py"        = "services/logo_cache.py"
    "risk_guard.py"        = "services/risk_guard.py"
    "session_store.py"     = "services/session_store.py"
    "t212_client.py"       = "services/t212_client.py"
    "mailer.py"            = "services/mailer.py"
    "style.css"            = "static/css/style.css"
    "common.js"            = "static/js/common.js"
    "warp.js"              = "static/js/warp.js"
    "watchlist.js"         = "static/js/watchlist.js"
    "api_keys.html"        = "templates/api_keys.html"
    "login.html"           = "templates/auth/login.html"
    "recover.html"         = "templates/auth/recover.html"
    "recovery_code.html"   = "templates/auth/recovery_code.html"
    "register.html"        = "templates/auth/register.html"
    "base.html"            = "templates/base.html"
    "history.html"         = "templates/history.html"
    "settings_index.html"  = "templates/settings_index.html"
    "warp.html"            = "templates/warp.html"
    "watchlist.html"       = "templates/watchlist.html"
}

Write-Host ""
Write-Host "=== Wysylanie $($files.Count) plikow ===" -ForegroundColor Cyan

$ok = 0
$failed = @()

foreach ($local in $files.Keys) {
    $remote = $files[$local]

    if (-not (Test-Path $local)) {
        Write-Host "POMINIETO (brak lokalnie): $local" -ForegroundColor Yellow
        $failed += $local
        continue
    }

    Write-Host "-> $local  ...  " -NoNewline
    scp -O -P $port -i $key $local "${dest}/${remote}" 2>&1 | Out-Null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK" -ForegroundColor Green
        $ok++
    } else {
        Write-Host "BLAD (kod $LASTEXITCODE)" -ForegroundColor Red
        $failed += $local
    }
}

Write-Host ""
Write-Host "=== Podsumowanie ===" -ForegroundColor Cyan
Write-Host "Wyslano poprawnie: $ok / $($files.Count)"

if ($failed.Count -gt 0) {
    Write-Host "Nieudane lub pominiete pliki:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
} else {
    Write-Host "Wszystkie pliki wyslane poprawnie." -ForegroundColor Green
}

Write-Host ""
if ($wipeDatabase) {
    Write-Host "=== Usuwanie starej bazy danych (WYMUSZONE - wipeDatabase=true) ===" -ForegroundColor Cyan
    ssh -p $port -i $key adamnas@62.250.26.111 "rm -f /volume1/docker/t212-panel/instance/sniper.db"
    Write-Host "Baza usunieta - zostanie utworzona na nowo przy starcie appki." -ForegroundColor Green
    Write-Host "UWAGA: wszystkie konta/watchlisty/klucze API trzeba bedzie dodac od nowa." -ForegroundColor Yellow
} else {
    Write-Host "Baza danych NIETKNIETA (wipeDatabase=false) - konta i ustawienia zostaja." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== GOTOWE ===" -ForegroundColor Cyan
Write-Host "Zaloguj sie teraz przez SSH i uruchom appke recznie (zebys widzial log na zywo):" -ForegroundColor Yellow
Write-Host "  ssh -p $port -i `"$key`" adamnas@62.250.26.111"
Write-Host "  cd /volume1/docker/t212-panel"
Write-Host "  python3 run.py"
