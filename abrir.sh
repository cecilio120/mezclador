#!/usr/bin/env bash
# Abre el Mezclador en su propia ventana (usa GTK4 + WebKit del runtime de GNOME en Flatpak).
DIR="$HOME/.local/share/mezclador"
systemctl --user start mezclador.service 2>/dev/null

for v in 50 49 48 47 46; do
  if flatpak info "org.gnome.Platform//$v" >/dev/null 2>&1; then
    exec flatpak run --command=python3 \
      --socket=wayland --socket=fallback-x11 --share=ipc --device=dri --share=network \
      --filesystem="$DIR":ro \
      "org.gnome.Platform//$v" "$DIR/mezclador-app.py"
  fi
done

# Último recurso: navegador
xdg-open http://127.0.0.1:8765
