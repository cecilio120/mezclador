#!/usr/bin/env python3
"""
Mezclador — un mezclador tipo SteelSeries Sonar para Linux (PipeWire).

Crea los canales virtuales GAME, CHAT, MEDIA y AUX, manda cada aplicación
a su canal de forma automática y deja dos mezclas independientes:
  * Audífonos (lo que tú escuchas)
  * Stream    (lo que captura OBS: dispositivo "Stream-Mix")

La interfaz se abre en el navegador en http://127.0.0.1:8765
Uso:  python3 mezclador.py          (normal)
      python3 mezclador.py --demo   (sin tocar el audio, para probar la interfaz)
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MEZCLADOR_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "mezclador")
CONF_FILE = os.path.join(CONF_DIR, "config.json")

CHANNELS = ["game", "chat", "media", "aux"]
SIDES = ["hp", "st"]                       # hp = audífonos, st = stream
PREFIX = "mezclador_"
STREAM_SINK = PREFIX + "stream"
LOOP_TAG = "MezcladorLoop"                 # nombre de las conexiones internas
SINK_DESC = {"game": "Game", "chat": "Chat", "media": "Media", "aux": "Aux", "stream": "Stream-Mix"}

DEFAULT_RULES = {
    # Chat
    "discord": "chat", "vesktop": "chat", "webcord": "chat", "teamspeak": "chat",
    "teamspeak3": "chat", "mumble": "chat", "zoom": "chat", "telegram": "chat",
    # Media
    "spotify": "media", "vlc": "media", "mpv": "media", "elisa": "media",
    "rhythmbox": "media", "strawberry": "media", "youtube music": "media",
    # Aux (navegadores)
    "google chrome": "aux", "chrome": "aux", "chromium": "aux", "firefox": "aux",
    "opera": "aux", "opera gx": "aux", "brave": "aux", "microsoft edge": "aux",
}

DEFAULT_CONFIG = {
    "output": None,            # dispositivo de audífonos (nombre del sink)
    "mic": None,               # micrófono (nombre de la source)
    "vol": {
        "master": {"hp": 100, "st": 100},
        "game": {"hp": 100, "st": 100},
        "chat": {"hp": 100, "st": 100},
        "media": {"hp": 100, "st": 100},
        "aux": {"hp": 100, "st": 100},
        "mic": {"hp": 50, "st": 100},     # hp = escucharte a ti mismo
    },
    "mute": {
        "master": {"hp": False, "st": False},
        "game": {"hp": False, "st": False},
        "chat": {"hp": False, "st": False},
        "media": {"hp": False, "st": False},
        "aux": {"hp": False, "st": False},
        "mic": {"hp": True, "st": False},
    },
    "mic_gain": 100,
    "chatmix": 50,             # 0 = todo juego, 100 = todo chat
    "stream_mode": False,      # False = modo normal (solo audífonos)
    "default_channel": "game",
    "rules": DEFAULT_RULES,
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def deep_merge(base, extra):
    out = json.loads(json.dumps(base))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "rules":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clamp(v, lo=0, hi=150):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def good(v):
    return isinstance(v, str) and v.strip() not in ("", "(null)")


def dev_name(d):
    """Nombre del dispositivo. Algunas versiones de pactl devuelven "(null)"."""
    props = d.get("properties") or {}
    for v in (d.get("name"), props.get("node.name")):
        if good(v):
            return v
    return ""


def dev_desc(d):
    props = d.get("properties") or {}
    for v in (d.get("description"), props.get("device.description"), props.get("node.description"),
              props.get("node.nick"), props.get("device.product.name"), dev_name(d)):
        if good(v):
            return v
    return "Dispositivo sin nombre"


def app_key(props):
    name = (props.get("application.name") or "").strip()
    binary = (props.get("application.process.binary") or "").strip()
    shown = name or binary or "Desconocido"
    if shown.lower().startswith("wine") and binary:      # juegos de Proton/Wine
        shown = binary.rsplit(".", 1)[0]
    return (name or binary or "desconocido").lower(), shown, binary.lower()


# ─────────────────────────────────────────────────────────────────────────────
class BaseMixer:
    """Lógica común: configuración, ChatMix, reglas. Los hijos aplican al audio."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = self.load_config()
        self.error = None

    # ── configuración ──
    def load_config(self):
        try:
            with open(CONF_FILE) as f:
                return deep_merge(DEFAULT_CONFIG, json.load(f))
        except FileNotFoundError:
            return json.loads(json.dumps(DEFAULT_CONFIG))
        except Exception as e:  # archivo dañado
            log("config dañada, uso valores por defecto:", e)
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def save_config(self):
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONF_FILE)

    # ── cálculo de volúmenes efectivos ──
    def chatmix_factor(self, chan):
        cm = self.cfg["chatmix"]
        if chan == "game":
            return 1.0 if cm <= 50 else (100 - cm) / 50
        if chan == "chat":
            return 1.0 if cm >= 50 else cm / 50
        return 1.0

    def effective(self, chan, side):
        """Volumen final (0-150) y mute de una conexión canal→salida."""
        v = self.cfg["vol"][chan][side]
        m = self.cfg["mute"][chan][side]
        if side == "hp":
            v = v * self.chatmix_factor(chan)
        else:
            v = v * self.cfg["vol"]["master"]["st"] / 100
            m = m or self.cfg["mute"]["master"]["st"]
        return clamp(v), m

    def rule_for(self, key, binary):
        rules = self.cfg["rules"]
        if key in rules:
            return rules[key]
        if binary and binary in rules:
            return rules[binary]
        for k, ch in rules.items():
            if len(k) >= 4 and k in key:
                return ch
        return None

    # ── API pública (llamada desde la interfaz) ──
    def set_vol(self, chan, side, value):
        with self.lock:
            self.cfg["vol"][chan][side] = clamp(value)
            self.save_config()
            self.apply_all()

    def set_mute(self, chan, side, value):
        with self.lock:
            self.cfg["mute"][chan][side] = bool(value)
            self.save_config()
            self.apply_all()

    def set_chatmix(self, value):
        with self.lock:
            self.cfg["chatmix"] = clamp(value, 0, 100)
            self.save_config()
            self.apply_all()

    def set_mic_gain(self, value):
        with self.lock:
            self.cfg["mic_gain"] = clamp(value)
            self.save_config()
            self.apply_all()

    def set_route(self, key, chan):
        if chan not in CHANNELS:
            return
        with self.lock:
            self.cfg["rules"][key] = chan
            self.save_config()
            self.route_all(force=True)

    def set_output(self, name):
        with self.lock:
            if name and name.startswith(PREFIX):
                return
            self.cfg["output"] = name
            self.save_config()
            self.rebuild()

    def set_mic(self, name):
        with self.lock:
            self.cfg["mic"] = name
            self.save_config()
            self.rebuild()

    def set_stream_mode(self, value):
        with self.lock:
            self.cfg["stream_mode"] = bool(value)
            self.save_config()
            self.update_mode()

    def set_default_channel(self, chan):
        if chan in CHANNELS:
            with self.lock:
                self.cfg["default_channel"] = chan
                self.save_config()
                self.route_all(force=True)

    # A implementar por cada backend
    def apply_all(self): ...
    def route_all(self, force=False): ...
    def rebuild(self): ...
    def update_mode(self): self.rebuild()
    def state(self): ...
    def start(self): ...
    def stop(self): ...


# ─────────────────────────────────────────────────────────────────────────────
class PipeWireMixer(BaseMixer):
    """Backend real: usa pactl (PipeWire-Pulse)."""

    def __init__(self):
        super().__init__()
        self.modules = {}          # "sink:game" / "loop:game:hp" → id de módulo
        self.moved = {}            # índice sink-input → sink al que lo mandamos
        self.dirty = threading.Event()
        self.topology_dirty = threading.Event()
        self.running = True
        self.sub_proc = None
        self.cache = {"sinks": [], "sources": [], "inputs": [], "outputs": []}

    # ── pactl ──
    def pactl(self, *args, js=False, check=False):
        cmd = ["pactl"] + (["-f", "json"] if js else []) + [str(a) for a in args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        except subprocess.TimeoutExpired:
            log("pactl tardó demasiado:", " ".join(cmd))
            return None
        if r.returncode != 0:
            if check:
                log("error pactl:", " ".join(cmd), r.stderr.strip())
            return None
        if js:
            try:
                return json.loads(r.stdout or "[]")
            except json.JSONDecodeError:
                return []
        return r.stdout

    def refresh_cache(self):
        self.cache["sinks"] = self.pactl("list", "sinks", js=True) or []
        self.cache["sources"] = self.pactl("list", "sources", js=True) or []
        self.cache["inputs"] = self.pactl("list", "sink-inputs", js=True) or []
        self.cache["outputs"] = self.pactl("list", "source-outputs", js=True) or []

    def sink_names(self):
        return {dev_name(s) for s in self.cache["sinks"]}

    def hw_sinks(self):
        return [s for s in self.cache["sinks"] if dev_name(s) and not dev_name(s).startswith(PREFIX)]

    def mics(self):
        return [s for s in self.cache["sources"]
                if dev_name(s) and not dev_name(s).endswith(".monitor")
                and not dev_name(s).startswith(PREFIX)
                and (s.get("properties", {}).get("device.class") != "monitor")]

    def default_sink(self):
        return (self.pactl("get-default-sink") or "").strip()

    def default_source(self):
        return (self.pactl("get-default-source") or "").strip()

    # ── creación / limpieza ──
    def cleanup_leftovers(self):
        out = self.pactl("list", "short", "modules") or ""
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and (PREFIX in parts[2] or LOOP_TAG in parts[2]):
                self.pactl("unload-module", parts[0])

    def load(self, key, *args):
        out = self.pactl("load-module", *args, check=True)
        if out and out.strip().isdigit():
            self.modules[key] = out.strip()
            return True
        return False

    def unload(self, key):
        mid = self.modules.pop(key, None)
        if mid:
            self.pactl("unload-module", mid)

    def ensure_sinks(self):
        names = self.sink_names()
        for ch in CHANNELS + ["stream"]:
            sink = PREFIX + ch
            if sink not in names:
                self.modules.pop("sink:" + ch, None)
                self.load("sink:" + ch, "module-null-sink", f"sink_name={sink}",
                          "channels=2", "channel_map=front-left,front-right",
                          f"sink_properties=device.description={SINK_DESC[ch]}")

    def loop_name(self, chan, side):
        return f"{LOOP_TAG}-{chan}-{side}"

    def ensure_loops(self):
        out = self.cfg["output"]
        mic = self.cfg["mic"]
        sinks = self.sink_names()
        sources = {dev_name(s) for s in self.cache["sources"]}
        have = {(i.get("properties") or {}).get("application.name") for i in self.cache["inputs"]}

        wanted = {}
        for ch in CHANNELS:
            if out in sinks:
                wanted[(ch, "hp")] = (PREFIX + ch + ".monitor", out)
            if self.cfg["stream_mode"]:
                wanted[(ch, "st")] = (PREFIX + ch + ".monitor", STREAM_SINK)
        if mic in sources:
            if out in sinks:
                wanted[("mic", "hp")] = (mic, out)
            if self.cfg["stream_mode"]:
                wanted[("mic", "st")] = (mic, STREAM_SINK)

        # En modo normal se quitan las conexiones hacia el Stream-Mix
        if not self.cfg["stream_mode"]:
            for ch in CHANNELS + ["mic"]:
                self.unload(f"loop:{ch}:st")
            out_mods = self.pactl("list", "short", "modules") or ""
            for line in out_mods.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and LOOP_TAG in parts[2] and "-st " in parts[2] + " ":
                    self.pactl("unload-module", parts[0])

        for (ch, side), (src, dst) in wanted.items():
            name = self.loop_name(ch, side)
            if name in have:
                continue
            self.unload(f"loop:{ch}:{side}")
            self.load(f"loop:{ch}:{side}", "module-loopback",
                      f"source={src}", f"sink={dst}", "latency_msec=25",
                      "source_dont_move=true", "sink_dont_move=true",
                      f"sink_input_properties=application.name={name}",
                      f"source_output_properties=application.name={name}")

    def pick_defaults(self):
        changed = False
        hw = [dev_name(s) for s in self.hw_sinks()]
        if self.cfg["output"] not in hw and hw:
            d = self.default_sink()
            self.cfg["output"] = d if d in hw else hw[0]
            changed = True
        mic_names = [dev_name(s) for s in self.mics()]
        if self.cfg["mic"] not in mic_names and mic_names:
            d = self.default_source()
            self.cfg["mic"] = d if d in mic_names else mic_names[0]
            changed = True
        if changed:
            self.save_config()

    def ensure_topology(self):
        with self.lock:
            self.refresh_cache()
            # Si el sink por defecto es uno nuestro, devolverlo a los audífonos
            self.ensure_sinks()
            self.refresh_cache()
            self.pick_defaults()
            if (self.default_sink() or "").startswith(PREFIX) and self.cfg["output"]:
                self.pactl("set-default-sink", self.cfg["output"])
            self.ensure_loops()
            self.refresh_cache()
            self.apply_all()
            self.route_all()

    def update_mode(self):
        # Solo añade o quita las conexiones del stream; las de los audífonos no se tocan (sin chasquidos)
        self.ensure_topology()

    def rebuild(self):
        for key in [k for k in self.modules if k.startswith("loop:")]:
            self.unload(key)
        time.sleep(0.2)
        self.ensure_topology()

    # ── volúmenes ──
    def input_by_name(self, name):
        for i in self.cache["inputs"]:
            if (i.get("properties") or {}).get("application.name") == name:
                return i.get("index")
        return None

    def apply_all(self):
        for ch in CHANNELS + ["mic"]:
            for side in SIDES:
                idx = self.input_by_name(self.loop_name(ch, side))
                if idx is None:
                    continue
                v, m = self.effective(ch, side)
                self.pactl("set-sink-input-volume", idx, f"{v}%")
                self.pactl("set-sink-input-mute", idx, "1" if m else "0")
        out = self.cfg["output"]
        if out:
            self.pactl("set-sink-volume", out, f"{clamp(self.cfg['vol']['master']['hp'])}%")
            self.pactl("set-sink-mute", out, "1" if self.cfg["mute"]["master"]["hp"] else "0")
        if self.cfg["mic"]:
            self.pactl("set-source-volume", self.cfg["mic"], f"{clamp(self.cfg['mic_gain'])}%")

    # ── enrutado de aplicaciones ──
    def is_ours(self, inp):
        name = (inp.get("properties") or {}).get("application.name") or ""
        return name.startswith(LOOP_TAG)

    def sink_name_by_index(self, idx):
        for s in self.cache["sinks"]:
            if s.get("index") == idx:
                return dev_name(s)
        return None

    def route_all(self, force=False):
        default_ch = self.cfg["default_channel"]
        alive = set()
        moved_any = False
        for inp in self.cache["inputs"]:
            if self.is_ours(inp):
                continue
            props = inp.get("properties") or {}
            idx = inp.get("index")
            alive.add(idx)
            if props.get("media.role") == "event":       # sonidos del sistema: directo
                continue
            key, _, binary = app_key(props)
            current = self.sink_name_by_index(inp.get("sink"))
            rule = self.rule_for(key, binary)

            # Si el usuario movió la app a otro canal desde KDE, lo aprendemos
            if (not force and idx in self.moved and current and current.startswith(PREFIX)
                    and current != self.moved[idx] and current != STREAM_SINK):
                learned = current[len(PREFIX):]
                if learned in CHANNELS:
                    self.cfg["rules"][key] = learned
                    self.save_config()
                    self.moved[idx] = current
                    log(f"aprendido: {key} → {learned}")
                    continue

            if idx in self.moved and not force:
                continue
            target = PREFIX + (rule or default_ch)
            if current != target:
                self.pactl("move-sink-input", idx, target)
                moved_any = True
            self.moved[idx] = target
        for idx in list(self.moved):
            if idx not in alive:
                del self.moved[idx]
        if moved_any:
            self.refresh_cache()

    # ── eventos de PipeWire ──
    def watch(self):
        while self.running:
            try:
                self.sub_proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, text=True)
                for line in self.sub_proc.stdout:
                    if not self.running:
                        break
                    if "sink-input" in line:
                        self.dirty.set()
                    elif ("'new'" in line or "'remove'" in line) and (" sink " in line or " source " in line):
                        self.topology_dirty.set()
                    elif "server" in line:
                        self.topology_dirty.set()
            except Exception as e:
                log("subscribe falló:", e)
            time.sleep(1)

    def worker(self):
        last_full = 0
        while self.running:
            time.sleep(0.25)
            try:
                if self.topology_dirty.is_set() or time.time() - last_full > 5:
                    self.topology_dirty.clear()
                    self.dirty.clear()
                    self.ensure_topology()
                    last_full = time.time()
                elif self.dirty.is_set():
                    self.dirty.clear()
                    with self.lock:
                        self.refresh_cache()
                        self.route_all()
                self.error = None
            except Exception as e:
                self.error = str(e)
                log("error:", e)

    def start(self):
        if not shutil.which("pactl"):
            self.error = "No se encontró 'pactl'. Este mezclador lo necesita (paquete pulseaudio-utils)."
            log(self.error)
            return
        test = self.pactl("-f", "json", "info")
        if test is None:
            self.error = "pactl no responde o no soporta JSON."
        self.cleanup_leftovers()
        self.ensure_topology()
        threading.Thread(target=self.watch, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()
        log("mezclador listo")

    def stop(self):
        self.running = False
        if self.sub_proc:
            self.sub_proc.terminate()
        # devolver las apps a los audífonos antes de quitar los canales
        try:
            self.refresh_cache()
            out = self.cfg["output"]
            for inp in self.cache["inputs"]:
                if not self.is_ours(inp) and out:
                    self.pactl("move-sink-input", inp.get("index"), out)
        except Exception:
            pass
        for key in list(self.modules):
            if key.startswith("loop:"):
                self.unload(key)
        for key in list(self.modules):
            self.unload(key)
        self.cleanup_leftovers()
        log("canales eliminados, audio devuelto a los audífonos")

    # ── estado para la interfaz ──
    def state(self):
        with self.lock:
            apps = {ch: [] for ch in CHANNELS + ["master", "mic"]}
            seen = set()
            for inp in self.cache["inputs"]:
                if self.is_ours(inp):
                    continue
                props = inp.get("properties") or {}
                key, name, _ = app_key(props)
                sink = self.sink_name_by_index(inp.get("sink")) or ""
                ch = sink[len(PREFIX):] if sink.startswith(PREFIX) else "master"
                if ch not in apps:
                    ch = "master"
                if (ch, key) in seen:
                    continue
                seen.add((ch, key))
                apps[ch].append({"key": key, "name": name})
            for out in self.cache["outputs"]:
                props = out.get("properties") or {}
                if (props.get("application.name") or "").startswith(LOOP_TAG):
                    continue
                key, name, _ = app_key(props)
                if ("mic", key) in seen or props.get("media.class") == "Stream/Input/Video":
                    continue
                seen.add(("mic", key))
                apps["mic"].append({"key": key, "name": name})
            return {
                "cfg": {k: self.cfg[k] for k in ("vol", "mute", "chatmix", "output", "mic",
                                                 "mic_gain", "default_channel", "stream_mode")},
                "apps": apps,
                "outputs": [{"name": dev_name(s), "desc": dev_desc(s)}
                            for s in self.hw_sinks()],
                "mics": [{"name": dev_name(s), "desc": dev_desc(s)}
                         for s in self.mics()],
                "error": self.error or (None if self.cfg["output"] in {dev_name(x) for x in self.hw_sinks()}
                                        else "No encuentro tus audífonos. Elige la salida abajo de MASTER."),
                "demo": False,
            }


# ─────────────────────────────────────────────────────────────────────────────
class DemoMixer(BaseMixer):
    """Modo demostración: no toca el audio, solo simula apps."""

    def __init__(self):
        super().__init__()
        self.fake = [
            ("discord", "Discord"), ("spotify", "Spotify"), ("google chrome", "Google Chrome"),
            ("cs2", "cs2"), ("steam", "Steam"),
        ]
        self.cfg["output"] = self.cfg["output"] or "razer_barracuda"
        self.cfg["mic"] = self.cfg["mic"] or "alias_pro"

    def save_config(self):
        pass

    def apply_all(self): pass
    def route_all(self, force=False): pass
    def rebuild(self): pass
    def start(self): log("modo demo")
    def stop(self): pass

    def state(self):
        apps = {ch: [] for ch in CHANNELS + ["master", "mic"]}
        for key, name in self.fake:
            ch = self.rule_for(key, key) or self.cfg["default_channel"]
            apps[ch].append({"key": key, "name": name})
        apps["master"].append({"key": "plasma", "name": "Sonidos del sistema"})
        apps["mic"].append({"key": "discord", "name": "Discord"})
        return {
            "cfg": {k: self.cfg[k] for k in ("vol", "mute", "chatmix", "output", "mic",
                                             "mic_gain", "default_channel", "stream_mode")},
            "apps": apps,
            "outputs": [{"name": "razer_barracuda", "desc": "Razer Barracuda X Estéreo analógico"},
                        {"name": "hdmi", "desc": "Navi 22 HDMI / DisplayPort"}],
            "mics": [{"name": "alias_pro", "desc": "SteelSeries Alias Pro"},
                     {"name": "barracuda_mic", "desc": "Razer Barracuda X Mono"}],
            "error": None,
            "demo": True,
        }


# ─────────────────────────────────────────────────────────────────────────────
MIXER: BaseMixer = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "mezclador.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self.send(200, MIXER.state())
        else:
            self.send(404, {"error": "no existe"})

    def do_POST(self):
        # Solo aceptar peticiones de la propia interfaz local
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "origen no permitido"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            p = self.path
            if p == "/api/vol":
                if body["chan"] in MIXER.cfg["vol"] and body["side"] in SIDES:
                    MIXER.set_vol(body["chan"], body["side"], body["value"])
            elif p == "/api/mute":
                if body["chan"] in MIXER.cfg["mute"] and body["side"] in SIDES:
                    MIXER.set_mute(body["chan"], body["side"], body["value"])
            elif p == "/api/chatmix":
                MIXER.set_chatmix(body["value"])
            elif p == "/api/micgain":
                MIXER.set_mic_gain(body["value"])
            elif p == "/api/route":
                MIXER.set_route(str(body["key"]).lower(), body["chan"])
            elif p == "/api/output":
                MIXER.set_output(body["name"])
            elif p == "/api/mic":
                MIXER.set_mic(body["name"])
            elif p == "/api/streammode":
                MIXER.set_stream_mode(body["value"])
            elif p == "/api/default":
                MIXER.set_default_channel(body["chan"])
            else:
                return self.send(404, {"error": "no existe"})
            self.send(200, {"ok": True})
        except (KeyError, ValueError, TypeError) as e:
            self.send(400, {"error": str(e)})


def main():
    global MIXER
    demo = "--demo" in sys.argv
    MIXER = DemoMixer() if demo else PipeWireMixer()
    MIXER.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(f"interfaz en http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    finally:
        MIXER.stop()


if __name__ == "__main__":
    main()
