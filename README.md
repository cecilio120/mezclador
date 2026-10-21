# Mezclador

Mezclador de audio para Linux (PipeWire) que separa el sonido del PC en canales:
General, Juego, Chat, Multimedia, Extra y Micrófono.

- Volumen y silencio por canal, para tus audífonos y para el stream por separado.
- Arrastra cada aplicación al canal que quieras y se recuerda.
- Balance juego/chat y ganancia del micrófono.
- Modo stream: crea una mezcla aparte para OBS (`Monitor of Stream-Mix`).

## Requisitos

- PipeWire con `pactl` (pipewire-pulse)
- Python 3
- Flatpak (la ventana usa GTK4 + WebKit del runtime de GNOME)

## Instalación

```bash
./install.sh
```

Se instala para tu usuario en `~/.local/share/mezclador`, con un servicio de systemd
de usuario y un acceso en el menú de aplicaciones. Para quitarlo: `./uninstall.sh`.
