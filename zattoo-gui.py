#!/usr/bin/env python3
"""Zattoo-DL GUI — moderne Web-Oberfläche für persönliche Zattoo-Aufnahmen.

Reine Python-Stdlib-Lösung. Teilt cookies.txt und output/ mit zattoo-dl.sh,
sodass beide Tools parallel nutzbar sind. Stream-URLs werden lazy beim Klick
auf den Download-Button generiert — niemals im Voraus für mehrere Aufnahmen.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import http.cookiejar
import http.server
import io
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

# --- Pfade / Konfiguration -------------------------------------------------

DOMAIN = "zattoo.com"
WORKDIR = Path.cwd()
COOKIE_FILE = WORKDIR / "cookies.txt"
COOKIE_LOCK = WORKDIR / "cookies.txt.lock"
CONFIG_FILE = WORKDIR / "config.json"
DEFAULT_OUTPUT_DIR = WORKDIR / "output"

# Aktuell aufgelöster Output-Pfad und Quelle ("config" | "cli" | "env" | "default").
# Wird in main() initialisiert und kann zur Laufzeit über /api/config geändert werden.
CONFIG: dict[str, Any] = {
    "output_dir": DEFAULT_OUTPUT_DIR,
    "output_dir_source": "default",
    "cli_output_dir": None,  # gemerkter --output-dir-Wert für Re-Resolution
}


def _cli_output_dir() -> str | None:
    return CONFIG.get("cli_output_dir")

SCRIPT_DIR = Path(__file__).resolve().parent
GUI_DIR = SCRIPT_DIR / "gui"

CACHE_DIR = Path.home() / ".cache" / "zattoo-dl"
PLAYLIST_CACHE = CACHE_DIR / "playlist.json"
CHANNELS_CACHE = CACHE_DIR / "channels.json"
THUMBS_DIR = CACHE_DIR / "thumbs"
PLAYLIST_TTL = 300  # Sekunden
CHANNELS_TTL = 24 * 60 * 60  # Sekunden — Logos ändern sich selten

# Statischer Channels-Endpoint (liefert cid → logo_token + Metadaten)
CHANNELS_URL = (
    f"https://{DOMAIN}/zapi/v4/cached/5619555c306306c0c028ccddb3ece844/channels"
)
LOGO_URL_TEMPLATE = "https://images.zattic.com/logos/{token}/black/84x48.png"

DEFAULT_PORT = 8765

ZATTOO_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/80.0.3987.87 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"https://{DOMAIN}/client",
    "Origin": f"https://{DOMAIN}",
}


# --- Hilfsfunktionen -------------------------------------------------------


@contextlib.contextmanager
def cookie_lock():
    """Verhindert Cookie-Race zwischen GUI und Bash-Skript."""
    COOKIE_LOCK.touch(exist_ok=True)
    fh = open(COOKIE_LOCK, "r+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    _current_output_dir().mkdir(parents=True, exist_ok=True)


def _current_output_dir() -> Path:
    return CONFIG["output_dir"]


def _normalize_path(value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (WORKDIR / p).resolve()
    else:
        p = p.resolve()
    return p


def _load_config() -> dict[str, Any]:
    """Liest config.json. Fehlt sie oder ist defekt, wird ein leeres Dict zurückgegeben."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️  config.json konnte nicht gelesen werden: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(cfg: dict[str, Any]) -> None:
    """Atomar nach config.json schreiben (temp + rename)."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="config.", suffix=".json.tmp", dir=str(CONFIG_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _resolve_output_dir(cli_value: str | None) -> tuple[Path, str]:
    """Reihenfolge: config.json > CLI-Flag > Env-Var > Default. Gibt (Pfad, Quelle) zurück."""
    cfg = _load_config()
    cfg_value = cfg.get("output_dir")
    if isinstance(cfg_value, str) and cfg_value.strip():
        return _normalize_path(cfg_value.strip()), "config"
    if cli_value:
        return _normalize_path(cli_value), "cli"
    env_value = os.environ.get("ZATTOO_DL_OUTPUT_DIR")
    if env_value and env_value.strip():
        return _normalize_path(env_value.strip()), "env"
    return DEFAULT_OUTPUT_DIR.resolve(), "default"


def _iso_to_epoch(iso: str) -> int:
    if not iso:
        return 0
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return int(_dt.datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return 0


def _clean_stream_url(url: str) -> str:
    """Repliziert exakt das Bash-`sed`-Cleanup aus zattoo-dl.sh:368.

    sed 's/enc//g' | sed -E 's#(/m)[^/]*\\.m3u8#\\1.m3u8#'
    """
    url_clean = url.replace("enc", "")
    url_clean = re.sub(r"(/m)[^/]*\.m3u8", r"\1.m3u8", url_clean)
    return url_clean


def _safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', " ", text).strip()


APP_FALLBACK = {
    # Fallback-App-Name, falls das URL-Schema auf macOS nicht registriert ist.
    # `open -a "<App>" <url>` startet die App mit der URL als Argument.
    "vlc": "VLC",
}


def _run_open(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, capture_output=True, timeout=8)
    except subprocess.TimeoutExpired:
        return False, "open hat länger als 8 Sekunden gebraucht"
    except Exception as e:  # noqa: BLE001
        return False, f"open konnte nicht ausgeführt werden: {e}"
    if result.returncode == 0:
        return True, ""
    err = result.stderr.decode("utf-8", errors="replace").strip()
    return False, err or f"open exit code {result.returncode}"


def _open_url(url: str) -> tuple[bool, str]:
    return _run_open(["open", url])


def _open_with_app(app_name: str, url: str) -> tuple[bool, str]:
    return _run_open(["open", "-a", app_name, url])


def _pick_folder_dialog(initial_dir: Path | None = None) -> tuple[str | None, str | None]:
    """Öffnet einen nativen Folder-Picker. Gibt (Pfad, Fehler) zurück.

    Bei Cancel: (None, None). Bei Fehler: (None, "...").
    Blockiert den Request-Thread, bis der Dialog geschlossen wird (bis zu 5 min).
    """
    plat = sys.platform
    initial = str(initial_dir) if initial_dir and initial_dir.exists() else None

    if plat == "darwin":
        prompt = "Ausgabe-Verzeichnis wählen"
        if initial:
            script = (
                f'POSIX path of (choose folder with prompt "{prompt}" '
                f'default location POSIX file "{initial}")'
            )
        else:
            script = f'POSIX path of (choose folder with prompt "{prompt}")'
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return None, "Dialog hat länger als 5 Minuten gebraucht"
        except FileNotFoundError:
            return None, "osascript nicht gefunden"
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace").strip().rstrip("/"), None
        err = result.stderr.decode("utf-8", "replace").strip()
        if "-128" in err or "User canceled" in err:
            return None, None  # Cancel ist kein Fehler
        return None, err or "Dialog konnte nicht geöffnet werden"

    if plat.startswith("linux"):
        for cmd in (
            ["zenity", "--file-selection", "--directory", "--title=Ausgabe-Verzeichnis wählen"]
            + (["--filename", initial + "/"] if initial else []),
            ["kdialog", "--getexistingdirectory", initial or os.path.expanduser("~")],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=300)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace").strip(), None
            if r.returncode == 1:  # Cancel bei zenity/kdialog
                return None, None
        return None, "Weder zenity noch kdialog verfügbar — bitte Pfad manuell eintragen"

    if plat == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Ausgabe-Verzeichnis wählen'; "
            + (f"$d.SelectedPath = '{initial}'; " if initial else "")
            + "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return None, f"PowerShell-Dialog fehlgeschlagen: {e}"
        path = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode == 0 and path:
            return path, None
        if r.returncode == 0:
            return None, None  # Cancel
        return None, r.stderr.decode("utf-8", "replace").strip() or "PowerShell-Fehler"

    return None, f"Plattform {plat} wird vom Folder-Picker nicht unterstützt"


# --- HLS-VOD-Preprocessing (für VLC-Start am Anfang) ----------------------
# Zattoos HLS-Playlists kommen ohne #EXT-X-ENDLIST, damit DVR/Live-Streaming
# möglich ist. VLC interpretiert das als Live und springt ans Ende. Wir holen
# die Playlist server-seitig, schreiben sie um (VOD + ENDLIST + absolute
# Segment-URLs) und legen sie als lokale Datei ab. VLC liest die Datei,
# erkennt VOD und startet vom Anfang. Segmente werden weiterhin direkt vom
# Zattoo-CDN geladen — kein Datendurchsatz durch unseren Server.


def _fetch_hls_text(client: "ZattooClient", url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": ZATTOO_HEADERS["User-Agent"]}
    )
    with client.opener.open(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _pick_best_variant(master_content: str, base_url: str) -> str | None:
    """Wählt aus einer Master-Playlist die Variant-URL mit höchster BANDWIDTH."""
    best_bw = -1
    best_url: str | None = None
    pending_bw: int | None = None
    for raw in master_content.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bw = int(m.group(1)) if m else 0
            continue
        if pending_bw is not None and line and not line.startswith("#"):
            if pending_bw > best_bw:
                best_bw = pending_bw
                best_url = urllib.parse.urljoin(base_url, line)
            pending_bw = None
    return best_url


_URI_ATTR_RE = re.compile(r'(URI=")([^"]*)(")')


def _absolutize_uri_attrs(tag_line: str, base_url: str) -> str:
    """In HLS-Tags wie #EXT-X-MAP, #EXT-X-KEY, #EXT-X-MEDIA: URI="..." absolut machen."""
    def _repl(m: re.Match) -> str:
        uri = m.group(2)
        if uri.startswith(("http://", "https://")):
            return m.group(0)
        return f'{m.group(1)}{urllib.parse.urljoin(base_url, uri)}{m.group(3)}'
    return _URI_ATTR_RE.sub(_repl, tag_line)


def _rewrite_variant_for_vod(content: str, base_url: str) -> str:
    """VOD-Type + ENDLIST sicherstellen, alle URLs absolut machen.

    Die Datei wird lokal gespeichert und von VLC als File-URL geladen — wenn
    nur Segment-URLs absolut wären und in HLS-Tags weiterhin relative URIs
    stehen (#EXT-X-MAP für fMP4-Init, #EXT-X-KEY für Crypto-Keys), versucht
    VLC sie relativ zum lokalen Dateipfad aufzulösen und scheitert.
    """
    has_endlist = any(
        l.strip() == "#EXT-X-ENDLIST" for l in content.splitlines()
    )

    out: list[str] = []
    inserted_vod = False
    for raw in content.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()

        # bestehende PLAYLIST-TYPE-Zeilen rauswerfen — wir setzen unsere
        if stripped.startswith("#EXT-X-PLAYLIST-TYPE:"):
            continue

        # Tags mit URI="..."-Attribut absolut machen (#EXT-X-MAP, #EXT-X-KEY, …)
        if stripped.startswith("#") and 'URI="' in stripped:
            out.append(_absolutize_uri_attrs(line, base_url))
            continue

        # Segment-URLs absolut machen
        if stripped and not stripped.startswith("#"):
            out.append(urllib.parse.urljoin(base_url, stripped))
            continue

        out.append(line)

        # VOD direkt nach #EXTM3U einsetzen
        if not inserted_vod and stripped == "#EXTM3U":
            out.append("#EXT-X-PLAYLIST-TYPE:VOD")
            inserted_vod = True

    if not inserted_vod:
        out.insert(0, "#EXT-X-PLAYLIST-TYPE:VOD")

    if not has_endlist:
        out.append("#EXT-X-ENDLIST")

    return "\n".join(out) + "\n"


def _collect_master_uris(master_content: str, base_url: str) -> list[tuple[str, str]]:
    """Liefert (kind, absolute_url)-Tupel für alle referenzierten Sub-Playlists.
    Wählt die beste Video-Variant (höchste BANDWIDTH) + ALLE Audio- und
    Subtitle-Tracks (#EXT-X-MEDIA). I-Frame-Streams werden ignoriert."""
    refs: list[tuple[str, str]] = []

    # Audio- und Subtitle-Tracks aus #EXT-X-MEDIA-Zeilen
    for raw in master_content.splitlines():
        line = raw.strip()
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        type_match = re.search(r"TYPE=(\w+)", line)
        uri_match = re.search(r'URI="([^"]*)"', line)
        if not (type_match and uri_match):
            continue
        kind = type_match.group(1).lower()
        if kind not in ("audio", "subtitles"):
            continue
        abs_url = urllib.parse.urljoin(base_url, uri_match.group(1))
        refs.append((kind, abs_url))

    # Beste Video-Variant
    best_video = _pick_best_variant(master_content, base_url)
    if best_video:
        refs.append(("video", best_video))

    return refs


def _rewrite_master_for_local(
    master_content: str,
    base_url: str,
    local_map: dict[str, str],
) -> str:
    """Schreibt den Master so um, dass alle URIs auf lokale Dateien zeigen.
    Dropped Varianten, die wir nicht heruntergeladen haben (z.B. niedrigere
    Bitraten und I-Frame-Streams)."""
    out: list[str] = []
    pending_stream_inf: str | None = None

    for raw in master_content.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()

        # I-Frame-Streams komplett rauswerfen — VLC braucht sie nicht
        if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            continue

        # #EXT-X-MEDIA: URI auf lokale Datei umbiegen oder Zeile droppen
        if stripped.startswith("#EXT-X-MEDIA:"):
            uri_match = re.search(r'URI="([^"]*)"', stripped)
            if uri_match:
                abs_url = urllib.parse.urljoin(base_url, uri_match.group(1))
                if abs_url in local_map:
                    out.append(re.sub(
                        r'URI="[^"]*"',
                        f'URI="{local_map[abs_url]}"',
                        line, count=1,
                    ))
                # else: Track ohne lokale Datei → droppen
            else:
                # Keine URI im MEDIA-Tag (z.B. CLOSED-CAPTIONS) — beibehalten
                out.append(line)
            continue

        # #EXT-X-STREAM-INF — buffern bis zur URI-Zeile
        if stripped.startswith("#EXT-X-STREAM-INF"):
            pending_stream_inf = line
            continue

        # URI-Zeile direkt nach STREAM-INF
        if pending_stream_inf and stripped and not stripped.startswith("#"):
            abs_url = urllib.parse.urljoin(base_url, stripped)
            if abs_url in local_map:
                out.append(pending_stream_inf)
                out.append(local_map[abs_url])
            # else: nicht in unserer Map → STREAM-INF + URI beide droppen
            pending_stream_inf = None
            continue

        # Alle anderen Zeilen (#EXTM3U, #EXT-X-VERSION, etc.) durchreichen
        out.append(line)

    return "\n".join(out) + "\n"


def _write_vod_debug_copy(content: str) -> None:
    """Debug-Kopie unter festem Namen, damit man bei Fehlern leicht reinschauen kann."""
    try:
        (Path(tempfile.gettempdir()) / "zattoo-vlc-latest.m3u8").write_text(
            content, encoding="utf-8"
        )
    except OSError:
        pass


def _prepare_vod_playlist(client: "ZattooClient", stream_url: str) -> Path | None:
    """Holt die HLS-Playlist von Zattoo, schreibt sie auf VOD um, speichert
    sie lokal. Bei einem Master-Playlist mit mehreren Tracks (Video + Audio +
    Subtitles) werden alle Sub-Playlists einzeln heruntergeladen und in ein
    Verzeichnis abgelegt — der zurückgegebene Master verweist relativ auf
    die lokalen Dateien. So sieht VLC alle Audiospuren + Untertitel."""
    try:
        master = _fetch_hls_text(client, stream_url)
    except Exception:  # noqa: BLE001
        return None

    tmpdir = Path(tempfile.gettempdir())

    # Fall 1: direkte Variant-Playlist (kein Master) → eine einzelne Datei
    if "#EXT-X-STREAM-INF" not in master:
        rewritten = _rewrite_variant_for_vod(master, stream_url)
        path = tmpdir / f"zattoo-{uuid.uuid4().hex[:8]}.m3u8"
        try:
            path.write_text(rewritten, encoding="utf-8")
        except OSError:
            return None
        _write_vod_debug_copy(rewritten)
        preview = "\n".join(rewritten.splitlines()[:8])
        print(f"[vlc] vorbereitet (variant): {path}\n{preview}\n[…]",
              file=sys.stderr, flush=True)
        return path

    # Fall 2: Master-Playlist → Verzeichnis mit master + allen Sub-Playlists
    refs = _collect_master_uris(master, stream_url)
    if not refs:
        return None

    work_dir = tmpdir / f"zattoo-{uuid.uuid4().hex[:8]}"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    local_map: dict[str, str] = {}
    counter = {"video": 0, "audio": 0, "subtitles": 0}

    for kind, abs_url in refs:
        try:
            content = _fetch_hls_text(client, abs_url)
        except Exception:  # noqa: BLE001
            continue
        rewritten = _rewrite_variant_for_vod(content, abs_url)
        local_name = f"{kind}-{counter[kind]}.m3u8"
        counter[kind] += 1
        try:
            (work_dir / local_name).write_text(rewritten, encoding="utf-8")
        except OSError:
            continue
        local_map[abs_url] = local_name

    if not local_map:
        return None

    rewritten_master = _rewrite_master_for_local(master, stream_url, local_map)
    master_path = work_dir / "master.m3u8"
    try:
        master_path.write_text(rewritten_master, encoding="utf-8")
    except OSError:
        return None

    _write_vod_debug_copy(rewritten_master)
    print(f"[vlc] vorbereitet (master): {master_path} "
          f"({counter['video']} video, {counter['audio']} audio, "
          f"{counter['subtitles']} subs)", file=sys.stderr, flush=True)
    return master_path


# --- Lokale Downloads via ffmpeg / yt-dlp ---------------------------------
# Serieller Job-Worker: nur ein Download zur Zeit, der Rest wartet in der
# Queue. Frontend pollt /api/download/<id>/progress und zeigt Bar/ETA an.


class DownloadJob:
    __slots__ = (
        "id", "recording_id", "stream_url", "filename", "bilingual",
        "state", "percent", "speed", "eta", "error",
        "process", "cancelled",
        "queued_at", "started_at", "finished_at",
        "total_seconds", "current_seconds",
    )

    def __init__(
        self,
        job_id: str,
        recording_id: str,
        stream_url: str,
        filename: str,
        bilingual: bool,
    ) -> None:
        self.id = job_id
        self.recording_id = recording_id
        self.stream_url = stream_url
        self.filename = filename
        self.bilingual = bilingual
        self.state = "queued"  # queued | running | done | error | cancelled
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.process: subprocess.Popen | None = None
        self.cancelled = threading.Event()
        self.queued_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.total_seconds = 0.0
        self.current_seconds = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "recording_id": self.recording_id,
            "state": self.state,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "filename": self.filename,
            "bilingual": self.bilingual,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


DOWNLOAD_QUEUE: "queue.Queue[str | None]" = queue.Queue()
DOWNLOAD_JOBS: dict[str, DownloadJob] = {}
DOWNLOAD_LOCK = threading.Lock()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _ensure_worker(client: "ZattooClient") -> None:
    """Startet den Download-Worker-Thread genau einmal."""
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        t = threading.Thread(
            target=_download_worker,
            args=(client,),
            daemon=True,
            name="zattoo-dl-worker",
        )
        t.start()
        _WORKER_STARTED = True


def _download_worker(client: "ZattooClient") -> None:
    """Verarbeitet die Queue seriell — nur ein Download zur Zeit."""
    while True:
        job_id = DOWNLOAD_QUEUE.get()
        if job_id is None:
            break
        with DOWNLOAD_LOCK:
            job = DOWNLOAD_JOBS.get(job_id)
        if job is None:
            continue
        if job.cancelled.is_set():
            if job.state == "queued":
                job.state = "cancelled"
                job.finished_at = time.time()
            continue
        try:
            _run_local_download(client, job)
        except Exception as e:  # noqa: BLE001
            job.state = "error"
            job.error = f"unerwarteter Fehler: {e}"
            job.finished_at = time.time()


def _hls_total_seconds(client: "ZattooClient", stream_url: str) -> float:
    """Summiert die EXTINF-Werte einer HLS-Variant und liefert die Gesamtdauer."""
    try:
        master = _fetch_hls_text(client, stream_url)
    except Exception:  # noqa: BLE001
        return 0.0

    if "#EXT-X-STREAM-INF" in master:
        variant_url = _pick_best_variant(master, stream_url)
        if not variant_url:
            return 0.0
        try:
            content = _fetch_hls_text(client, variant_url)
        except Exception:  # noqa: BLE001
            return 0.0
    else:
        content = master

    total = 0.0
    for line in content.splitlines():
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+)", line)
            if m:
                try:
                    total += float(m.group(1))
                except ValueError:
                    pass
    return total


def _format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def _format_eta(seconds: float) -> str:
    if seconds <= 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _safe_run_terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _run_local_download(client: "ZattooClient", job: DownloadJob) -> None:
    """Führt den eigentlichen Download über ffmpeg oder yt-dlp aus."""
    job.state = "running"
    job.started_at = time.time()

    # Gesamtdauer einmal aus der HLS-Playlist berechnen — für Prozent-Anzeige
    job.total_seconds = _hls_total_seconds(client, job.stream_url)

    output_dir = _current_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{job.filename}.mp4"

    if job.bilingual:
        if not shutil.which("yt-dlp"):
            job.state = "error"
            job.error = "yt-dlp ist nicht installiert"
            job.finished_at = time.time()
            return
        _run_yt_dlp(job, output_path)
    else:
        if not shutil.which("ffmpeg"):
            job.state = "error"
            job.error = "ffmpeg ist nicht installiert"
            job.finished_at = time.time()
            return
        _run_ffmpeg(job, output_path)


def _run_ffmpeg(job: DownloadJob, output_path: Path) -> None:
    """ffmpeg-Aufruf analog zu zattoo-dl.sh:159, Progress über `-progress pipe:1`."""
    cmd = [
        "ffmpeg", "-y",
        "-i", job.stream_url,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy",
        "-progress", "pipe:1",
        "-loglevel", "error",
        str(output_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        job.state = "error"
        job.error = "ffmpeg konnte nicht gestartet werden"
        job.finished_at = time.time()
        return

    job.process = proc

    speed_x = 0.0  # zuletzt gesehener Speed-Multiplier (z.B. 7.83)

    def _update_eta() -> None:
        """ETA = (Restdauer des Videos) / Speed-Multiplier."""
        if job.total_seconds <= 0:
            return
        remaining_playback = max(0.0, job.total_seconds - job.current_seconds)
        if speed_x > 0:
            job.eta = _format_eta(remaining_playback / speed_x)
        else:
            # Noch kein gültiger Speed-Wert — temporär die rohe Restdauer zeigen
            job.eta = _format_eta(remaining_playback)

    while True:
        if job.cancelled.is_set():
            _safe_run_terminate(proc)
            job.state = "cancelled"
            job.finished_at = time.time()
            job.process = None
            return

        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            break
        line = line.strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key == "out_time_us":
            try:
                us = int(val)
                job.current_seconds = us / 1_000_000
                if job.total_seconds > 0:
                    job.percent = min(99.9, (job.current_seconds / job.total_seconds) * 100)
                _update_eta()
            except ValueError:
                pass
        elif key == "speed":
            v = val.strip()
            # ffmpeg liefert "5.2x" = Verarbeitung relativ zur Echtzeit.
            # Bei -c copy ist das primär Netzwerk-/Disk-IO-bound, kein Encoding.
            job.speed = "" if v in ("N/A", "") else v
            m = re.match(r"([\d.]+)x", v)
            speed_x = float(m.group(1)) if m else 0.0
            _update_eta()
        elif key == "progress" and val == "end":
            break

    rc = proc.wait()
    job.process = None

    if job.cancelled.is_set():
        job.state = "cancelled"
    elif rc == 0:
        job.percent = 100.0
        job.eta = ""
        job.state = "done"
    else:
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().strip()
            except Exception:  # noqa: BLE001
                pass
        job.state = "error"
        job.error = err or f"ffmpeg exit code {rc}"
    job.finished_at = time.time()


def _run_yt_dlp(job: DownloadJob, output_path: Path) -> None:
    """yt-dlp-Aufruf analog zu zattoo-dl.sh:157 mit bilingualem Audio + Subs."""
    progress_template = (
        "DLPROGRESS:%(progress.downloaded_bytes)s/"
        "%(progress.total_bytes_estimate)s/"
        "%(progress.speed)s/"
        "%(progress.eta)s"
    )
    cmd = [
        "yt-dlp",
        "--newline",
        "--no-warnings",
        "--audio-multistreams",
        "-f", "bv+mergeall[vcodec=none]",
        "--sub-langs", "en.*,de.*,fr.*,es.*",
        "--embed-subs",
        "--merge-output-format", "mp4",
        "--progress-template", progress_template,
        job.stream_url,
        "-o", str(output_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        job.state = "error"
        job.error = "yt-dlp konnte nicht gestartet werden"
        job.finished_at = time.time()
        return

    job.process = proc

    while True:
        if job.cancelled.is_set():
            _safe_run_terminate(proc)
            job.state = "cancelled"
            job.finished_at = time.time()
            job.process = None
            return

        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            break
        line = line.strip()
        if not line.startswith("DLPROGRESS:"):
            continue
        parts = line[len("DLPROGRESS:"):].split("/")
        if len(parts) < 4:
            continue
        dl, total, speed, eta = parts[0], parts[1], parts[2], parts[3]
        if dl and total and total not in ("NA", "None"):
            try:
                pct = (float(dl) / float(total)) * 100
                job.percent = min(99.9, pct)
            except (ValueError, ZeroDivisionError):
                pass
        if speed and speed not in ("NA", "None"):
            try:
                job.speed = _format_speed(float(speed))
            except ValueError:
                pass
        if eta and eta not in ("NA", "None"):
            try:
                job.eta = _format_eta(float(eta))
            except ValueError:
                pass

    rc = proc.wait()
    job.process = None

    if job.cancelled.is_set():
        job.state = "cancelled"
    elif rc == 0:
        job.percent = 100.0
        job.eta = ""
        job.state = "done"
    else:
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().strip()
            except Exception:  # noqa: BLE001
                pass
        job.state = "error"
        job.error = err or f"yt-dlp exit code {rc}"
    job.finished_at = time.time()


def _enqueue_local_download(
    client: "ZattooClient",
    recording_id: str,
    stream_url: str,
    filename: str,
    bilingual: bool,
) -> DownloadJob:
    job_id = uuid.uuid4().hex[:8]
    job = DownloadJob(job_id, recording_id, stream_url, filename, bilingual)
    with DOWNLOAD_LOCK:
        DOWNLOAD_JOBS[job_id] = job
    DOWNLOAD_QUEUE.put(job_id)
    _ensure_worker(client)
    return job


def _queue_position(job: DownloadJob) -> int:
    """0 = laufender Job; ≥1 = wartend mit dieser Position vor sich."""
    if job.state in ("done", "error", "cancelled"):
        return 0
    with DOWNLOAD_LOCK:
        if job.state == "running":
            return 0
        ahead = 0
        for j in DOWNLOAD_JOBS.values():
            if j is job:
                continue
            if j.state == "running":
                ahead += 1
            elif j.state == "queued" and j.queued_at < job.queued_at:
                ahead += 1
        return ahead


# --- Zattoo-Client ---------------------------------------------------------


class ZattooError(RuntimeError):
    pass


class ZattooClient:
    """Schmaler Wrapper um die Zattoo-API. Stdlib only."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.MozillaCookieJar(str(COOKIE_FILE))
        if COOKIE_FILE.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    # -- Low-level --

    def _request(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        extra_headers: dict | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        body = None
        headers = dict(ZATTOO_HEADERS)
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            payload = resp.read()
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ZattooError(f"Ungültige JSON-Antwort von {url}") from exc

    # -- Auth --

    def is_logged_in(self) -> bool:
        if not COOKIE_FILE.exists():
            return False
        try:
            r = self._request("GET", f"https://{DOMAIN}/zapi/channels/favorites")
            return bool(r.get("success"))
        except (urllib.error.URLError, ZattooError, OSError):
            return False

    def login(self, username: str, password: str) -> None:
        with cookie_lock():
            if COOKIE_FILE.exists():
                COOKIE_FILE.unlink()
            self.jar.clear()

            token_data = self._request("GET", f"https://{DOMAIN}/token.json")
            app_token = token_data.get("session_token")
            if not app_token:
                raise ZattooError("Kein session_token erhalten")

            uid = str(uuid.uuid4())
            ck = http.cookiejar.Cookie(
                version=0, name="uuid", value=uid,
                port=None, port_specified=False,
                domain=DOMAIN, domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True,
                secure=True, expires=None,
                discard=False, comment=None, comment_url=None,
                rest={}, rfc2109=False,
            )
            self.jar.set_cookie(ck)

            session_info = self._request(
                "POST",
                f"https://{DOMAIN}/zapi/v3/session/hello",
                data={
                    "uuid": uid,
                    "lang": "en",
                    "format": "json",
                    "app_version": "3.2120.1",
                    "client_app_token": app_token,
                },
            )
            if not session_info.get("active"):
                raise ZattooError("Session Hello fehlgeschlagen")

            login_resp = self._request(
                "POST",
                f"https://{DOMAIN}/zapi/v3/account/login",
                data={
                    "login": username,
                    "password": password,
                    "remember": "true",
                    "format": "json",
                },
            )
            if not login_resp.get("active"):
                raise ZattooError("Login fehlgeschlagen — Zugangsdaten prüfen")

            self.jar.save(ignore_discard=True, ignore_expires=True)

    # -- Recordings --

    def fetch_recordings(self, *, force: bool = False) -> dict[str, Any]:
        """Liefert die Playlist (gecached). API-Aufruf nur bei Cache-Miss/force."""
        if not force and PLAYLIST_CACHE.exists():
            age = time.time() - PLAYLIST_CACHE.stat().st_mtime
            if age < PLAYLIST_TTL:
                return json.loads(PLAYLIST_CACHE.read_text("utf-8"))

        raw = self._request("GET", f"https://{DOMAIN}/zapi/v2/playlist")
        now = int(time.time())
        recordings = raw.get("recordings", []) or []
        recordings = [r for r in recordings if _iso_to_epoch(r.get("end", "")) <= now]
        recordings.sort(key=lambda r: _iso_to_epoch(r.get("start", "")), reverse=True)
        raw["recordings"] = recordings
        PLAYLIST_CACHE.write_text(json.dumps(raw), "utf-8")
        return raw

    # -- Channels / Logos --

    def fetch_channels(self, *, force: bool = False) -> dict[str, str]:
        """Liefert eine cid → logo_token Map (24h gecached).

        Bei Fehler (z.B. nicht eingeloggt, URL-Schema geändert) wird ein
        leerer Dict zurückgegeben — die GUI rendert dann CID-Text statt Logo.
        """
        if not force and CHANNELS_CACHE.exists():
            age = time.time() - CHANNELS_CACHE.stat().st_mtime
            if age < CHANNELS_TTL:
                try:
                    return json.loads(CHANNELS_CACHE.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass

        try:
            raw = self._request("GET", CHANNELS_URL)
        except (urllib.error.URLError, ZattooError, OSError):
            return {}

        # Echte API-Struktur: { "channels": [ { "cid": "rtl",
        # "qualities": [ { "level": "hd", "logo_token": "..." },
        # { "level": "sd", "logo_token": "..." } ], ... }, ... ] }
        # Wir nehmen bevorzugt das HD-Logo, sonst SD, sonst das erste.
        channels: list[dict[str, Any]] = []
        flat = raw.get("channels")
        if isinstance(flat, list):
            channels = flat
        # Fallback: manche Schemata haben channels in groups verschachtelt
        if not channels:
            groups = raw.get("channel_groups") or raw.get("groups")
            if isinstance(groups, list):
                for g in groups:
                    if isinstance(g, dict):
                        channels.extend(g.get("channels") or [])

        def pick_logo_token(ch: dict[str, Any]) -> str | None:
            # 1. logo_token direkt auf Channel-Ebene (manche API-Versionen)
            t = ch.get("logo_token") or ch.get("logo_id")
            if isinstance(t, str) and t:
                return t
            # 2. qualities[0].logo_token — bevorzugt 'hd', sonst erstes
            qualities = ch.get("qualities")
            if isinstance(qualities, list) and qualities:
                hd = next(
                    (q for q in qualities
                     if isinstance(q, dict) and q.get("level") == "hd"),
                    None,
                )
                pick = hd if hd else qualities[0]
                if isinstance(pick, dict):
                    t = pick.get("logo_token") or pick.get("logo_id")
                    if isinstance(t, str) and t:
                        return t
            return None

        cid_to_token: dict[str, str] = {}
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            cid = ch.get("cid")
            if not isinstance(cid, str) or not cid:
                continue
            token = pick_logo_token(ch)
            if token:
                cid_to_token[cid] = token

        if cid_to_token:
            try:
                CHANNELS_CACHE.write_text(json.dumps(cid_to_token), "utf-8")
            except OSError:
                pass
        return cid_to_token

    # -- Stream-URL (lazy, on demand) --

    def stream_url(self, recording_id: str) -> str:
        resp = self._request(
            "POST",
            f"https://{DOMAIN}/zapi/watch/recording/{recording_id}",
            data={
                "with_schedule": "false",
                "stream_type": "hls7_fairplay",
                "https_watch_urls": "true",
                "sdh_subtitles": "true",
            },
        )
        url = (resp.get("stream") or {}).get("url")
        if not url:
            raise ZattooError("Keine Stream-URL erhalten")
        return _clean_stream_url(url)


# --- Thumbnail-Helpers -----------------------------------------------------


def extract_thumbnail_url(rec: dict[str, Any]) -> str | None:
    """Versucht eine Thumbnail-URL aus den Recording-Feldern zu konstruieren."""
    for key in ("image_url", "image", "preview_image", "background_url"):
        v = rec.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v

    token = rec.get("image_token") or rec.get("image_id")
    if isinstance(token, str) and token:
        return f"https://images.zattic.com/cms/{token}/format_480x270.jpg"

    cid = rec.get("cid")
    if isinstance(cid, str) and cid:
        return f"https://images.zattic.com/logos/{cid}/header_white.png"
    return None


# --- HTTP-Server -----------------------------------------------------------


class GUIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ZattooDL-GUI/1.0"
    client: ZattooClient = None  # type: ignore[assignment]

    URL_SCHEMES = {
        "downie": "downie://XUOpenLink?url={url}&title={title}",
        "vlc": "vlc-x-callback://x-callback-url/stream?url={url}",
    }

    # -- Helpers --

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- Routing --

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_static(GUI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/static/app.js":
            self._send_static(GUI_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/static/styles.css":
            self._send_static(GUI_DIR / "styles.css", "text/css; charset=utf-8")
            return

        if path == "/api/session":
            self._send_json(200, {"logged_in": self.client.is_logged_in()})
            return
        if path == "/api/config":
            self._handle_config_get()
            return
        if path == "/api/recordings":
            self._handle_recordings(force=False)
            return
        if path == "/api/jobs":
            self._handle_jobs_list()
            return
        if path == "/api/thumbnail":
            self._handle_thumbnail(urllib.parse.parse_qs(parsed.query))
            return

        m = re.match(r"^/api/download/([a-f0-9]+)/progress$", path)
        if m:
            self._handle_progress(m.group(1))
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/config":
            self._handle_config_post()
            return
        if path == "/api/pick-folder":
            self._handle_pick_folder()
            return
        if path == "/api/recordings/refresh":
            self._handle_recordings(force=True)
            return
        if path == "/api/download":
            self._handle_download()
            return

        m = re.match(r"^/api/download/([a-f0-9]+)/cancel$", path)
        if m:
            self._handle_cancel(m.group(1))
            return

        self.send_error(404)

    # -- Endpoints --

    def _handle_login(self) -> None:
        body = self._read_json_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            self._send_json(400, {"error": "Benutzername und Passwort erforderlich"})
            return
        try:
            self.client.login(username, password)
            self._send_json(200, {"ok": True})
        except ZattooError as e:
            self._send_json(401, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"Login-Fehler: {e}"})

    def _handle_config_get(self) -> None:
        self._send_json(200, {
            "output_dir": str(CONFIG["output_dir"]),
            "default_output_dir": str(DEFAULT_OUTPUT_DIR.resolve()),
            "source": CONFIG["output_dir_source"],
        })

    def _handle_config_post(self) -> None:
        body = self._read_json_body()
        raw = body.get("output_dir")

        cfg = _load_config()

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # Override entfernen → fällt zurück auf CLI/Env/Default.
            cfg.pop("output_dir", None)
            try:
                _save_config(cfg)
            except OSError as e:
                self._send_json(500, {"error": f"config.json konnte nicht geschrieben werden: {e}"})
                return
            new_dir, source = _resolve_output_dir(_cli_output_dir())
        else:
            if not isinstance(raw, str):
                self._send_json(400, {"error": "output_dir muss ein String sein"})
                return
            try:
                target = _normalize_path(raw.strip())
                target.mkdir(parents=True, exist_ok=True)
                if not os.access(target, os.W_OK):
                    raise PermissionError(f"Kein Schreibzugriff auf {target}")
            except (OSError, ValueError) as e:
                self._send_json(400, {"error": f"Pfad ungültig: {e}"})
                return
            cfg["output_dir"] = str(target)
            try:
                _save_config(cfg)
            except OSError as e:
                self._send_json(500, {"error": f"config.json konnte nicht geschrieben werden: {e}"})
                return
            new_dir, source = target, "config"

        CONFIG["output_dir"] = new_dir
        CONFIG["output_dir_source"] = source
        self._send_json(200, {
            "output_dir": str(new_dir),
            "default_output_dir": str(DEFAULT_OUTPUT_DIR.resolve()),
            "source": source,
        })

    def _handle_pick_folder(self) -> None:
        body = self._read_json_body()
        raw_initial = body.get("initial") if isinstance(body, dict) else None
        initial = None
        if isinstance(raw_initial, str) and raw_initial.strip():
            try:
                initial = _normalize_path(raw_initial.strip())
            except (OSError, ValueError):
                initial = None
        if initial is None:
            initial = _current_output_dir()

        path, err = _pick_folder_dialog(initial)
        if err:
            self._send_json(503, {"error": err})
            return
        if path is None:
            self._send_json(200, {"cancelled": True})
            return
        self._send_json(200, {"path": path})

    def _handle_recordings(self, *, force: bool) -> None:
        if not self.client.is_logged_in():
            self._send_json(401, {"error": "not_logged_in"})
            return
        try:
            data = self.client.fetch_recordings(force=force)
        except ZattooError as e:
            self._send_json(500, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"Recordings-Fehler: {e}"})
            return

        # Channel-Logos einmalig holen (24h-Cache) und auf jede Aufnahme anwenden
        try:
            channels_map = self.client.fetch_channels()
        except Exception:  # noqa: BLE001
            channels_map = {}

        items = []
        for r in data.get("recordings", []):
            rid = r.get("id") or r.get("program_id") or ""
            if not rid:
                continue
            thumb = extract_thumbnail_url(r)
            cid = r.get("cid") or ""
            logo_url = None
            if cid:
                token = channels_map.get(cid)
                if token:
                    logo_url = LOGO_URL_TEMPLATE.format(token=token)
            items.append({
                "id": str(rid),
                "cid": cid,
                "title": r.get("title") or "",
                "episode": r.get("episode_title") or "",
                "start": r.get("start") or "",
                "end": r.get("end") or "",
                "thumbnail": thumb,
                "logo_url": logo_url,
            })
        self._send_json(200, {"recordings": items, "count": len(items)})

    def _handle_thumbnail(self, qs: dict[str, list[str]]) -> None:
        rid = (qs.get("id") or [""])[0]
        url = (qs.get("url") or [""])[0]
        if not rid:
            self.send_error(400)
            return

        cache_path = THUMBS_DIR / f"{rid}.bin"
        if not cache_path.exists():
            if not url or not url.startswith(("http://", "https://")):
                self.send_error(404)
                return
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": ZATTOO_HEADERS["User-Agent"]}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cache_path.write_bytes(resp.read())
            except Exception:  # noqa: BLE001
                self.send_error(404)
                return

        data = cache_path.read_bytes()
        ctype = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _handle_download(self) -> None:
        body = self._read_json_body()
        rid = (body.get("recording_id") or "").strip()
        target = (body.get("target") or "").strip().lower()
        if not rid:
            self._send_json(400, {"error": "recording_id fehlt"})
            return
        valid_targets = ("downie", "vlc", "metube", "local")
        if target not in valid_targets:
            self._send_json(400, {"error": f"target muss eines von {valid_targets} sein"})
            return

        # Lazy: erst JETZT die Stream-URL holen
        try:
            stream = self.client.stream_url(rid)
        except ZattooError as e:
            self._send_json(502, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"Stream-URL-Fehler: {e}"})
            return

        # Lokaler Download via ffmpeg/yt-dlp — geht in die serielle Queue
        if target == "local":
            bilingual = bool(body.get("bilingual", False))
            filename = (body.get("filename") or "").strip() or f"recording-{rid}"
            job = _enqueue_local_download(
                self.client, rid, stream, filename, bilingual,
            )
            self._send_json(200, {
                "ok": True,
                "target": "local",
                "job_id": job.id,
                "queue_position": _queue_position(job),
            })
            return

        # VLC: erst eine VOD-umgeschriebene Playlist als lokale Datei vorbereiten,
        # damit VLC am Anfang einsetzt statt am Live-Edge. Bei Fehlschlag fällt der
        # Code auf den normalen URL-Schema/open-a-Fallback unten durch.
        if target == "vlc":
            vod_path = _prepare_vod_playlist(self.client, stream)
            if vod_path is not None:
                ok_vod, err_vod = _open_with_app("VLC", str(vod_path))
                if ok_vod:
                    self._send_json(200, {
                        "ok": True, "target": "vlc", "via": "vod_file",
                        "note": "VOD-Playlist lokal vorbereitet — VLC startet am Anfang",
                    })
                    return

        if target in self.URL_SCHEMES:
            title = (body.get("title") or "").strip()
            scheme_url = self.URL_SCHEMES[target].format(
                url=urllib.parse.quote(stream, safe=""),
                title=urllib.parse.quote(title, safe=""),
            )
            ok, err = _open_url(scheme_url)
            if ok:
                self._send_json(200, {"ok": True, "target": target, "via": "scheme"})
                return

            # Fallback: das URL-Schema ist nicht registriert (häufig bei VLC/macOS,
            # da `vlc-x-callback://` ein iOS-Schema ist). Direkt die App mit der
            # Stream-URL als Argument starten.
            fallback_app = APP_FALLBACK.get(target)
            if fallback_app:
                ok2, err2 = _open_with_app(fallback_app, stream)
                if ok2:
                    self._send_json(200, {
                        "ok": True, "target": target, "via": "app",
                        "note": f"URL-Schema nicht registriert; {fallback_app} direkt geöffnet",
                    })
                    return
                self._send_json(500, {
                    "ok": False, "target": target,
                    "error": (
                        f"Weder URL-Schema noch direkter App-Aufruf hat funktioniert. "
                        f"Schema: {err}. App: {err2}"
                    ),
                })
                return

            self._send_json(500, {"ok": False, "target": target, "error": err})
            return

        # target == metube
        host = (body.get("metube_host") or "").strip()
        if not host:
            self._send_json(400, {"error": "metube_host fehlt (in den Einstellungen setzen)"})
            return
        filename_prefix = (body.get("filename") or "").strip()

        payload = json.dumps({
            "url": stream,
            "quality": "best",
            "format": "any",
            "auto_start": True,
            "custom_name_prefix": filename_prefix,
        }).encode("utf-8")
        metube_url = f"http://{host}/add"
        req = urllib.request.Request(
            metube_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                metube_resp = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.URLError as e:
            self._send_json(502, {"error": f"Metube nicht erreichbar: {e.reason}"})
            return
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"Metube-Fehler: {e}"})
            return

        ok = metube_resp.get("status") == "ok"
        self._send_json(200 if ok else 502, {
            "ok": ok,
            "target": "metube",
            "metube": metube_resp,
        })

    def _handle_jobs_list(self) -> None:
        """Liefert alle nicht-terminalen Jobs (queued + running) — vom Frontend
        beim Bootstrap genutzt, um nach Page-Reload die Progress-Anzeige der
        laufenden Downloads wiederherzustellen."""
        with DOWNLOAD_LOCK:
            active = [
                j for j in DOWNLOAD_JOBS.values()
                if j.state in ("queued", "running")
            ]
        items = []
        for job in active:
            d = job.to_dict()
            d["queue_position"] = _queue_position(job)
            items.append(d)
        items.sort(key=lambda j: j["queued_at"])
        self._send_json(200, {"jobs": items, "count": len(items)})

    def _handle_progress(self, job_id: str) -> None:
        with DOWNLOAD_LOCK:
            job = DOWNLOAD_JOBS.get(job_id)
        if job is None:
            self._send_json(404, {"error": "job not found"})
            return
        d = job.to_dict()
        d["queue_position"] = _queue_position(job)
        self._send_json(200, d)

    def _handle_cancel(self, job_id: str) -> None:
        with DOWNLOAD_LOCK:
            job = DOWNLOAD_JOBS.get(job_id)
        if job is None:
            self._send_json(404, {"error": "job not found"})
            return
        if job.state in ("done", "error", "cancelled"):
            self._send_json(200, {"ok": True, "state": job.state})
            return
        # Worker-Thread sieht das Event und terminiert den Subprozess.
        # Falls noch in Queue: state hier direkt umsetzen, der Worker
        # überspringt die Job-ID dann.
        job.cancelled.set()
        if job.state == "queued":
            job.state = "cancelled"
            job.finished_at = time.time()
        self._send_json(200, {"ok": True, "state": job.state})

    # -- Quieter logging --

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write(
            f"[{_dt.datetime.now().strftime('%H:%M:%S')}] "
            f"{self.address_string()} {fmt % args}\n"
        )


# --- Server-Bootstrap ------------------------------------------------------


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _port_free(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zattoo-DL GUI — moderne Web-Oberfläche für Zattoo-Aufnahmen.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (Default: {DEFAULT_PORT})")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Bind-Adresse (Default: 127.0.0.1; für Docker 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Browser nicht automatisch öffnen")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Verzeichnis für lokale Downloads "
                             "(überschreibt ZATTOO_DL_OUTPUT_DIR; "
                             "wird wiederum vom UI-Override in config.json überschrieben).")
    args = parser.parse_args()

    CONFIG["cli_output_dir"] = args.output_dir
    out_dir, source = _resolve_output_dir(args.output_dir)
    CONFIG["output_dir"] = out_dir
    CONFIG["output_dir_source"] = source
    print(f"📂 Output-Verzeichnis: {out_dir}  (Quelle: {source})")

    _ensure_dirs()

    if not GUI_DIR.exists():
        print(f"❌ Frontend-Ordner fehlt: {GUI_DIR}", file=sys.stderr)
        return 1

    port = args.port
    if not _port_free(args.bind, port):
        print(f"⚠️  Port {port} auf {args.bind} belegt — nutze --port <anderer>",
              file=sys.stderr)
        return 1

    GUIHandler.client = ZattooClient()

    server = ThreadingServer((args.bind, port), GUIHandler)
    display_host = "127.0.0.1" if args.bind in ("127.0.0.1", "localhost") else args.bind
    url = f"http://{display_host}:{port}"
    print(f"🚀 Zattoo-DL GUI läuft auf {url}")
    if args.bind == "0.0.0.0":
        print("   ⚠️  Bind 0.0.0.0 — alle Interfaces, nur in vertrauten Netzen "
              "oder hinter Reverse-Proxy verwenden.")
    print("   Strg+C zum Beenden.")

    if not args.no_browser and args.bind in ("127.0.0.1", "localhost"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ Beendet.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
