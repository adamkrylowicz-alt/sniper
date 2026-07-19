from app import create_app
from app.config import Config

app = create_app()

if __name__ == '__main__':
    if Config.DEBUG:
        # Tylko do lokalnego debugowania (FLASK_DEBUG=true w .env) - reloader
        # i interaktywny debugger Werkzeuga NIGDY nie powinny byc dostepne
        # przez internet (debugger przy nieobsluzonym wyjatku daje zdalne
        # wykonanie kodu, jesli ktos zgadnie/przejmie PIN).
        app.run(host='0.0.0.0', port=5050, debug=True)
    else:
        # Produkcyjnie (w tym dostep publiczny przez nginx-proxy-manager) -
        # waitress zamiast wbudowanego serwera deweloperskiego Flaska/Werkzeug,
        # ktory nie jest przeznaczony pod realny ruch z internetu. Czysty
        # Python, dziala bez problemu na Python 3.8 na NAS-ie.
        from waitress import serve
        serve(app, host='0.0.0.0', port=5050)
