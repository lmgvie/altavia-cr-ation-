import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Chemins
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
TEMPLATES_DIR = BASE_DIR / "templates"
LISTES_DIR = BASE_DIR / "listes"
LOGS_DIR = BASE_DIR / "logs"

# Scopes Gmail requis
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Délais de relance disponibles
DELAIS = {
    "1": {"label": "+1 jour",     "jours": 1},
    "2": {"label": "+3 jours",    "jours": 3},
    "3": {"label": "+1 semaine",  "jours": 7},
    "4": {"label": "+2 semaines", "jours": 14},
    "5": {"label": "+1 mois",     "jours": 30},
    "6": {"label": "Maintenant",  "jours": 0},
}
