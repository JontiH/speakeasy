#!/usr/bin/env bash
# Installs speakeasy on Ubuntu 24.04 / GNOME Wayland. Safe to re-run.
# Run as your normal user; it calls sudo where needed.
#
#   ./install.sh                      # defaults
#   ./install.sh --from <path|URL>    # also install your personal config.toml
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
FROM=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:?--from needs a path or URL}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

GREEN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'; CYA=$'\e[36m'; RST=$'\e[0m'
info() { echo "${CYA}==>${RST} $*"; }
ok()   { echo "${GREEN}  ok${RST} $*"; }
warn() { echo "${YEL}  !!${RST} $*"; }
die()  { echo "${RED}error:${RST} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as your normal user, not root. The script calls sudo itself."

info "Requesting sudo"
sudo -v

info "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y python3-venv ffmpeg wl-clipboard libnotify-bin ydotool ydotoold
ok "packages installed"

info "Granting the input group access to /dev/uinput"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/uinput || true
ok "udev rule and uinput module configured"

NEED_RELOGIN=0
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  ok "$USER is already in the input group"
else
  sudo usermod -aG input "$USER"
  ok "added $USER to the input group"
fi
# Group membership only reaches processes started after the next login.
if ! id -nG | tr ' ' '\n' | grep -qx input; then
  NEED_RELOGIN=1
fi

info "Setting up the Python environment"
[[ -x "$HERE/venv/bin/python" ]] || python3 -m venv "$HERE/venv"
"$HERE/venv/bin/python" -m pip install --quiet --upgrade pip
"$HERE/venv/bin/python" -m pip install --quiet faster-whisper numpy "onnx-asr[cpu,hub]"
ok "venv ready"

info "Downloading the Whisper and Parakeet models (about 1.3 GB the first time)"
"$HERE/speakeasy" --download

info "Installing the ydotoold user service"
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$HERE/systemd/ydotoold.service" "$HOME/.config/systemd/user/ydotoold.service"
systemctl --user daemon-reload
systemctl --user enable ydotoold >/dev/null 2>&1
if [[ $NEED_RELOGIN -eq 0 ]]; then
  systemctl --user restart ydotoold
  sleep 1
  if [[ -S /tmp/.ydotool_socket ]]; then
    ok "ydotoold running"
  else
    warn "ydotoold did not create /tmp/.ydotool_socket. Check: journalctl --user -u ydotoold"
  fi
else
  ok "ydotoold enabled, it will start after you log in again"
fi

info "Installing the focused-window GNOME extension"
EXT_UUID="focused-window@speakeasy"
mkdir -p "$HOME/.local/share/gnome-shell/extensions"
rm -rf "$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
cp -r "$HERE/gnome-extension/$EXT_UUID" "$HOME/.local/share/gnome-shell/extensions/"
# gnome-extensions enable fails until the shell has seen the extension (next
# login on Wayland), so add it to the enabled list directly.
ENABLED="$(gsettings get org.gnome.shell enabled-extensions)"
NEW_ENABLED="$(python3 -c '
import ast, sys
cur, uuid = sys.argv[1], sys.argv[2]
items = [] if cur.startswith("@") else ast.literal_eval(cur)
if uuid not in items:
    items.append(uuid)
print(str(items))
' "$ENABLED" "$EXT_UUID")"
gsettings set org.gnome.shell enabled-extensions "$NEW_ENABLED"
if gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/FocusedWindow \
     --method org.gnome.Shell.FocusedWindow.Get >/dev/null 2>&1; then
  ok "extension active"
else
  NEED_RELOGIN=1
  ok "extension installed, it loads at your next login"
fi

info "Personal config, mic and hotkeys"
"$HERE/speakeasy" --setup ${FROM:+"$FROM"}

echo
echo "${GREEN}Install complete.${RST}"
if [[ $NEED_RELOGIN -eq 1 ]]; then
  echo "${YEL}Log out and back in${RST} so the input group and GNOME extension apply, then test."
fi
cat <<EOF

Test:
  1. Click into any text field.
  2. Press the toggle hotkey (Super+/ by default), speak, press it again.
     The cancel hotkey (Super+') throws the recording away instead.
  3. The text is typed at the cursor and also copied to the clipboard.

If typing fails, check: systemctl --user status ydotoold
EOF
