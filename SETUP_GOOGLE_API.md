# Configuration de l'API Google Gmail

## 1. Créer un projet Google Cloud

1. Rendez-vous sur https://console.cloud.google.com/
2. Cliquez sur **"Nouveau projet"** → donnez-lui un nom (ex: `Relance-Altavia`)
3. Sélectionnez le projet créé

## 2. Activer l'API Gmail

1. Menu gauche → **"API et services"** → **"Bibliothèque"**
2. Recherchez **"Gmail API"** → cliquez dessus → **"Activer"**

## 3. Créer les identifiants OAuth 2.0

1. Menu gauche → **"API et services"** → **"Identifiants"**
2. Cliquez sur **"+ Créer des identifiants"** → **"ID client OAuth"**
3. Type d'application : **"Application de bureau"**
4. Nom : `relance-client`
5. Cliquez sur **"Créer"**
6. Téléchargez le fichier JSON → **renommez-le `credentials.json`**
7. Placez `credentials.json` à la racine du projet (même dossier que `relance.py`)

## 4. Configurer l'écran de consentement (si demandé)

1. Menu gauche → **"Écran de consentement OAuth"**
2. Type : **Externe** → **Créer**
3. Remplissez le nom de l'application et votre email
4. Ajoutez votre adresse Gmail dans **"Utilisateurs test"**
5. Sauvegardez

## 5. Installer les dépendances Python

```bash
pip install -r requirements.txt
```

## 6. Lancer le système de relance

```bash
python relance.py
```

À la première exécution, une fenêtre de navigateur s'ouvrira pour autoriser l'accès Gmail.
Le token sera sauvegardé dans `token.json` pour les prochaines utilisations (pas besoin de se re-connecter).

---

## Structure du projet

```
altavia-cr-ation-/
├── relance.py              ← Script principal (lancer ici)
├── gmail_auth.py           ← Gestion OAuth Gmail
├── config.py               ← Configuration & délais
├── requirements.txt        ← Dépendances Python
├── credentials.json        ← Vos identifiants Google (à créer)
├── token.json              ← Token auto-généré (ne pas supprimer)
│
├── templates/              ← Templates de mails (JSON)
│   ├── prospection.json
│   ├── suivi_devis.json
│   └── equipe_interne.json
│
├── listes/                 ← Listes de contacts (CSV)
│   └── exemple_prospects.csv
│
└── logs/                   ← Logs d'envoi automatiques
```

## Format du fichier CSV (liste de contacts)

```csv
nom,prenom,email,entreprise,template
Dupont,Jean,jean.dupont@exemple.com,Société Alpha,prospection
Martin,Sophie,sophie.martin@exemple.com,Beta Corp,suivi_devis
```

**Colonnes obligatoires :** `nom`, `prenom`, `email`
**Colonnes optionnelles :** `entreprise`, `template` (si absent → sélection manuelle)

## Format d'un template (JSON)

```json
{
  "sujet": "Relance — {entreprise}",
  "corps": "Bonjour {prenom},\n\nVotre message ici...\n\nCordialement,\n[VOTRE NOM]"
}
```

Les variables `{nom}`, `{prenom}`, `{email}`, `{entreprise}` sont remplacées automatiquement par les données du CSV.

## Ajouter un nouveau template

Créez simplement un fichier `.json` dans `templates/` avec la structure ci-dessus.
Il apparaîtra automatiquement dans le menu de sélection.

## Ajouter une liste de contacts

Créez un fichier `.csv` dans `listes/` avec les colonnes requises.
Il apparaîtra automatiquement dans le menu de sélection.
