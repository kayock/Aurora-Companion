#!/usr/bin/env python3
"""Aurora entry point.

    python run.py                 # serve the dashboard (http://127.0.0.1:8795)
    python run.py --port 9000     # custom port
    python run.py --config my.yaml
    python run.py --check         # verify provider reachability and exit
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aurora.config import Config
from aurora.app import CompanionApp

ROOT = Path(__file__).parent
SEED_PATH = ROOT / "personas" / "default.json"


def build_app(config_path: Path | None) -> CompanionApp:
    config = Config.load(config_path)
    return CompanionApp(config, seed_path=SEED_PATH)


def tls_options(config: Config) -> dict:
    """Optional HTTPS for the dashboard. Browsers only expose the microphone
    (getUserMedia) in a secure context — HTTPS or localhost — so serving TLS
    on Aurora's own port is what enables LAN push-to-talk without any new
    service or port. Both paths must be configured; no certs ship in the
    repo (generate with deploy/make-cert.sh)."""
    cert = str(config.get("server.tls_cert", "") or "")
    key = str(config.get("server.tls_key", "") or "")
    if cert and key:
        if not (Path(cert).is_file() and Path(key).is_file()):
            print(f"WARNING: TLS configured but files missing "
                  f"({cert} / {key}) — serving plain HTTP")
            return {}
        return {"ssl_certfile": cert, "ssl_keyfile": key}
    if cert or key:
        print("WARNING: set BOTH server.tls_cert and server.tls_key — ignoring TLS")
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Aurora unified AI companion")
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("AURORA_CONFIG", ROOT / "config.yaml")))
    parser.add_argument("--port", type=int,
                        default=int(os.environ["AURORA_PORT"]) if os.environ.get("AURORA_PORT") else None)
    parser.add_argument("--host", type=str,
                        default=os.environ.get("AURORA_HOST") or None)
    parser.add_argument("--check", action="store_true", help="verify provider, voice services and exit")
    args = parser.parse_args()

    app = build_app(args.config if args.config.exists() else None)
    if args.port:
        app.config.set("port", args.port)
    if args.host:
        app.config.set("host", args.host)

    if args.check:
        async def _check():
            rc = 0
            if app.chat_provider is None:
                print("chat provider: NOT CONFIGURED")
                rc = 1
            else:
                ok = await app.chat_provider.healthcheck()
                print(f"chat provider: {app.chat_provider.name} -> {'reachable' if ok else 'UNREACHABLE'}")
                if not ok:
                    rc = 2
            # voice services (informational; never required for Aurora to run)
            tts = app.voice.tts
            if hasattr(tts, "list_voices"):
                voices, err = await tts.list_voices()
                if voices:
                    print(f"tts: {tts.name} {tts.endpoint} -> reachable, "
                          f"{len(voices)} voices, selected '{tts.voice}'")
                else:
                    print(f"tts: {tts.name} {tts.endpoint} -> UNREACHABLE ({err or 'no voices'})")
            else:
                print(f"tts: {tts.name} (no discovery)")
            stt = app.voice.stt
            if hasattr(stt, "healthcheck"):
                ok = await stt.healthcheck()
                print(f"stt: {stt.name} {stt.endpoint} -> {'reachable' if ok else 'UNREACHABLE'}")
            else:
                print(f"stt: {stt.name}")
            if app.config.get("resources.traffic_light_url"):
                snap = await app.coordinator.probe(force=True)
                print(f"traffic light: {snap['state']} ({snap['detail'].get('path') or 'no probe answered'})")
            else:
                print("traffic light: not configured (resources.traffic_light_url is empty)")
            return rc
        return asyncio.run(_check())

    import uvicorn
    from aurora.server.app import create_server

    api = create_server(app)

    @api.on_event("startup")
    async def _startup():
        await app.start()

    @api.on_event("shutdown")
    async def _shutdown():
        await app.stop()

    host = app.config.get("host", "127.0.0.1")
    port = int(app.config.get("port", 8795))
    tls = tls_options(app.config)
    scheme = "https" if tls else "http"
    print(f"Aurora {__import__('aurora').__version__} -> {scheme}://{host}:{port}")
    uvicorn.run(api, host=host, port=port, log_level="warning", **tls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
