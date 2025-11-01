#!/usr/bin/env python3
import os, sys, time, threading, importlib, traceback
from typing import Dict, Any, List
from dotenv import load_dotenv

from core.config import get_config
from core.state import State
from core.bus import make_publisher
from core.notify import notify_pushover

load_dotenv()

# Global print lock for thread-safe console output
_print_lock = threading.Lock()

def _thread_excepthook(args):
    with _print_lock:
        print(f"[thread:{getattr(args, 'thread', None)}] unhandled exception: {args.exc_type.__name__}: {args.exc_value}", flush=True)
        traceback.print_tb(args.exc_traceback)

threading.excepthook = _thread_excepthook

def load_monitor(name: str):
    mod = importlib.import_module(f"monitors.{name}")
    return mod.Monitor

def run_monitor_loop(MonitorCls, publish, cfg: Dict[str, Any], ctx: Dict[str, Any], name: str):
    """
    Keeps a monitor alive: if it raises, log and restart after a short backoff.
    """
    backoff = 5
    while True:
        try:
            mon = MonitorCls(publish=publish, config=cfg, ctx=ctx)
            mon.run()
        except Exception as e:
            with _print_lock:
                print(f"[runner:{name}] crashed: {e}", flush=True)
                traceback.print_exc()
            backoff = min(120, backoff * 2)
            for i in range(backoff, 0, -5):
                with _print_lock:
                    print(f"[runner:{name}] restart in {i}s …", flush=True)
                time.sleep(min(5, i))
            continue

def main():
    cfg = get_config()
    state = State(path=cfg["STATE_FILE"])
    publish = make_publisher(cfg=cfg, state=state)
    
    # Optional feed system for structured logging
    feed_storage = None
    feeds_enabled = os.getenv("ENABLE_MONITOR_FEEDS", "false").lower() in ("1", "true", "yes")
    
    if feeds_enabled:
        try:
            from core.monitor_feed import FeedStorage, MonitorFeed
            feed_db = os.getenv("MONITOR_FEEDS_DB", ".monitor_feeds.db")
            feed_storage = FeedStorage(feed_db)
            print(f"[main] Monitor feeds ENABLED → {feed_db}", flush=True)
        except Exception as e:
            print(f"[main] Failed to initialize feeds: {e}", file=sys.stderr)
            feeds_enabled = False
    else:
        print("[main] Monitor feeds DISABLED (set ENABLE_MONITOR_FEEDS=true to enable)", flush=True)

    enabled = [s.strip() for s in os.getenv("ENABLED_MONITORS", "truth_social,example").split(",") if s.strip()]
    pretty = {
        "truth_social": "Truth Social (@{})".format(cfg["TRUTH_HANDLE"]),
        "example": "Example Monitor",
        "taco": "TACO (Trump Always Chickens Out)",
        "bls_rss": "BLS Economic Releases (CPI/PPI/NFP)",
    }
    names_list = ", ".join(pretty.get(n, n) for n in enabled)

    # One-time startup ping
    try:
        notify_pushover(
            title="TruthTrader – service started",
            message=f"Monitors: {names_list}\nModel: {cfg['MODEL']}\nReasoning: {cfg['REASONING_MODEL']}\nFeeds: {'ON' if feeds_enabled else 'OFF'}",
            priority=0,
            token=cfg.get("PUSHOVER_TOKEN"),
            user=cfg.get("PUSHOVER_USER")
        )
    except Exception as e:
        print(f"[startup] Pushover notify failed: {e}", file=sys.stderr)

    threads: List[threading.Thread] = []
    for name in enabled:
        try:
            MonitorCls = load_monitor(name)
        except Exception as e:
            print(f"[main] Failed to load monitor '{name}': {e}", file=sys.stderr)
            continue
        
        # Build context for this monitor
        ctx = {"state": state, "print_lock": _print_lock}
        
        # Add feed interface if enabled
        if feeds_enabled:
            from core.monitor_feed import MonitorFeed
            ctx["feed"] = MonitorFeed(name, storage=feed_storage, enabled=True)
        else:
            from core.monitor_feed import NoOpFeed
            ctx["feed"] = NoOpFeed(name)
        
        t = threading.Thread(
            target=run_monitor_loop,
            args=(MonitorCls, publish, cfg, ctx, name),
            daemon=True,
            name=f"monitor:{name}"
        )
        t.start()
        threads.append(t)
        print(f"[main] started monitor: {name}", flush=True)

    # Quieter watchdog - only print status changes or every 5 minutes
    def _watchdog():
        last_status = {}
        last_full_report = time.time()
        
        while True:
            current_status = {t.name: t.is_alive() for t in threads}
            
            # Print if status changed or 5 minutes elapsed
            status_changed = current_status != last_status
            time_for_report = (time.time() - last_full_report) > 300
            
            if status_changed or time_for_report:
                with _print_lock:
                    for t in threads:
                        status = "✓ alive" if t.is_alive() else "✗ DEAD"
                        print(f"[watchdog] {t.name} {status}", flush=True)
                last_status = current_status.copy()
                last_full_report = time.time()
            
            time.sleep(15)

    wd = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
    wd.start()

    # Keep the main process alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        with _print_lock:
            print("\n[main] shutdown requested", flush=True)

if __name__ == "__main__":
    main()
