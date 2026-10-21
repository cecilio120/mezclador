#!/usr/bin/env bash
# Quita el Mezclador. Al detenerse devuelve todo el audio a tus audífonos.
systemctl --user disable --now mezclador.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/mezclador.service" \
      "$HOME/.local/share/applications/mezclador.desktop" \
      "$HOME/.local/share/applications/io.github.mezclador.Mezclador.desktop"
rm -rf "$HOME/.local/share/mezclador"
systemctl --user daemon-reload
echo "Mezclador eliminado. Tu configuración sigue en ~/.config/mezclador (puedes borrarla)."
