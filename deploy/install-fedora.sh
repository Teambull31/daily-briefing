#!/usr/bin/env bash
# Installation complète de Jarvis sur Fedora (pensé pour Fedora 44 + GPU AMD Radeon).
# Usage : ./deploy/install-fedora.sh   (avec ton utilisateur normal, sudo sera demandé)
# Relançable sans risque : chaque étape déjà faite est sautée.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHAT_MODEL="${CHAT_MODEL:-qwen3:14b}"
AGENT_MODEL="${AGENT_MODEL:-qwen3-coder:30b}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-32768}"
ENV_FILE="$REPO_DIR/.env"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ask_yes() { local r; read -rp "    $1 [o/N] " r; [[ "${r,,}" == o* || "${r,,}" == y* ]]; }

# Écrit KEY=VALUE dans .env (remplace la ligne existante, commentée ou non).
set_env() {
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import re, sys
path, key, value = sys.argv[1:]
text = open(path, encoding="utf-8").read()
line = f"{key}={value}"
pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.M)
text = pattern.sub(lambda _: line, text, count=1) if pattern.search(text) else text.rstrip("\n") + "\n" + line + "\n"
open(path, "w", encoding="utf-8").write(text)
PY
}

comment_env() { sed -i "s|^$1=|# $1=|" "$ENV_FILE"; }

if [[ $EUID -eq 0 ]]; then
    echo "Lance ce script avec ton utilisateur normal (pas root) : il utilisera sudo au besoin."
    exit 1
fi

step "Paquets système"
sudo dnf install -y git python3 python3-pip curl zstd pciutils podman

step "Ollama (IA locale, accélérée par ta Radeon via ROCm)"
if command -v ollama >/dev/null; then
    info "Déjà installé : $(ollama --version 2>/dev/null | tail -1)"
else
    curl -fsSL https://ollama.com/install.sh | sh
fi
# Contexte long (indispensable pour les agents de code) + modèles gardés en mémoire 30 min.
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/jarvis.conf >/dev/null <<EOF
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=$CONTEXT_LENGTH"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
sudo systemctl daemon-reload
sudo systemctl enable ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama
for _ in $(seq 1 30); do
    curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 && break
    sleep 1
done

step "Modèles : $CHAT_MODEL (discussion) et $AGENT_MODEL (code) — plusieurs Go à télécharger"
ollama pull "$CHAT_MODEL"
ollama pull "$AGENT_MODEL"

step "Vérification du GPU"
ollama run "$CHAT_MODEL" "Réponds uniquement : OK" >/dev/null 2>&1 || true
ollama ps
if ollama ps | grep -q "GPU"; then
    info "✅ Le modèle tourne sur la carte graphique."
else
    info "⚠️  Le modèle semble tourner sur le processeur. Voir « Dépannage GPU » dans le README."
fi

step "Environnement Python de Jarvis"
cd "$REPO_DIR"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt faster-whisper
info "Dépendances installées (vocal compris)."

step "OpenCode (agent de code gratuit)"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
if command -v opencode >/dev/null; then
    info "Déjà installé."
else
    curl -fsSL https://opencode.ai/install | bash
fi
mkdir -p "$HOME/.config/opencode"
if [[ -f "$HOME/.config/opencode/opencode.json" ]]; then
    info "$HOME/.config/opencode/opencode.json existe déjà : non modifié."
else
    cp opencode.example.json "$HOME/.config/opencode/opencode.json"
    info "Configuration copiée dans ~/.config/opencode/opencode.json"
fi

step "Configuration du bot Telegram"
[[ -f "$ENV_FILE" ]] || cp .env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"
set_env CHAT_MODEL "$CHAT_MODEL"
set_env AGENT_LOCAL "opencode run -m ollama/$AGENT_MODEL {prompt}"

current_token="$(grep -E '^TELEGRAM_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -z "$current_token" ]]; then
    info "Sur Telegram : ouvre @BotFather, envoie /newbot, choisis un nom, copie le token."
    read -rp "    Colle le token ici : " current_token
    current_token="$(echo "$current_token" | tr -d '[:space:]')"
    set_env TELEGRAM_TOKEN "$current_token"
fi

if ! grep -qE '^ALLOWED_USER_IDS=[0-9]' "$ENV_FILE"; then
    systemctl --user stop jarvis 2>/dev/null || true   # sinon il intercepterait le message
    info "Envoie maintenant « bonjour » à ton bot sur Telegram, puis appuie sur Entrée."
    read -r _
    user_id="$(curl -fsS "https://api.telegram.org/bot${current_token}/getUpdates" | python3 -c '
import json, sys
msgs = [u.get("message") for u in json.load(sys.stdin).get("result", []) if u.get("message")]
print(msgs[-1]["from"]["id"] if msgs else "")')" || user_id=""
    if [[ -n "$user_id" ]]; then
        set_env ALLOWED_USER_IDS "$user_id"
        info "✅ Ton identifiant ($user_id) est autorisé."
    else
        info "⚠️  Aucun message reçu. Plus tard : envoie /id au bot et mets le nombre dans ALLOWED_USER_IDS (.env)."
    fi
fi

step "Recherche web (SearXNG, gratuit et privé)"
if ask_yes "Installer SearXNG pour que Jarvis puisse chercher sur internet (/web) ?"; then
    mkdir -p "$HOME/.config/containers/systemd" "$HOME/.config/searxng" "$HOME/.local/share/searxng"
    if [[ ! -f "$HOME/.config/searxng/settings.yml" ]]; then
        secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        sed "s/CHANGE-MOI/$secret/" deploy/searxng-settings.yml > "$HOME/.config/searxng/settings.yml"
    fi
    cp deploy/searxng.container "$HOME/.config/containers/systemd/searxng.container"
    systemctl --user daemon-reload
    systemctl --user restart searxng
    for _ in $(seq 1 60); do  # premier démarrage : téléchargement de l'image
        curl -fsS "http://localhost:8888/search?q=test&format=json" >/dev/null 2>&1 && break
        sleep 2
    done
    set_env SEARXNG_URL "http://localhost:8888"
    if curl -fsS "http://localhost:8888/search?q=test&format=json" >/dev/null 2>&1; then
        info "✅ SearXNG répond sur http://localhost:8888"
    else
        info "⚠️  SearXNG ne répond pas encore : systemctl --user status searxng"
    fi
fi

step "Claude Code (optionnel, utilise ton abonnement Claude)"
if command -v claude >/dev/null; then
    info "Déjà installé. Si ce n'est pas fait : lance « claude » une fois pour te connecter."
elif ask_yes "Installer Claude Code pour pouvoir utiliser /claude ?"; then
    curl -fsSL https://claude.ai/install.sh | bash
    info "Ensuite, lance « claude » une fois dans un terminal et connecte-toi avec ton compte Pro/Max."
else
    comment_env AGENT_CLAUDE
    info "Agent claude désactivé (décommente AGENT_CLAUDE dans .env pour l'activer plus tard)."
fi

step "Démarrage automatique (service systemd utilisateur)"
mkdir -p "$HOME/.config/systemd/user"
sed "s|%h/daily-briefing|$REPO_DIR|g" deploy/jarvis.service > "$HOME/.config/systemd/user/jarvis.service"
systemctl --user daemon-reload
systemctl --user enable jarvis >/dev/null
systemctl --user restart jarvis
sudo loginctl enable-linger "$USER"   # tourne même sans session ouverte
info "Logs en direct : journalctl --user -u jarvis -f"

step "Mise en veille"
if command -v gsettings >/dev/null && ask_yes "Empêcher le PC de se mettre en veille automatiquement (sur secteur) ?"; then
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
    info "Veille automatique désactivée."
fi

step "Terminé 🎉"
info "Envoie /aide à ton bot sur Telegram."
