#!/bin/bash
# ============================================================
# ZettaBrain Lite — Installation Script
# ============================================================
# Installs:
#   1. Ollama (local LLM runtime)
#   2. Default embedding model (nomic-embed-text)
#   3. Default LLM model (phi4-mini — works on any hardware)
#   4. ZettaBrain Lite via pipx
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo -e "${BLUE}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}${BOLD}║         ZettaBrain Lite — Installation               ║${NC}"
echo -e "${BLUE}${BOLD}║   Local AI · RAG + Skills · Your data stays private  ║${NC}"
echo -e "${BLUE}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Detect OS ──────────────────────────────────────────
_OS="linux"
[ "$(uname -s)" = "Darwin" ] && _OS="macos"

# ── Step 1: Install Ollama ─────────────────────────────
info "Step 1/4: Installing Ollama..."

if command -v ollama &>/dev/null; then
  success "Ollama already installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
else
  if [ "$_OS" = "macos" ]; then
    if command -v brew &>/dev/null; then
      info "Installing Ollama via Homebrew..."
      brew install ollama
    else
      error "Homebrew not found. Install Homebrew first:"
      error '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
      exit 1
    fi
  else
    info "Installing Ollama via official installer..."
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  success "Ollama installed."
fi

# ── Step 2: Start Ollama ───────────────────────────────
info "Step 2/4: Starting Ollama service..."

if [ "$_OS" = "macos" ]; then
  if ! pgrep -x "ollama" &>/dev/null; then
    ollama serve &>/dev/null &
    sleep 3
  fi
else
  if command -v systemctl &>/dev/null; then
    sudo systemctl enable ollama 2>/dev/null || true
    sudo systemctl start ollama 2>/dev/null || true
    sleep 2
  else
    if ! pgrep -x "ollama" &>/dev/null; then
      ollama serve &>/dev/null &
      sleep 3
    fi
  fi
fi

# Verify Ollama is responding
for i in 1 2 3 4 5; do
  if curl -s http://localhost:11434 &>/dev/null; then
    success "Ollama is running."
    break
  fi
  [ "$i" -eq 5 ] && { warn "Ollama not responding yet — models will pull when it starts."; }
  sleep 2
done

# ── Step 3: Pull default models ────────────────────────
info "Step 3/4: Pulling default AI models..."

info "Pulling embedding model: nomic-embed-text..."
ollama pull nomic-embed-text 2>/dev/null && success "nomic-embed-text ready." || warn "Could not pull nomic-embed-text (Ollama may not be ready)."

info "Pulling LLM model: phi4-mini..."
ollama pull phi4-mini 2>/dev/null && success "phi4-mini ready." || warn "Could not pull phi4-mini (Ollama may not be ready)."

# ── Step 4: Install ZettaBrain Lite ────────────────────
info "Step 4/4: Installing ZettaBrain Lite..."

if ! command -v pipx &>/dev/null; then
  info "pipx not found, installing..."
  if [ "$_OS" = "macos" ]; then
    brew install pipx
    pipx ensurepath
  else
    if command -v apt-get &>/dev/null; then
      sudo apt-get install -y pipx 2>/dev/null || pip install --user pipx
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y pipx 2>/dev/null || pip install --user pipx
    else
      pip install --user pipx
    fi
    pipx ensurepath 2>/dev/null || true
  fi
fi

# Install from current directory if pyproject.toml exists, otherwise from PyPI
if [ -f "pyproject.toml" ]; then
  info "Installing from local source..."
  pipx install ".[all]" --force 2>/dev/null || pipx install . --force
else
  info "Installing from PyPI..."
  pipx install "zettabrain-lite[all]" --force 2>/dev/null || pipx install zettabrain-lite --force
fi

success "ZettaBrain Lite installed."

# ── Create data directory ──────────────────────────────
if [ "$_OS" = "macos" ]; then
  DATA_DIR="$HOME/Library/Application Support/ZettaBrain-Lite"
else
  DATA_DIR="/opt/zettabrain-lite"
  sudo mkdir -p "$DATA_DIR/data" 2>/dev/null || mkdir -p "$DATA_DIR/data"
fi

# ── Summary ────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Installation Complete!${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  To start ZettaBrain Lite:"
echo -e "    ${CYAN}zettabrain-lite${NC}"
echo ""
echo -e "  Then open in your browser:"
echo -e "    ${CYAN}http://localhost:7860${NC}"
echo ""
echo -e "  Models installed:"
echo -e "    LLM:       phi4-mini"
echo -e "    Embedding: nomic-embed-text"
echo ""
echo -e "  Pull more models from Settings > Pull LLM Model"
echo -e "  Browse available models: ${CYAN}https://ollama.com/library${NC}"
echo ""
