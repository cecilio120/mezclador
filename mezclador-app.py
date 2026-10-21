#!/usr/bin/env python3
"""Ventana propia del Mezclador (GTK4 + WebKit), sin abrir el navegador."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, Gtk, WebKit  # noqa: E402

URL = "http://127.0.0.1:8765"
APP_ID = "io.github.mezclador.Mezclador"


class Mezclador(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", True)

        win = Gtk.ApplicationWindow(application=self, title="Mezclador")
        win.set_default_size(1480, 860)
        win.set_icon_name("audio-volume-high")

        view = WebKit.WebView()
        view.set_background_color(_rgba("#141311"))
        view.connect("context-menu", lambda *a: True)          # sin menú de navegador
        view.connect("load-failed", self.on_fail)
        ws = view.get_settings()
        ws.set_enable_developer_extras(False)
        win.set_child(view)
        view.load_uri(URL)
        win.present()

    def on_fail(self, view, event, uri, error):
        # El servicio aún no arranca: reintentar cada segundo
        GLib.timeout_add_seconds(1, lambda: (view.load_uri(URL), False)[1])
        return True


def _rgba(hex_color):
    from gi.repository import Gdk
    c = Gdk.RGBA()
    c.parse(hex_color)
    return c


if __name__ == "__main__":
    Mezclador().run(None)
