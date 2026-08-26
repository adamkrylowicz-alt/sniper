#!/bin/sh
# Pelny diff app/ miedzy canonical repo / dev / prod - odpalic na STARCIE
# kazdej sesji Snajpera, nie tylko dla plikow ktore akurat sa dotykane.
# Powod: reczne `cp` pojedynczych plikow po kazdej zmianie regularnie
# gubilo pliki (29.07 dev 2 tyg. w tyle, 25.08 x2, 27.08 signal_engine.py
# fix ochrony profitu nigdy nie trafil na prod) - nikt tego zlosliwie nie
# zostawial, po prostu zadna sesja nie sprawdzala CALEGO drzewa.
set -e
REPO=/volume1/docker/code-server/workspace/sniper/t212-panel
DEV=/volume1/docker/t212-panel-dev/t212-panel
PROD=/volume1/docker/t212-panel

echo "=== repo vs dev ==="
diff -rq "$REPO/app" "$DEV/app" --exclude=__pycache__ || true
echo "=== repo vs prod ==="
diff -rq "$REPO/app" "$PROD/app" --exclude=__pycache__ || true
echo "=== CLAUDE.md ==="
diff -q "$REPO/../CLAUDE.md" "$DEV/../CLAUDE.md" || true
diff -q "$REPO/../CLAUDE.md" "$PROD/CLAUDE.md" || true
echo "Gotowe - brak wyjscia powyzej naglowkow = wszystko zsynchronizowane."
