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
import re
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
OUTPUT_DIR = WORKDIR / "output"

SCRIPT_DIR = Path(__file__).resolve().parent
GUI_DIR = SCRIPT_DIR / "gui"

CACHE_DIR = Path.home() / ".cache" / "zattoo-dl"
PLAYLIST_CACHE = CACHE_DIR / "playlist.json"
THUMBS_DIR = CACHE_DIR / "thumbs"
PLAYLIST_TTL = 300  # Sekunden

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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


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


def _prepare_vod_playlist(client: "ZattooClient", stream_url: str) -> Path | None:
    """Holt die Zattoo-HLS-Playlist, schreibt sie auf VOD um, speichert sie
    als lokale Datei. Liefert den Pfad, oder None wenn was schiefging."""
    try:
        master = _fetch_hls_text(client, stream_url)
    except Exception:  # noqa: BLE001
        return None

    if "#EXT-X-STREAM-INF" in master:
        variant_url = _pick_best_variant(master, stream_url)
        if not variant_url:
            return None
        try:
            variant = _fetch_hls_text(client, variant_url)
        except Exception:  # noqa: BLE001
            return None
        variant_base = variant_url
    else:
        # Es war direkt eine Variant-Playlist (kein Master)
        variant = master
        variant_base = stream_url

    rewritten = _rewrite_variant_for_vod(variant, variant_base)

    tmpdir = Path(tempfile.gettempdir())
    path = tmpdir / f"zattoo-{uuid.uuid4().hex[:8]}.m3u8"
    try:
        path.write_text(rewritten, encoding="utf-8")
    except OSError:
        return None

    # Debug-Kopie unter festem Namen, damit man bei Fehlern leicht reinschauen kann
    try:
        (tmpdir / "zattoo-vlc-latest.m3u8").write_text(rewritten, encoding="utf-8")
    except OSError:
        pass

    # Auf dem Server-Stdout die ersten Zeilen der vorbereiteten Playlist zeigen
    preview = "\n".join(rewritten.splitlines()[:8])
    print(f"[vlc] vorbereitet: {path}\n{preview}\n[…]", file=sys.stderr, flush=True)

    return path


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
        "downie": "downie://XUOpenLink?url={url}",
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
        if path == "/api/recordings":
            self._handle_recordings(force=False)
            return
        if path == "/api/thumbnail":
            self._handle_thumbnail(urllib.parse.parse_qs(parsed.query))
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/recordings/refresh":
            self._handle_recordings(force=True)
            return
        if path == "/api/download":
            self._handle_download()
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

        items = []
        for r in data.get("recordings", []):
            rid = r.get("id") or r.get("program_id") or ""
            if not rid:
                continue
            thumb = extract_thumbnail_url(r)
            items.append({
                "id": str(rid),
                "cid": r.get("cid") or "",
                "title": r.get("title") or "",
                "episode": r.get("episode_title") or "",
                "start": r.get("start") or "",
                "end": r.get("end") or "",
                "thumbnail": thumb,
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
        valid_targets = ("downie", "vlc", "metube")
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
            scheme_url = self.URL_SCHEMES[target].format(
                url=urllib.parse.quote(stream, safe="")
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


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zattoo-DL GUI — moderne Web-Oberfläche für Zattoo-Aufnahmen.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (Default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Browser nicht automatisch öffnen")
    args = parser.parse_args()

    _ensure_dirs()

    if not GUI_DIR.exists():
        print(f"❌ Frontend-Ordner fehlt: {GUI_DIR}", file=sys.stderr)
        return 1

    port = args.port
    if not _port_free(port):
        print(f"⚠️  Port {port} belegt — nutze --port <anderer>", file=sys.stderr)
        return 1

    GUIHandler.client = ZattooClient()

    server = ThreadingServer(("127.0.0.1", port), GUIHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"🚀 Zattoo-DL GUI läuft auf {url}")
    print("   Strg+C zum Beenden.")

    if not args.no_browser:
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
