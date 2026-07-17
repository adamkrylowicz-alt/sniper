# Używamy lekkiego obrazu Pythona
FROM python:3.11-slim

# Ustawiamy folder roboczy w kontenerze
WORKDIR /app

# Kopiujemy wymagania i instalujemy je
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiujemy całą resztę kodu
COPY . .

# Otwieramy port 5000
EXPOSE 5000

# Komenda startowa
CMD ["python", "run.py"]