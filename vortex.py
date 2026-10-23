# vortex.py - LIGHTWEIGHT VINTED WAR MACHINE (fixed + web keep-alive)
import json
import os
import random
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
import http.server
import socketserver
import threading

import requests

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().with_name("config.example.json")
SEEN_ITEMS_PATH = Path(__file__).resolve().with_name("seen_items.json")
WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
RATE_LIMIT_BASE_SECONDS = 30
RATE_LIMIT_MAX_SECONDS = 300
DISCORD_SEND_DELAY_SECONDS = 1
SESSION_REFRESH_SECONDS = 20 * 60
MAX_PRICE = 50

WEBHOOK_URL = ""
SCAN_INTERVAL = 30
TERRAINS = []
PROXY = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.vinted.fr/",
}

seen_ids = set()
session = requests.Session()
_session_started_at = 0.0


# === MINI SERVEUR POUR RENDER (Garde le Web Service gratuit actif) ===
def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            print(f"[+] Keep-alive server running on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"[!] Keep-alive server error: {e}")

threading.Thread(target=keep_alive, daemon=True).start()
# ===================================================================


def _parse_retry_after(value):
    if not value:
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            return max(0, int(retry_at.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return 0


def _rate_limit_delay(attempt, retry_after=0):
    exponent = min(max(attempt - 1, 0), 10)
    backoff = min(RATE_LIMIT_BASE_SECONDS * (2**exponent), RATE_LIMIT_MAX_SECONDS)
    return max(backoff, retry_after)


class ConfigurationError(ValueError):
    """Raised when the required startup configuration is invalid."""


def _valid_http_url(value, allow_credentials=False):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and (allow_credentials or (not parsed.username and not parsed.password))
        )
    except ValueError:
        return False


def _valid_discord_webhook(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        path_parts = [part for part in parsed.path.split("/") if part]
    except ValueError:
        return False

    is_discord_host = parsed.hostname in ("discord.com", "discordapp.com")
    if len(path_parts) == 4:
        is_webhook_path = path_parts[:2] == ["api", "webhooks"]
        webhook_parts = path_parts[2:]
    elif len(path_parts) == 5:
        is_webhook_path = (
            path_parts[0] == "api"
            and re.fullmatch(r"v\d+", path_parts[1]) is not None
            and path_parts[2] == "webhooks"
        )
        webhook_parts = path_parts[3:]
    else:
        is_webhook_path = False
        webhook_parts = []

    return (
        parsed.scheme == "https"
        and is_discord_host
        and is_webhook_path
        and all(webhook_parts)
        and not parsed.username
        and not parsed.password
    )


def load_configuration(config_path=CONFIG_PATH, environ=None):
    config_path = Path(config_path)
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw_config = json.load(config_file)
    except FileNotFoundError:
        raise ConfigurationError(
            f"{config_path.name} is missing. Copy "
            f"{EXAMPLE_CONFIG_PATH.name} to {config_path.name} and configure it."
        ) from None
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"{config_path.name} contains invalid JSON "
            f"(line {error.lineno}, column {error.colno})."
        ) from None
    except OSError as error:
        raise ConfigurationError(
            f"Cannot read {config_path.name} ({type(error).__name__})."
        ) from None

    errors = []
    if not isinstance(raw_config, dict):
        raise ConfigurationError(f"{config_path.name} must contain a JSON object.")

    allowed_config_keys = {
        "_scan_interval_seconds",
        "_terrains",
        "_proxy_optional_http",
    }
    unknown_keys = sorted(set(raw_config) - allowed_config_keys)
    if "_webhook_discord" in raw_config:
        errors.append(
            f"Remove `_webhook_discord` from {config_path.name}; set "
            f"{WEBHOOK_ENV_VAR} as an environment secret instead."
        )
    other_unknown_keys = [key for key in unknown_keys if key != "_webhook_discord"]
    if other_unknown_keys:
        errors.append(f"Unsupported configuration field(s): {', '.join(other_unknown_keys)}.")

    environment = os.environ if environ is None else environ
    webhook_url = environment.get(WEBHOOK_ENV_VAR)
    if not webhook_url or not webhook_url.strip():
        errors.append(f"Missing required environment secret: {WEBHOOK_ENV_VAR}.")
    elif not _valid_discord_webhook(webhook_url):
        errors.append(
            f"{WEBHOOK_ENV_VAR} must be an HTTPS Discord webhook URL "
            "on discord.com or discordapp.com."
        )

    scan_interval = raw_config.get("_scan_interval_seconds", 30)
    if (
        not isinstance(scan_interval, int)
        or isinstance(scan_interval, bool)
        or scan_interval <= 0
    ):
        errors.append("`_scan_interval_seconds` must be a positive integer.")

    raw_terrains = raw_config.get("_terrains")
    terrains = []
    if not isinstance(raw_terrains, list) or not raw_terrains:
        errors.append("`_terrains` must be a non-empty list.")
    else:
        required_terrain_keys = {"name", "url", "color"}
        for index, terrain in enumerate(raw_terrains):
            label = f"`_terrains[{index}]`"
            if not isinstance(terrain, dict):
                errors.append(f"{label} must be an object.")
                continue

            missing_keys = sorted(required_terrain_keys - set(terrain))
            extra_keys = sorted(set(terrain) - required_terrain_keys)
            if missing_keys:
                errors.append(f"{label} is missing: {', '.join(missing_keys)}.")
            if extra_keys:
                errors.append(f"{label} has unsupported field(s): {', '.join(extra_keys)}.")

            name = terrain.get("name")
            url = terrain.get("url")
            color = terrain.get("color")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{label}.name must be a non-empty string.")
            if not _valid_http_url(url):
                errors.append(f"{label}.url must be a valid HTTP or HTTPS URL.")
            if not isinstance(color, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
                errors.append(f"{label}.color must be a six-digit hexadecimal color.")
            if (
                isinstance(name, str)
                and name.strip()
                and _valid_http_url(url)
                and isinstance(color, str)
                and re.fullmatch(r"#?[0-9a-fA-F]{6}", color)
            ):
                terrains.append({"name": name.strip(), "url": url, "color": color})

    proxy = raw_config.get("_proxy_optional_http")
    if proxy is not None and (
        not isinstance(proxy, str) or not _valid_http_url(proxy, allow_credentials=True)
    ):
        errors.append("`_proxy_optional_http` must be an HTTP or HTTPS URL, or null.")

    if errors:
        raise ConfigurationError("Invalid configuration:\n- " + "\n- ".join(errors))

    return {
        "_scan_interval_seconds": scan_interval,
        "_terrains": terrains,
        "_proxy_optional_http": proxy,
    }, webhook_url


def load_seen_items():
    if not SEEN_ITEMS_PATH.exists():
        SEEN_ITEMS_PATH.write_text("[]", encoding="utf-8")
    try:
        with SEEN_ITEMS_PATH.open("r", encoding="utf-8") as seen_file:
            return set(str(i) for i in json.load(seen_file))
    except (json.JSONDecodeError, OSError):
        return set()


def log_seen(item_id):
    seen_ids.add(str(item_id))
    with SEEN_ITEMS_PATH.open("w", encoding="utf-8") as seen_file:
        json.dump(list(seen_ids), seen_file, indent=2)


def hex_to_int(hex_color):
    return int(hex_color.lstrip("#"), 16)


def ensure_session(base_url, force=False):
    global _session_started_at
    if not force and time.time() - _session_started_at < SESSION_REFRESH_SECONDS:
        return
    session.cookies.clear()
    session.headers.update(HEADERS)
    session.proxies = {"http": PROXY, "https": PROXY} if PROXY else {}
    session.get(base_url, timeout=15)
    _session_started_at = time.time()


def build_params(url):
    params = {}
    catalog_ids = []
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
        if key in ("catalog[]", "catalog_ids"):
            catalog_ids.extend(v for v in value.split(",") if v)
        elif key.endswith("[]"):
            base = key[:-2]
            params[base] = f"{params[base]},{value}" if base in params else value
        elif key not in ("page", "per_page", "time"):
            params[key] = value
    if catalog_ids:
        params["catalog_ids"] = ",".join(catalog_ids)
    params.setdefault("order", "newest_first")
    params["page"] = 1
    params["per_page"] = 96
    return params


def send_alert(terrain_name, title, price, url, image_url, color_hex):
    embed = {
        "title": f"🚨 PÉPITE LOCALISÉE : {title}"[:256],
        "url": url,
        "description": f"**💰 Prix : {price}€**\n\n👉 [CLIQUE ICI POUR ACHETER]({url})",
        "color": hex_to_int(color_hex),
        "footer": {"text": f"🌍 {terrain_name} | ⏰ {datetime.now().strftime('%H:%M:%S')}"},
    }
    if image_url:
        embed["image"] = {"url": image_url}

    data = {
        "username": "Vortex V1",
        "avatar_url": "https://i.imgur.com/7KzR6lP.png",
        "embeds": [embed],
    }

    try:
        r = requests.post(WEBHOOK_URL, json=data, timeout=10)
        if r.status_code in (200, 204):
            print(f"[+] Alert sent: {title[:30]}...")
        else:
            print(f"[!] Discord fail: {r.status_code}")
    except Exception as error:
        print(
            f"[X] Webhook error ({type(error).__name__}); "
            "details suppressed to protect the Discord webhook secret."
        )


def scan_terrain(name, url, color):
    try:
        parts = urlsplit(url)
        base_url = f"{parts.scheme}://{parts.netloc}"
        ensure_session(base_url)

        r = session.get(
            f"{base_url}/api/v2/catalog/items",
            params=build_params(url),
            timeout=15,
        )

        if r.status_code in (401, 403):
            ensure_session(base_url, force=True)
            return True, _parse_retry_after(r.headers.get("Retry-After")), f"HTTP {r.status_code}"

        if r.status_code == 429 or r.status_code >= 500:
            return True, _parse_retry_after(r.headers.get("Retry-After")), f"HTTP {r.status_code}"

        try:
            resp_json = r.json()
        except ValueError:
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"non-JSON response (HTTP {r.status_code})",
            )

        if not isinstance(resp_json, dict) or not isinstance(resp_json.get("items"), list):
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"unexpected response (HTTP {r.status_code})",
            )

        for item in resp_json["items"]:
            item_id = item.get("id")
            if item_id is None or str(item_id) in seen_ids:
                continue

            price_obj = item.get("price") or {}
            try:
                price_amount = float(price_obj.get("amount") if isinstance(price_obj, dict) else price_obj)
            except (TypeError, ValueError):
                log_seen(item_id)
                continue

            if price_amount > MAX_PRICE:
                log_seen(item_id)
                continue

            photo_data = item.get("photo") or {}
            photo = photo_data.get("full_size_url") or photo_data.get("url")
            item_url = item.get("url") or f"{base_url}/items/{item_id}"
            if item_url.startswith("/"):
                item_url = base_url + item_url

            send_alert(name, item.get("title", "Sans titre"), price_amount, item_url, photo, color)
            log_seen(item_id)
            time.sleep(DISCORD_SEND_DELAY_SECONDS)

        return False, 0, ""

    except Exception as error:
        print(f"[X] Scan fail ({name}): {type(error).__name__}")
        return True, 0, type(error).__name__


def main():
    global WEBHOOK_URL, SCAN_INTERVAL, TERRAINS, PROXY, seen_ids

    config, webhook_url = load_configuration()
    WEBHOOK_URL = webhook_url
    SCAN_INTERVAL = config["_scan_interval_seconds"]
    TERRAINS = config["_terrains"]
    PROXY = config["_proxy_optional_http"]
    seen_ids = load_seen_items()

    print("[+] VORTEX V1 — BOOTED AND HUNGRY LIKE A STARVED DOG")
    print(f"Loaded {len(seen_ids)} items into memory.")
    print("[*] Starting active watch...")

    rate_limit_attempts = 0

    while True:
        rate_limited = False

        for terrain in TERRAINS:
            should_back_off, retry_after, failure_reason = scan_terrain(
                terrain["name"], terrain["url"], terrain["color"]
            )
            if should_back_off:
                rate_limit_attempts += 1
                cooldown = _rate_limit_delay(rate_limit_attempts, retry_after)
                print(
                    f"[!] Vinted {failure_reason}; skipping remaining terrains "
                    f"and waiting {cooldown}s before retrying."
                )
                time.sleep(cooldown)
                rate_limited = True
                break

            rate_limit_attempts = 0
            time.sleep(random.uniform(3.5, 7))

        if rate_limited:
            continue

        jitter = random.randint(5, 15)
        print(f"[~] Cycle done. Sleeping for {SCAN_INTERVAL + jitter}s")
        time.sleep(SCAN_INTERVAL + jitter)


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as error:
        print(f"[X] Configuration error: {error}")
        raise SystemExit(1) from None
