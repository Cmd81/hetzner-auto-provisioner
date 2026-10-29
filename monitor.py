#!/usr/bin/env python3
"""Hetzner Cloud capacity watcher / one-time capped auto-provisioner.

Requires only Python 3.9+ standard library. Never deletes servers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.hetzner.cloud/v1"
LABEL_KEY = "managed-by"
LABEL_VALUE = "hetzner-capacity-watcher"
LOG = logging.getLogger("hetzner-watcher")
STOP = False


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def as_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class Target:
    kind: str
    location: str
    count: int

    def name(self, prefix: str, index: int) -> str:
        return f"{prefix}-{self.kind}-{self.location}-{index:02d}"


def parse_targets(spec: str) -> list[Target]:
    """Syntax: cx23@fsn1=1,cx23@nbg1=2,cpx22@hel1=1"""
    result = []
    seen = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        match = re.fullmatch(r"([a-z0-9]+)@([a-z0-9]+)=([1-9][0-9]*)", part)
        if not match:
            raise ValueError(f"Invalid TARGETS entry: {part!r}; expected TYPE@LOCATION=COUNT")
        kind, location, count_string = match.groups()
        key = (kind, location)
        if key in seen:
            raise ValueError(f"Duplicate TARGETS entry: {kind}@{location}")
        seen.add(key)
        count = int(count_string)
        if count > 20:
            raise ValueError("Maximum COUNT per target is 20 for safety")
        result.append(Target(kind, location, count))
    if not result:
        raise ValueError("TARGETS must define at least one server target")
    return result


@dataclass
class Config:
    token: str
    interval: int
    image: str
    targets: list[Target]
    prefix: str
    ssh_keys: list[str]
    auto_create: bool
    max_per_cycle: int
    state_file: Path

    @classmethod
    def from_environment(cls) -> "Config":
        token = os.getenv("HETZNER_API_TOKEN", "").strip()
        if not token or token in ("YOUR_HETZNER_TOKEN", "CHANGE_ME"):
            raise ValueError("Set a valid HETZNER_API_TOKEN in the configuration file")
        interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
        if not 30 <= interval <= 86400:
            raise ValueError("POLL_INTERVAL_SECONDS must be between 30 and 86400")
        max_per_cycle = int(os.getenv("MAX_CREATES_PER_CYCLE", "1"))
        if not 1 <= max_per_cycle <= 10:
            raise ValueError("MAX_CREATES_PER_CYCLE must be between 1 and 10")
        image = os.getenv("SERVER_IMAGE", "ubuntu-22.04").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", image):
            raise ValueError("Invalid SERVER_IMAGE")
        prefix = os.getenv("NAME_PREFIX", "hcloud-auto").strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,24}[a-z0-9])?", prefix):
            raise ValueError("NAME_PREFIX must be 1-26 lowercase letters/digits/hyphens")
        targets = parse_targets(os.getenv("TARGETS", "cx23@fsn1=1"))
        for target in targets:
            if len(target.name(prefix, target.count)) > 63:
                raise ValueError("Generated server name is longer than 63 characters")
        keys = [x.strip() for x in os.getenv("SSH_KEYS", "").split(",") if x.strip()]
        for key in keys:
            if not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,150}", key):
                raise ValueError("Invalid SSH_KEYS value")
        return cls(
            token=token, interval=interval, image=image, targets=targets, prefix=prefix,
            ssh_keys=keys, auto_create=as_bool(os.getenv("AUTO_CREATE", "false")),
            max_per_cycle=max_per_cycle,
            state_file=Path(os.getenv("STATE_FILE", "/var/lib/hetzner-auto-provisioner/state.json")),
        )


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} ({code}): {message}")
        self.status, self.code = status, code


class RateLimited(ApiError):
    def __init__(self, seconds: int):
        super().__init__(429, "rate_limit_exceeded", "API quota exceeded")
        self.seconds = seconds


class HetznerAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, endpoint: str, payload: dict | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(
            f"{API}{endpoint}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "hetzner-capacity-watcher/1.0"},
        )
        try:
            with urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read(8192)
            try:
                error = json.loads(raw.decode("utf-8")).get("error", {})
            except (ValueError, UnicodeError):
                error = {}
            if exc.code == 429:
                retry = exc.headers.get("Retry-After")
                reset = exc.headers.get("RateLimit-Reset")
                delay = 120
                if retry and retry.isdigit():
                    delay = max(60, int(retry))
                elif reset and reset.isdigit():
                    delay = max(60, int(reset) - int(time.time()))
                raise RateLimited(min(delay, 3600)) from exc
            raise ApiError(exc.code, str(error.get("code", "unknown")),
                           str(error.get("message", "Unexpected API error"))) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ConnectionError("Hetzner API connection failed; check network and retry later") from exc

    def list_collection(self, endpoint: str, key: str) -> list[dict]:
        page, output = 1, []
        while True:
            separator = "&" if "?" in endpoint else "?"
            data = self.request("GET", f"{endpoint}{separator}{urlencode({'page': page, 'per_page': 50})}")
            output.extend(data.get(key, []))
            pagination = (data.get("meta") or {}).get("pagination") or {}
            next_page = pagination.get("next_page")
            if next_page is None:
                break
            page = int(next_page)
            if page > 1000:
                raise ValueError("Pagination limit exceeded")
        return output

    def server_types(self) -> dict[str, dict]:
        return {item["name"]: item for item in self.list_collection("/server_types", "server_types")}

    def servers(self) -> list[dict]:
        return self.list_collection("/servers", "servers")

    def action(self, action_id: int) -> dict:
        return self.request("GET", f"/actions/{action_id}")["action"]

    def create(self, name: str, target: Target, config: Config) -> dict:
        body = {
            "name": name,
            "server_type": target.kind,
            "location": target.location,
            "image": config.image,
            "labels": {LABEL_KEY: LABEL_VALUE},
            "start_after_create": True,
        }
        if config.ssh_keys:
            body["ssh_keys"] = config.ssh_keys
        return self.request("POST", "/servers", body)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"completed": [], "pending": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("completed"), list) or not isinstance(data.get("pending"), dict):
        raise ValueError("Invalid state file; fix it manually before proceeding")
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def available_at(server_type: dict, location: str) -> bool:
    for entry in server_type.get("locations", []):
        loc_name = entry.get("name") or (entry.get("location") or {}).get("name")
        if loc_name == location:
            return entry.get("available") is True
    return False


def cycle(api: HetznerAPI, config: Config, state: dict) -> bool:
    """Poll once. Return true when state changed."""
    changed = False
    types = api.server_types()
    servers = {server.get("name"): server for server in api.servers()}

    # Verify async server creation before treating a successful HTTP POST as complete.
    for name, item in list(state["pending"].items()):
        if item.get("action_id") is None:
            # Crash/old response fallback: never re-POST while the same name exists.
            if name in servers and servers[name].get("status") in ("running", "off"):
                state["completed"].append(name)
                del state["pending"][name]
                changed = True
            continue
        action = api.action(int(item["action_id"]))
        status = action.get("status")
        if status == "success":
            state["completed"].append(name)
            del state["pending"][name]
            LOG.info("Provisioned successfully: %s", name)
            changed = True
        elif status == "error":
            del state["pending"][name]
            LOG.warning("Provisioning action failed for %s: %s", name, action.get("error"))
            changed = True

    completed = set(state["completed"])
    started = 0
    for target in config.targets:
        st = types.get(target.kind)
        if st is None:
            LOG.warning("Unknown server type %s; skipping", target.kind)
            continue
        if not available_at(st, target.location):
            LOG.info("Not available: %s at %s", target.kind, target.location)
            continue
        for index in range(1, target.count + 1):
            name = target.name(config.prefix, index)
            if name in completed or name in state["pending"]:
                continue
            if name in servers:
                server = servers[name]
                if (server.get("labels") or {}).get(LABEL_KEY) == LABEL_VALUE:
                    if server.get("status") in ("running", "off"):
                        state["completed"].append(name)
                        completed.add(name)
                        changed = True
                        LOG.info("Adopted existing managed server: %s", name)
                    else:
                        LOG.info("Server %s already exists; waiting for stable status", name)
                else:
                    LOG.error("Name conflict with an unmanaged server: %s", name)
                continue
            if not config.auto_create:
                LOG.warning("AVAILABLE (dry-run): %s at %s -> would create %s", target.kind, target.location, name)
                continue
            if started >= config.max_per_cycle:
                break
            LOG.info("Creating %s | type=%s | location=%s | image=%s", name, target.kind, target.location, config.image)
            result = api.create(name, target, config)
            server = result.get("server") or {}
            action = result.get("action") or {}
            action_id = action.get("id")
            if action_id is not None:
                state["pending"][name] = {"action_id": action_id, "server_id": server.get("id")}
            else:
                # Do not assume completion: wait for server status next cycle.
                state["pending"][name] = {"action_id": None, "server_id": server.get("id")}
            changed = True
            started += 1
            # Persist each successful POST before another is sent.
            save_state(config.state_file, state)
        if started >= config.max_per_cycle:
            break
    return changed


def shutdown(signum, frame) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Hetzner Cloud auto-provisioner")
    parser.add_argument("--once", action="store_true", help="Perform one API check then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(Path(os.getenv("CONFIG_FILE", str(Path(__file__).with_name(".env")))))
    try:
        config = Config.from_environment()
        state = load_state(config.state_file)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        LOG.error("Configuration/state error: %s", exc)
        return 2
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Started: interval=%ss, image=%s, targets=%s, auto_create=%s",
             config.interval, config.image, len(config.targets), config.auto_create)
    api = HetznerAPI(config.token)
    while not STOP:
        delay = config.interval
        try:
            if cycle(api, config, state):
                save_state(config.state_file, state)
        except RateLimited as exc:
            delay = max(delay, exc.seconds)
            LOG.warning("Rate limited; sleeping %ss before next check", delay)
        except ApiError as exc:
            LOG.error("Hetzner error: %s", exc)
            if exc.status in (401, 403):
                LOG.error("Invalid/insufficient API token permissions; exiting")
                return 3
            if exc.status in (400, 422):
                delay = max(delay, 180)
        except (ConnectionError, OSError, ValueError) as exc:
            LOG.error("Check failed: %s", exc)
            delay = max(delay, 120)
        if args.once:
            break
        end = time.monotonic() + delay
        while not STOP and time.monotonic() < end:
            time.sleep(min(1, max(0, end - time.monotonic())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
