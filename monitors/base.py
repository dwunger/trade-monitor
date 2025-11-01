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
        self.feed = ctx.get("feed")
        
        # Thread-safe printing
        self._print_lock = ctx.get("print_lock")
    
    def _print(self, message: str, end: str = '\n', flush: bool = True):
        """Thread-safe print"""
        if self._print_lock:
            with self._print_lock:
                print(f"[{self.name}] {message}", end=end, flush=flush)
        else:
            print(f"[{self.name}] {message}", end=end, flush=flush)

    def run(self) -> None:
        raise NotImplementedError
