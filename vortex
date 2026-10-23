# vortex.py - LIGHTWEIGHT VINTED WAR MACHINE
import json
import os
import random
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().with_name("config.example.json")
SEEN_ITEMS_PATH = Path(__file__).resolve().with_name("seen_items.json")
WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
RATE_LIMIT_BASE_SECONDS = 30
RATE_LIMIT_MAX_SECONDS =30
DISCORD_SEND_DELAY_SECONDS = 1

WEBHOOK_URL = ""
SCAN_INTERVAL = 30
TERRAINS = []
PROXY = PROXY = "http://51.159.66.19:80"  # ou un autre proxy fonctionnel

seen_ids = set()


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
            and (
                allow_credentials
                or (not parsed.username and not parsed.password)
            )
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
    """Load public settings from JSON and the Discord URL from the environment."""
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
            if not isinstance(color, str) or not re.fullmatch(
                r"#?[0-9a-fA-F]{6}", color
            ):
                errors.append(f"{label}.color must be a six-digit hexadecimal color.")
            if (
                isinstance(name, str)
                and name.strip()
                and _valid_http_url(url)
                and isinstance(color, str)
                and re.fullmatch(r"#?[0-9a-fA-F]{6}", color)
            ):
                terrains.append(
                    {"name": name.strip(), "url": url, "color": color}
                )

    proxy = raw_config.get("_proxy_optional_http")
    if proxy is not None and (
        not isinstance(proxy, str)
        or not _valid_http_url(proxy, allow_credentials=True)
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

    with SEEN_ITEMS_PATH.open("r", encoding="utf-8") as seen_file:
        stored_ids = json.load(seen_file)
    return set(stored_ids)


def log_seen(item_id):
    seen_ids.add(str(item_id))
    with SEEN_ITEMS_PATH.open("w", encoding="utf-8") as seen_file:
        json.dump(list(seen_ids), seen_file, indent=2)


def hex_to_int(hex_color):
    return int(hex_color.lstrip("#"), 16)


# === SEND ALERT TO DISCORD ===
def send_alert(terrain_name, title, price, url, image_url, color_hex):
    color_int = hex_to_int(color_hex)

    data = {
        "username": "Vortex V1",
        "avatar_url": "https://i.imgur.com/7KzR6lP.png",
        "embeds": [
            {
                "title": f"🚨 PÉPITE LOCALISÉE : {title}",
                "url": url,
                "description": f"**💰 Prix : {price}€**\n\n👉 [CLIQUE ICI POUR ACHETER]({url})",
                "color": color_int,
                "image": {"url": image_url},
                "footer": {
                    "text": f"🌍 {terrain_name} | ⏰ {datetime.now().strftime('%H:%M:%S')}"
                },
            }
        ],
    }

    try:
        r = requests.post(WEBHOOK_URL, json=data, timeout=5)
        if r.status_code == 204:
            print(f"[+] Alert sent: {title[:30]}...")
        else:
            print(f"[!] Discord fail: {r.status_code}")
    except Exception as error:
        # Requests exceptions can contain the full URL, including the webhook token.
        print(
            f"[X] Webhook error ({type(error).__name__}); "
            "details suppressed to protect the Discord webhook secret."
        )


# === SCAN A SINGLE URL ===
def scan_terrain(name, url, color):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip",
        "Connection": "keep-alive",
        "Referer": "https://www.vinted.fr/",
    }

    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    session.get("https://www.vinted.fr", headers=headers)

    try:
        # Use API to avoid HTML parsing
        api_url = "https://www.vinted.fr/api/v2/catalog/items"

        params = {
            "search_text": "",
            "catalog_ids": "",
            "order": "newest_first",
            "page": 1,
            "per_page": 96,
            # Extract filters from your URL manually and paste here later if needed
        }

        # If you have full URL with filters, extract search params manually and add them above.
        r = requests.get(
            api_url,
            headers=headers,
            params=params,
            proxies=proxies,
            timeout=15,
        )

        if r.status_code in (429, 403):
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"HTTP {r.status_code}",
            )

        if r.status_code >= 500:
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"HTTP {r.status_code}",
            )

        try:
            resp_json = r.json()
        except ValueError:
            content_type = r.headers.get("Content-Type", "unknown")
            print(
                "[!] Vinted returned a non-JSON response "
                f"(HTTP {r.status_code}, content-type {content_type})."
            )
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"non-JSON response (HTTP {r.status_code})",
            )

        if not isinstance(resp_json, dict) or not isinstance(
            resp_json.get("items"), list
        ):
            print(f"[!] Vinted returned an unexpected catalog response (HTTP {r.status_code}).")
            return (
                True,
                _parse_retry_after(r.headers.get("Retry-After")),
                f"unexpected response (HTTP {r.status_code})",
            )

        for item in resp_json["items"]:
            item_id = item["id"]

            if str(item_id) in seen_ids:
                continue

            # Price check — only alert for low prices?
            price_amount = float(item["price"]["amount"])

            # Optional filter — comment out to receive all items regardless of price.
            if price_amount > 50:
                log_seen(item_id)
                continue

            title = item["title"]
            price = item["price"]["amount"]
            photo = item["photo"]["urls"]["large"] or item["photo"]["urls"]["medium"]
            item_url = item["url"]

            send_alert(name, title, price, item_url, photo, color)

            log_seen(item_id)
            time.sleep(DISCORD_SEND_DELAY_SECONDS)

        return False, 0, ""

    except Exception as error:
        # Request errors can contain proxy credentials or full request URLs.
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
    print("[*] Loading known items...")
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
            time.sleep(random.uniform(3.5, 7))  # Random delay between terrains

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
