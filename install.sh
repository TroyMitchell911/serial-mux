#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/TroyMitchell911/serial-mux.git"
INSTALL_DIR="/usr/local/lib/serial-mux"
BIN_DIR="/usr/local/bin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Check dependencies
for cmd in git python3 pip3; do
    command -v "$cmd" >/dev/null 2>&1 || error "'$cmd' not found. Please install it first."
done

# Need root for /usr/local
if [ "$(id -u)" -ne 0 ]; then
    error "Run with sudo: curl -fsSL <url> | sudo bash"
fi

info "Cloning serial-mux..."
if [ -d "$INSTALL_DIR" ]; then
    warn "$INSTALL_DIR exists, updating..."
    cd "$INSTALL_DIR" && git pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi

info "Installing Python dependencies..."
pip3 install --break-system-packages pyserial pyyaml 2>/dev/null \
    || pip3 install pyserial pyyaml

info "Creating wrapper scripts in $BIN_DIR..."

# serial-mux
cat > "$BIN_DIR/serial-mux" << 'WRAPPER'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/usr/local/lib/serial-mux")
from serial_mux.cli import main
main()
WRAPPER
chmod +x "$BIN_DIR/serial-mux"

# smtty
cat > "$BIN_DIR/smtty" << 'WRAPPER'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/usr/local/lib/serial-mux")
from serial_mux.client import main
main()
WRAPPER
chmod +x "$BIN_DIR/smtty"

info "Installed:"
info "  serial-mux  -> $BIN_DIR/serial-mux"
info "  smtty       -> $BIN_DIR/smtty"

# Detect serial group
SERIAL_GROUP=""
if getent group uucp >/dev/null 2>&1; then
    SERIAL_GROUP="uucp"
elif getent group dialout >/dev/null 2>&1; then
    SERIAL_GROUP="dialout"
fi

if [ -n "$SERIAL_GROUP" ]; then
    REAL_USER="${SUDO_USER:-$USER}"
    if ! id -nG "$REAL_USER" | grep -qw "$SERIAL_GROUP"; then
        warn "User '$REAL_USER' is not in the '$SERIAL_GROUP' group."
        warn "Run: sudo usermod -aG $SERIAL_GROUP $REAL_USER"
        warn "Then log out and back in."
    fi
fi

echo ""
info "Installation complete! Try: serial-mux --help"
