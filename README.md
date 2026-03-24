# Slack Bot Claude — mode "source only"

Bot Slack qui répond **uniquement sur la base de l'export Slack** chargé au démarrage.
Zéro hallucination : si la réponse n'est pas dans les messages, il le dit explicitement.

## Architecture

```
Export Slack (dossier dézippé)
        ↓ chargé au démarrage
    app.py (mémoire)
        ↓ à chaque @mention
  sélection des channels pertinents
        ↓
  injection dans le prompt Claude
        ↓
  réponse citant les sources
```

## Prérequis

- Python 3.9+
- Un export Slack dézippé (dossier avec sous-dossiers par channel + users.json)
- Clé API Anthropic
- Une Slack App configurée (voir ci-dessous)

---

## 1. Préparer l'export Slack

Dézippé, le dossier doit ressembler à :

```
slack_export/
├── users.json
├── channels.json
├── general/
│   ├── 2025-01-10.json
│   └── ...
├── best-practices/
│   └── ...
└── ...
```

Place ce dossier à la racine du projet (ou configure `SLACK_EXPORT_DIR` dans `.env`).

---

## 2. Créer la Slack App

1. Va sur https://api.slack.com/apps → **"Create New App"** → **"From scratch"**
2. Nom : ex. `TML Assistant`

### Permissions (OAuth & Permissions → Bot Token Scopes)
Ajoute :
- `app_mentions:read`
- `chat:write`
- `channels:history`
- `groups:history` *(si channels privés)*

### Socket Mode
- Va dans **"Socket Mode"** → active
- Génère un **App-Level Token** avec le scope `connections:write`
- Ce token commence par `xapp-`

### Event Subscriptions
- Active les events
- **Subscribe to bot events** → ajoute `app_mention`

### Installer l'app
- **"Install to Workspace"** → copie le **Bot Token** (`xoxb-`)
- Dans Slack, invite le bot dans un channel : `/invite @TML-Assistant`

---

## 3. Variables d'environnement

Copie `.env.example` en `.env` :

```bash
cp .env.example .env
```

Remplis les 4 valeurs :
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ANTHROPIC_API_KEY=sk-ant-...
SLACK_EXPORT_DIR=./slack_export
```

---

## 4. Lancer en local

```bash
pip install -r requirements.txt
python app.py
```

Au démarrage tu verras :
```
⏳ Chargement de l'export Slack...
✅ 18 channels chargés — 4683 messages au total
⚡ Bot démarré — répond uniquement sur les données Slack chargées
```

---

## 5. Déployer sur Railway

1. Push ce projet sur GitHub (avec le dossier `slack_export/` inclus)
2. Va sur https://railway.app → **"New Project"** → **"Deploy from GitHub"**
3. Ajoute les 4 variables d'environnement dans Railway → Variables
4. Railway détecte le `Procfile` et lance automatiquement

> ⚠️ Si l'export est volumineux (>100MB), héberge-le sur un volume Railway ou un bucket S3
> et monte-le via `SLACK_EXPORT_DIR`.

---

## Utilisation

Dans n'importe quel channel où le bot est invité :

```
@TML-Assistant quels outils de marketing automation ont été recommandés ?
@TML-Assistant qui cherche un CMO freelance ?
@TML-Assistant résume les best practices partagées sur le copywriting LinkedIn
```

Le bot répond en thread et indique les channels sources utilisés.

---

## Personnalisation

Dans `app.py`, modifie :
- `SYSTEM_PROMPT` — pour changer le ton ou les règles de réponse
- `CHANNEL_KEYWORDS` — pour améliorer la sélection automatique des channels
- `MAX_CONTEXT_TOKENS` — pour ajuster la limite (défaut : 150k)
