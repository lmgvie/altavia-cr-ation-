#!/usr/bin/env python3
"""
Système de relance automatique de mails via Gmail.
Usage : python relance.py
"""

import csv
import json
import time
import base64
import logging
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import schedule

from config import DELAIS, TEMPLATES_DIR, LISTES_DIR, LOGS_DIR
from gmail_auth import get_gmail_service

# ── Logging ──────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / f"relance_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def charger_liste(chemin_csv: Path) -> list[dict]:
    """Charge une liste de contacts depuis un fichier CSV."""
    contacts = []
    with open(chemin_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            contacts.append(row)
    return contacts


def charger_template(nom_template: str) -> dict:
    """Charge un template JSON depuis le dossier templates/."""
    chemin = TEMPLATES_DIR / f"{nom_template}.json"
    if not chemin.exists():
        raise FileNotFoundError(f"Template introuvable : {chemin}")
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def personnaliser(texte: str, contact: dict) -> str:
    """Remplace les variables {clé} par les valeurs du contact."""
    for cle, valeur in contact.items():
        texte = texte.replace(f"{{{cle}}}", valeur)
    return texte


def construire_message(expediteur: str, destinataire: str, sujet: str, corps: str):
    """Construit un message MIME encodé en base64 pour l'API Gmail."""
    msg = MIMEText(corps, "plain", "utf-8")
    msg["to"] = destinataire
    msg["from"] = expediteur
    msg["subject"] = sujet
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def envoyer_mail(service, expediteur: str, contact: dict, template: dict) -> bool:
    """Envoie un mail de relance à un contact."""
    try:
        sujet = personnaliser(template["sujet"], contact)
        corps = personnaliser(template["corps"], contact)
        message = construire_message(expediteur, contact["email"], sujet, corps)
        service.users().messages().send(userId="me", body=message).execute()
        log.info(f"✓ Mail envoyé → {contact['email']} ({contact.get('nom', '')} {contact.get('prenom', '')})")
        return True
    except Exception as e:
        log.error(f"✗ Échec envoi → {contact['email']} : {e}")
        return False


def lancer_campagne(service, expediteur: str, contacts: list[dict], template: dict):
    """Envoie la relance à tous les contacts de la liste."""
    total = len(contacts)
    succes = 0
    echecs = []

    print(f"\n{'─'*50}")
    print(f"  Envoi en cours : {total} destinataire(s)")
    print(f"{'─'*50}")

    for i, contact in enumerate(contacts, 1):
        print(f"  [{i}/{total}] {contact.get('email', '?')}...", end=" ", flush=True)
        ok = envoyer_mail(service, expediteur, contact, template)
        if ok:
            succes += 1
            print("✓")
        else:
            echecs.append(contact.get("email", "?"))
            print("✗")
        # Pause anti-spam Gmail (max ~500 mails/jour en compte standard)
        time.sleep(0.5)

    print(f"\n{'─'*50}")
    print(f"  Résultat : {succes}/{total} mails envoyés")
    if echecs:
        print(f"  Échecs   : {', '.join(echecs)}")
    print(f"{'─'*50}\n")


# ── Interface CLI ─────────────────────────────────────────────────────────────

def choisir_liste() -> Path:
    fichiers = sorted(LISTES_DIR.glob("*.csv"))
    if not fichiers:
        print("\n[ERREUR] Aucun fichier CSV dans le dossier listes/")
        print("→ Créez un fichier CSV avec les colonnes : nom, prenom, email, entreprise, template\n")
        raise SystemExit(1)

    print("\n╔══ LISTES DISPONIBLES ══════════════════════════╗")
    for i, f in enumerate(fichiers, 1):
        nb = sum(1 for _ in open(f, encoding="utf-8")) - 1
        print(f"  {i}. {f.name} ({nb} contact(s))")
    print("╚════════════════════════════════════════════════╝")

    while True:
        choix = input("\nChoisissez une liste (numéro) : ").strip()
        if choix.isdigit() and 1 <= int(choix) <= len(fichiers):
            return fichiers[int(choix) - 1]
        print("  → Entrée invalide, réessayez.")


def choisir_template() -> str:
    fichiers = sorted(TEMPLATES_DIR.glob("*.json"))
    if not fichiers:
        print("\n[ERREUR] Aucun template dans le dossier templates/")
        raise SystemExit(1)

    print("\n╔══ TEMPLATES DISPONIBLES ═══════════════════════╗")
    for i, f in enumerate(fichiers, 1):
        print(f"  {i}. {f.stem}")
    print("╚════════════════════════════════════════════════╝")

    while True:
        choix = input("\nChoisissez un template (numéro) : ").strip()
        if choix.isdigit() and 1 <= int(choix) <= len(fichiers):
            return fichiers[int(choix) - 1].stem
        print("  → Entrée invalide, réessayez.")


def choisir_delai() -> int:
    """Retourne le nombre de jours avant l'envoi."""
    print("\n╔══ MOMENT DE RELANCE ═══════════════════════════╗")
    for k, v in DELAIS.items():
        print(f"  {k}. {v['label']}")
    print("╚════════════════════════════════════════════════╝")

    while True:
        choix = input("\nChoisissez le délai de relance : ").strip()
        if choix in DELAIS:
            return DELAIS[choix]["jours"]
        print("  → Entrée invalide, réessayez.")


def confirmer(contacts: list[dict], template_nom: str, delai_jours: int) -> bool:
    heure_envoi = datetime.now() + timedelta(days=delai_jours)
    print(f"\n╔══ RÉCAPITULATIF ════════════════════════════════╗")
    print(f"  Destinataires : {len(contacts)} contact(s)")
    print(f"  Template      : {template_nom}")
    print(f"  Envoi prévu   : {heure_envoi.strftime('%d/%m/%Y à %H:%M')}")
    print(f"╚════════════════════════════════════════════════╝")
    rep = input("\nConfirmer l'envoi ? (oui/non) : ").strip().lower()
    return rep in ("oui", "o", "yes", "y")


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "═"*52)
    print("   SYSTÈME DE RELANCE AUTOMATIQUE — ALTAVIA")
    print("═"*52)

    # 1. Authentification Gmail
    print("\n→ Connexion à Gmail...")
    service = get_gmail_service()
    profil = service.users().getProfile(userId="me").execute()
    expediteur = profil["emailAddress"]
    print(f"  Connecté en tant que : {expediteur}")

    # 2. Sélection de la liste
    chemin_liste = choisir_liste()
    contacts = charger_liste(chemin_liste)

    # 3. Sélection du template
    # Si tous les contacts ont le même template défini dans le CSV, on l'utilise
    templates_csv = set(c.get("template", "").strip() for c in contacts if c.get("template"))
    if len(templates_csv) == 1:
        template_nom = templates_csv.pop()
        print(f"\n→ Template détecté depuis la liste : {template_nom}")
        try:
            template = charger_template(template_nom)
        except FileNotFoundError:
            print(f"  [AVERTISSEMENT] Template '{template_nom}' introuvable, sélection manuelle.")
            template_nom = choisir_template()
            template = charger_template(template_nom)
    else:
        template_nom = choisir_template()
        template = charger_template(template_nom)

    # 4. Délai de relance
    delai_jours = choisir_delai()

    # 5. Confirmation
    if not confirmer(contacts, template_nom, delai_jours):
        print("\n  Opération annulée.\n")
        return

    # 6. Envoi immédiat ou programmé
    if delai_jours == 0:
        lancer_campagne(service, expediteur, contacts, template)
    else:
        heure_envoi = datetime.now() + timedelta(days=delai_jours)
        print(f"\n  Relance programmée pour le {heure_envoi.strftime('%d/%m/%Y à %H:%M')}")
        print("  Le script doit rester actif jusqu'à l'envoi. Ctrl+C pour annuler.\n")

        def job():
            lancer_campagne(service, expediteur, contacts, template)
            return schedule.CancelJob

        schedule.every(delai_jours).days.do(job)

        while schedule.jobs:
            schedule.run_pending()
            time.sleep(30)

    print("  Campagne terminée. Consultez les logs/ pour le détail.\n")


if __name__ == "__main__":
    main()
