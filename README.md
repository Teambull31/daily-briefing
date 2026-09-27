# Jarvis — ton assistant perso, piloté depuis ton téléphone

Tu écris (ou dictes) une demande sur Telegram depuis ton téléphone → ton **PC** la reçoit →
Jarvis répond, ou lance un **agent de code** qui travaille sur ton PC aussi longtemps
qu'il faut → tu reçois une notification avec le résultat.

**100 % gratuit** en mode local : l'IA tourne sur ta carte graphique via [Ollama](https://ollama.com),
Telegram est gratuit, la météo (Open-Meteo) et les actus (RSS) aussi.

```
 Téléphone (Telegram) ──► Bot Jarvis (sur ton PC) ──► Ollama (LLM local, GPU)
         ▲                     │
         │                     └──► Agent au choix : OpenCode+Ollama (gratuit) ou Claude Code (abonnement)
         └──── notification ◄──────── travaille dans ~/jarvis-workspace/<projet>
```

Pas besoin d'ouvrir de port sur ta box : le bot va chercher les messages chez Telegram.

## Ce que ça fait

| Tu envoies | Jarvis fait |
|---|---|
| « C'est quoi la différence entre TCP et UDP ? » | Répond directement (LLM local) |
| « Crée-moi un script qui renomme mes photos par date » | Lance une tâche, code, teste, te prévient à la fin |
| 🎙 un message vocal | Le transcrit en local puis le traite pareil |
| `/briefing` (ou automatiquement chaque matin) | Météo + résumé des actus |
| `/projet site-perso` puis des demandes | Travaille dans ce dossier, garde l'historique git |
| `/taches`, `/log 3`, `/stop 3`, `/get index.html` | Suivre, arrêter, récupérer un fichier |
| `/claude <tâche>` ou « Claude, … » | Utilise Claude Code (ton abonnement) au lieu du modèle gratuit |

Jarvis choisit seul entre « répondre » et « agir » (`AUTO_ROUTE=1`). Tu peux forcer avec `/ask` ou `/do`.
Les tâches n'ont **aucune limite de durée**.

## Installation express : Fedora + GPU AMD (ta config)

Pensé pour : **Fedora 44, Radeon RX 7900 GRE (16 Go VRAM), 32 Go de RAM, Ryzen 5 3600X.**

```bash
git clone https://github.com/Teambull31/daily-briefing && cd daily-briefing
git checkout claude/mobile-jarvis-assistant-klqx0n   # tant que ce n'est pas fusionné
./deploy/install-fedora.sh
```

Le script installe et configure tout : Ollama avec ROCm (l'accélération AMD), les modèles, Python,
OpenCode, le vocal, le bot Telegram (il récupère ton identifiant tout seul), Claude Code en option,
le démarrage automatique et la désactivation de la veille. Il te pose 3-4 questions, rien d'autre.
Compte ~30 Go de téléchargement pour les modèles.

**Modèles choisis pour ta machine**

| Rôle | Modèle | Où il tourne |
|---|---|---|
| Discussion + tri des demandes | `qwen3:14b` (~9 Go) | Entièrement sur la 7900 GRE : réponses rapides |
| Agent de code | `qwen3-coder:30b` (~19 Go) | La majeure partie sur la carte, le reste en RAM. C'est un modèle « MoE » (seule une petite partie travaille à chaque mot), donc ça reste fluide malgré le débordement |
| Variante agent rapide | `gpt-oss:20b` (~13 Go) | Entièrement sur la carte : plus rapide, un peu moins fort en code (`AGENT_RAPIDE` dans `.env`) |
| Vocal | Whisper `small` | Processeur (faster-whisper ne gère pas les cartes AMD) : quelques secondes par message |

Quand une tâche de code tourne, les deux modèles ne tiennent pas ensemble dans les 16 Go : Ollama
alterne, ce qui ajoute quelques secondes à la première réponse suivante. Si ça te gêne,
mets `CHAT_MODEL=qwen3:8b` dans `.env`.

**Dépannage GPU** (si `ollama ps` affiche « CPU » au lieu de « GPU ») :
- `journalctl -u ollama -n 50` : cherche les lignes « amdgpu » / « rocm ».
- Vérifie que l'utilisateur `ollama` est dans les groupes `render` et `video` : `id ollama`.
- En dernier recours, les versions récentes d'Ollama ont un moteur Vulkan : ajoute
  `Environment="OLLAMA_VULKAN=1"` dans `/etc/systemd/system/ollama.service.d/jarvis.conf`,
  puis `sudo systemctl daemon-reload && sudo systemctl restart ollama`.

## Installation manuelle (autres systèmes, ~20 min)

### 1. Ollama + modèles

Installe [Ollama](https://ollama.com/download) (Windows, Linux, macOS), puis choisis selon ta carte graphique :

| VRAM | Discussion (`CHAT_MODEL`) | Agent de code |
|---|---|---|
| 8 Go | `qwen3:8b` | `qwen2.5-coder:7b` |
| 12–16 Go | `qwen3:14b` | `qwen2.5-coder:14b` |
| 24 Go et + | `qwen3:32b` | `qwen3-coder:30b` |

```bash
ollama pull qwen3:14b
ollama pull qwen3-coder:30b
```

> Les modèles évoluent vite : regarde les plus récents sur ollama.com/search.
> `qwen3-coder:30b` fonctionne aussi avec moins de VRAM (une partie passe en RAM), juste plus lentement.
> Pour les agents, augmente le contexte : variable d'environnement `OLLAMA_CONTEXT_LENGTH=32768`.

### 2. L'agent de code (gratuit) : OpenCode

```bash
npm install -g opencode-ai        # nécessite Node.js
```

Copie `opencode.example.json` vers `~/.config/opencode/opencode.json`
(Windows : `%USERPROFILE%\.config\opencode\opencode.json`) et adapte les noms de modèles.
Test : `opencode run -m ollama/qwen3-coder:30b "écris hello.py qui affiche bonjour"`.

### 3. Le bot Telegram

1. Sur Telegram, parle à **@BotFather** → `/newbot` → récupère le token.
2. Installe Jarvis :

```bash
git clone <ce dépôt> daily-briefing && cd daily-briefing
python -m venv .venv
# Linux/WSL : source .venv/bin/activate     Windows : .venv\Scripts\activate
pip install -r requirements.txt
pip install faster-whisper          # optionnel : messages vocaux
cp .env.example .env                # Windows : copy .env.example .env
```

3. Mets le token dans `.env`, lance `python -m jarvis`, envoie `/id` à ton bot,
   copie ton identifiant dans `ALLOWED_USER_IDS`, relance. C'est prêt.

### 4. Démarrage automatique

- **Linux / WSL** : `deploy/jarvis.service` (instructions dans le fichier). Logs : `journalctl --user -u jarvis -f`.
- **Windows** : `deploy/start-jarvis.ps1` + Planificateur de tâches (instructions dans le fichier).
- Pense à désactiver la mise en veille du PC, ou active le Wake-on-LAN.

## Sécurité — à lire

L'agent **exécute des commandes sur ton PC**. Donc :

- `ALLOWED_USER_IDS` est obligatoire : sans lui, personne ne peut rien faire (seul `/id` répond).
- Idéalement, lance Jarvis dans **WSL**, une VM ou un compte utilisateur dédié, pas sur ta session principale.
- Chaque projet est un dépôt git : tu vois quels fichiers ont changé, et tu peux annuler.
- Ne mets jamais le token Telegram dans git (`.env` est ignoré).

## Alterner entre modèles gratuits et Claude Code (abonnement)

Déclare plusieurs agents dans `.env` (voir `.env.example`) :

```ini
AGENT_LOCAL=opencode run -m ollama/qwen3-coder:30b {prompt}
AGENT_CLAUDE=claude -p {prompt} --dangerously-skip-permissions
DEFAULT_AGENT=local
```

Puis depuis ton téléphone :

| Tu envoies | Effet |
|---|---|
| `/claude refais le design du site` | Cette tâche seulement avec Claude Code |
| `Claude, corrige le bug du login` (texte ou vocal) | Pareil |
| `/claude` (seul) ou `/agent claude` | Claude devient l'agent par défaut |
| `/local` ou `/agent local` | Retour aux modèles gratuits |
| `/agent` | Liste des agents et celui qui est actif |

**Abonnement, pas API** : installe Claude Code sur le PC et connecte-toi une fois avec `claude login`
(compte Pro/Max). Jarvis retire `ANTHROPIC_API_KEY` et `ANTHROPIC_AUTH_TOKEN` de l'environnement de
Claude Code : même si une clé API traîne sur ta machine, elle ne sera pas utilisée. Les limites d'usage
de ton abonnement s'appliquent. Si une tâche Claude échoue (limite atteinte par exemple), Jarvis te
propose de la relancer avec l'agent gratuit. Sous Windows, préfère l'installateur natif de Claude Code
(`claude.exe`) à la version npm.

Les questions simples (sans `/claude`) et le briefing restent sur Ollama : ils ne consomment rien.
Un projet garde son dossier et son historique git quel que soit l'agent : tu peux commencer avec
le modèle gratuit et demander à Claude de reprendre.

**Pourquoi alterner ?** Les modèles locaux sont bons pour des scripts, petits sites, automatisations
et explications. Sur les gros projets ou le code complexe, ils se trompent plus souvent. Claude est
bien plus fiable, mais consomme ton quota. Tu gardes donc Claude pour ce qui le mérite.

Alternative sans Jarvis : `claude remote-control` dans un terminal sur ton PC. La session apparaît
dans l'app Claude sur ton téléphone et tourne sur ton PC.

## Et le VPS ?

Ton PC est plus puissant, donc c'est lui qui fait le travail. Le VPS peut servir de solution de secours
toujours allumée : même installation avec un petit modèle (`qwen3:4b`) pour le briefing et les
questions simples quand le PC est éteint — avec un **autre** bot Telegram (un token ne peut être
utilisé que par une seule instance à la fois).

## Tests

```bash
pip install pytest && python -m pytest -q
```
