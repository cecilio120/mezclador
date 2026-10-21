#!/usr/bin/env bash
# Instala el Mezclador para tu usuario (no toca el sistema base de Bazzite).
set -e
cd "$(dirname "$0")"

if ! command -v pactl >/dev/null 2>&1; then
  echo "⚠️  No se encontró 'pactl'. El mezclador lo necesita para manejar el audio."
  echo "    Avísale a Claude con este mensaje y lo resolvemos."
  exit 1
fi

# La ventana usa GTK4 + WebKit del runtime de GNOME (Flatpak)
HAVE_RT=""
for v in 50 49 48 47 46; do
  flatpak info "org.gnome.Platform//$v" >/dev/null 2>&1 && HAVE_RT=1 && break
done
if [ -z "$HAVE_RT" ]; then
  echo "Instalando componentes para la ventana (una sola vez)…"
  flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  flatpak install --user -y flathub org.gnome.Platform//48
fi

DEST="$HOME/.local/share/mezclador"
APPS="$HOME/.local/share/applications"
mkdir -p "$DEST" "$HOME/.config/systemd/user" "$APPS" "$HOME/.config/autostart"
cp mezclador.py mezclador.html mezclador-app.py abrir.sh "$DEST/"
chmod +x "$DEST/mezclador.py" "$DEST/abrir.sh"
rm -f "$APPS/mezclador.desktop"                      # versión anterior

# Servicio que crea los canales al iniciar sesión
cat > "$HOME/.config/systemd/user/mezclador.service" <<EOF
[Unit]
Description=Mezclador (canales Game/Chat/Media/Aux)
After=pipewire-pulse.service wireplumber.service
Wants=pipewire-pulse.service wireplumber.service

[Service]
ExecStart=/usr/bin/python3 $DEST/mezclador.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Acceso en el menú de aplicaciones
cat > "$APPS/io.github.mezclador.Mezclador.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Mezclador
Comment=Canales de audio (Game, Chat, Media, Aux, Mic)
Exec=$DEST/abrir.sh
Icon=audio-volume-high
Terminal=false
Categories=AudioVideo;Audio;Mixer;
StartupWMClass=io.github.mezclador.Mezclador
EOF

systemctl --user daemon-reload
systemctl --user enable mezclador.service >/dev/null 2>&1
systemctl --user restart mezclador.service
sleep 2

if systemctl --user is-active --quiet mezclador.service; then
  echo "✅ Mezclador instalado. Los canales se crean solos cada vez que entras a Bazzite."
  echo "   Ábrelo desde el menú de aplicaciones: 'Mezclador'"
  nohup "$DEST/abrir.sh" >/dev/null 2>&1 &
else
  echo "❌ El servicio no arrancó. Mándale a Claude lo que sale con:"
  echo "   journalctl --user -u mezclador -n 30"
fi
