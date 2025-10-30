from typing import Callable, Dict, Any
from core.bus import Event

Publisher = Callable[[Event], None]

class Monitor:
    name = "base"
    def __init__(self, publish: Publisher, config: Dict[str, Any], ctx: Dict[str, Any]):
        self.publish = publish
        self.config = config
        self.ctx = ctx  # ctx["state"] is a State object
        
        # Optional feed interface for structured logging
        # Monitors can use: self.feed.log(), .log_post(), .log_api_call(), .stat()
        # If feeds are disabled, these are no-ops
        self.feed = ctx.get("feed")  # MonitorFeed or NoOpFeed

    def run(self) -> None:
        raise NotImplementedError