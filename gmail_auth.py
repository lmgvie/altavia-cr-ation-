import os
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from config import SCOPES, CREDENTIALS_FILE, TOKEN_FILE


def get_gmail_service(scopes: list[str] | None = None, token_file: Path | None = None):
    """Authentifie et retourne le service Gmail.

    `scopes` / `token_file` permettent à d'autres outils (ex. l'agent de
    candidatures) d'utiliser des scopes plus larges sans invalider le token
    du script de relance.
    """
    from googleapiclient.discovery import build

    scopes = scopes or SCOPES
    token_file = Path(token_file or TOKEN_FILE)
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print("\n[ERREUR] Fichier credentials.json introuvable.")
                print("→ Suivez le guide SETUP_GOOGLE_API.md pour le configurer.\n")
                raise FileNotFoundError("credentials.json manquant")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), scopes
            )
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)
