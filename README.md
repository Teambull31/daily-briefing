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
| `/taches`, `/log 3`, `/stop 3`, `/relancer 3`, `/get index.html` | Suivre, arrêter, relancer, récupérer un fichier |
| `/suite ajoute aussi un mode sombre` | Continue la dernière tâche du projet **dans la même session** de l'agent (il se souvient de ce qu'il vient de faire) |
| `/web prix d'une RTX 5070` ou « quels sont les résultats du match d'hier ? » | Cherche sur internet (SearXNG), lit les pages et répond en citant ses sources |
| `/etat` | Charge GPU, VRAM, température, RAM, disque, modèles chargés dans Ollama (et % sur GPU), tâches en cours |
| `/claude <tâche>` ou « Claude, … » | Utilise Claude Code (ton abonnement) au lieu du modèle gratuit |
| « Rappelle-moi demain à 9h d'appeler le garage » | Rappel programmé (`/rappels` pour la liste) |
| « Souviens-toi que je code surtout en Python » | Retenu durablement, utilisé dans les réponses (`/memoire`) |
| 📎 Une photo ou un fichier (avec ou sans légende) | Rangé dans `inbox/` du projet ; la légende devient une demande |

Jarvis choisit seul entre « répondre » et « agir » (`AUTO_ROUTE=1`). Tu peux forcer avec `/ask` ou `/do`.
Les tâches n'ont **aucune limite de durée**.
Pendant une tâche, le message « 🛠 Tâche lancée » se met à jour toutes les 5 minutes avec ce que fait
l'agent (sans te notifier à chaque fois). Le projet actif, l'agent choisi, la conversation et l'historique
des tâches sont conservés quand le PC ou Jarvis redémarre ; si une tâche a été coupée, Jarvis te prévient
au démarrage et `/relancer` la reprend.

Les rappels comprennent « dans 20 min », « à 18h30 », « ce soir », « demain à 9h », « après-demain à 14 heures »
instantanément ; pour le reste (« lundi prochain », « le 3 à midi »), c'est l'IA locale qui lit la date.
Un rappel prévu pendant que le PC était éteint est envoyé dès le redémarrage, marqué « en retard ».
Jarvis connaît aussi la date et l'heure du jour, et tout ce que tu lui as demandé de retenir.

## Installation express : Fedora + GPU AMD (ta config)

Pensé pour : **Fedora 44, Radeon RX 7900 GRE (16 Go VRAM), 32 Go de RAM, Ryzen 5 3600X.**

```bash
git clone https://github.com/Teambull31/daily-briefing && cd daily-briefing
git checkout claude/mobile-jarvis-assistant-klqx0n   # tant que ce n'est pas fusionné
./deploy/install-fedora.sh
```

Le script installe et configure tout : Ollama avec ROCm (l'accélération AMD), les modèles, Python,
OpenCode, le vocal (reconnaissance + voix de Jarvis), la recherche web (SearXNG dans Podman, en option), le bot Telegram (il récupère ton identifiant tout seul), Claude Code en option,
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

## Coach de productivité : finir ce que tu commences

| Tu envoies | Jarvis fait |
|---|---|
| `/todo rédiger le devis` (une tâche par ligne, `!` devant = urgent) ou « ajoute à ma liste … » | L'ajoute à ta liste |
| `/next` | Te donne **une seule** chose à faire maintenant, pas toute la liste |
| `/decoupe créer le site de l'asso` | L'IA découpe l'objectif en 3 à 8 petites étapes concrètes (< 30 min), ajoutées à ta liste |
| `/focus` · `/focus 45 x3 sur le rapport` · « focus 30 min sur la compta » | Session de concentration (pomodoro) ; sans sujet, prend ta prochaine tâche. Pause, reprise, relance automatique |
| `/fait` (ou `/fait 2`) | Coche, te félicite et te dit quoi faire ensuite |
| `/bilan` | Tâches cochées, minutes de concentration, travail de l'agent, ce qui reste |
| `/voix off\|rappels\|tout` | Rappels, fins de focus, briefing et bilan en **messages vocaux** (et même tes réponses avec `tout`) |

- **Rappels audio** : voix française générée en local par [Piper](https://github.com/OHF-Voice/piper1-gpl)
  (gratuit, ~0,5 s par phrase sur le processeur), envoyée en vrai message vocal Telegram (via ffmpeg).
  Avec `PC_AUDIO=1`, elle est aussi jouée sur les haut-parleurs du PC.
- **Pendant un focus**, les notifications de fin de tâche de l'agent arrivent **sans sonnerie** ; seuls tes rappels sonnent.
- **Rituels** : le briefing du matin inclut ta liste du jour, tes rappels du jour et ta concentration d'hier ;
  le bilan du soir (`REVIEW_TIME=20:30`) te demande par quoi commencer demain.
- La session de focus survit à un redémarrage du PC ; une session arrêtée compte quand même le temps passé.

### Habitudes, relances, statistiques, agenda

| Tu envoies | Jarvis fait |
|---|---|
| `/habitude sport`, puis « j'ai fait du sport » ou `/check sport` | Suit l'habitude, compte la série 🔥 et fête les paliers (7, 14, 30, 100 jours) |
| `/habitudes` | Ce qui est fait ou pas aujourd'hui, avec les séries |
| (rien : c'est automatique) | **Relance** si ta tâche prioritaire n'a pas bougé depuis `NUDGE_HOURS` pendant tes heures de travail — jamais pendant un focus ni en plein rendez-vous |
| `/plustard` | Repousse la tâche du moment en fin de liste et remet les relances à zéro |
| `/stats` · `/stats 30` | **Graphique** : minutes de concentration, tâches cochées et habitudes tenues par jour, + résumé chiffré. Envoyé aussi chaque dimanche avec le bilan |
| `/agenda` · `/agenda demain` | Tes rendez-vous (et dans le briefing du matin) |
| (automatique) | Rappel vocal `EVENT_REMINDER_MINUTES` avant chaque rendez-vous |
| `/bloquer` · `/bloquer 90` | Trouve le prochain créneau libre dans tes heures de travail, t'envoie un fichier `.ics` à toucher pour l'ajouter à ton agenda, et te rappelle de lancer `/focus` à l'heure |

**Connecter Google Agenda** (2 minutes, lecture seule, sans compte développeur) : sur ordinateur, Google Agenda →
⚙️ Paramètres → clique sur ton agenda → « Intégrer l'agenda » → **Adresse secrète au format iCal** → copie-la dans
`CALENDAR_ICS_URLS` du `.env`. Marche aussi avec Proton, Nextcloud, Outlook, iCloud (toute adresse `.ics`).
Jarvis ne peut pas écrire dans ton agenda par ce biais : c'est pour ça que `/bloquer` t'envoie un fichier `.ics` à ajouter d'un geste.

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
