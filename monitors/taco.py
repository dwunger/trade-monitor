"""
TACO Monitor: Trump Always Chickens Out
Uses Haiku for cheap, intelligent screening of tariff-related posts.
"""
import os, re, time, inspect
from typing import Optional
from .base import Monitor
from core.bus import Event

VERSION = "taco/2.1.0-feeds"
print(f"[taco] module file → {inspect.getfile(inspect.currentframe())}", flush=True)

class Monitor(Monitor):
    name = "taco"
    
    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        
        try:
            import truthbrush as tb
            self.api = tb.Api()
        except Exception as e:
            raise RuntimeError(f"truthbrush module not available: {e}")
        
        try:
            from anthropic import Anthropic
            self.anthropic = Anthropic(api_key=config["ANTHROPIC_API_KEY"])
        except Exception as e:
            raise RuntimeError(f"Anthropic SDK not available: {e}")
        
        self.handle = config.get("TACO_HANDLE") or config["TRUTH_HANDLE"]
        self.poll_seconds = int(os.getenv("TACO_POLL_SECONDS", "90"))
        self.screening_model = os.getenv("TACO_SCREENING_MODEL", "claude-haiku-4-5-20251001")
        self.state = ctx.get("state")
        self.state_key_last = "taco:last_seen_id"
        self.config = config
        
        # For smart console output
        self._last_console_line_len = 0
        
        print(f"[taco] initialized for @{self.handle} | poll={self.poll_seconds}s | screening={self.screening_model}", flush=True)
        
        # Log to feed
        self.feed.log(f"Monitor initialized for @{self.handle}", subclass="lifecycle")
        self.feed.log(f"Screening model: {self.screening_model}", subclass="config")
    
    def _print_updating(self, message: str):
        """Print a message that updates in place"""
        if self._last_console_line_len > 0:
            print('\r' + ' ' * self._last_console_line_len + '\r', end='', flush=True)
        print(f"[taco] {message}", end='', flush=True)
        self._last_console_line_len = len(f"[taco] {message}")

    def _print_newline(self, message: str):
        """Print a message on a new line"""
        if self._last_console_line_len > 0:
            print('\r' + ' ' * self._last_console_line_len + '\r', end='', flush=True)
            self._last_console_line_len = 0
        print(f"[taco] {message}", flush=True)
    
    def _strip_html(self, s: str) -> str:
        if not s:
            return ""
        if "<" in s and ">" in s:
            s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    
    def _screen_with_haiku(self, text: str) -> dict:
        """Use Haiku to intelligently screen if post is tariff-related."""
        try:
            t0 = time.time()
            response = self.anthropic.messages.create(
                model=self.screening_model,
                max_tokens=200,
                temperature=0,
                system="You are a trading assistant screening social media posts. Your ONLY job is to identify if a post is about tariffs, trade policy, or trade negotiations. Respond with JSON only.",
                messages=[{
                    "role": "user",
                    "content": f'Is this post about tariffs/trade policy? Respond with JSON: {{"is_tariff_related": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}}\n\nPost: {text[:500]}'
                }]
            )
            
            duration = time.time() - t0
            
            # Extract text
            response_text = ""
            for block in response.content:
                if hasattr(block, 'type') and block.type == 'text':
                    response_text += block.text
            
            # Parse JSON
            import json
            response_text = response_text.strip()
            if response_text.startswith("```"):
                response_text = re.sub(r"^```[a-zA-Z]*\s*", "", response_text)
                response_text = re.sub(r"\s*```$", "", response_text)
            
            result = json.loads(response_text)
            
            # Log to feed
            tokens = response.usage.input_tokens + response.usage.output_tokens
            cost = tokens * 0.25 / 1_000_000  # Haiku pricing
            
            self.feed.log_api_call(
                provider="anthropic",
                model=self.screening_model,
                tokens=tokens,
                cost=cost,
                duration=duration,
                subclass="screening"
            )
            
            return result
            
        except Exception as e:
            self._print_newline(f"Haiku screening error: {e}")
            self.feed.error("Screening failed", exception=e, subclass="screening")
            # Fallback to simple keyword check
            text_lower = text.lower()
            is_related = any(kw in text_lower for kw in ["tariff", "trade war", "trade deal"])
            return {"is_tariff_related": is_related, "confidence": 0.5, "reasoning": "fallback"}
    
    def run(self) -> None:
        self._print_newline(f"RUN START – {VERSION}")
        self.feed.log(f"Monitor starting – {VERSION}", subclass="lifecycle")
        
        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        self._print_newline(f"Monitoring @{self.handle} for tariff posts | last_seen={last_seen}")
        
        # Bootstrap
        if not last_seen:
            self._print_newline("Bootstrap → fetching recent posts")
            self.feed.log("Bootstrap: fetching initial posts", subclass="lifecycle")
            
            try:
                page_iter = self.api.pull_statuses(
                    username=self.handle, replies=False, verbose=False,
                    created_after=None, since_id=None, pinned=False,
                )
                first_page = []
                for i, post in enumerate(page_iter):
                    first_page.append(post)
                    if i >= 5:
                        break
                
                if first_page:
                    latest = first_page[0]
                    raw = latest.get("content") or latest.get("text") or ""
                    text = self._strip_html(raw)
                    
                    if text:
                        screen_result = self._screen_with_haiku(text)
                        
                        if screen_result["is_tariff_related"] and screen_result["confidence"] > 0.6:
                            url = latest.get("url") or f"https://truthsocial.com/@{self.handle}/{latest.get('id')}"
                            created_at = latest.get("created_at") or ""
                            
                            self._print_newline(f"✓ Bootstrap: TARIFF POST (conf={screen_result['confidence']:.2f})")
                            self.feed.log(
                                f"Bootstrap: tariff post detected (conf={screen_result['confidence']:.2f})",
                                subclass="screening",
                                data={"reasoning": screen_result['reasoning']}
                            )
                            
                            evt = Event(
                                source=self.name,
                                title="TACO Analysis",
                                message="Analyzing tariff-related post...",
                                url=url,
                                created_at=created_at,
                                priority=0,
                                payload={
                                    "analyze": True,
                                    "text": text,
                                    "taco_mode": True,
                                    "screen_confidence": screen_result["confidence"],
                                }
                            )
                            self.publish(evt)
                        else:
                            self._print_newline(f"✗ Bootstrap: not tariff-related (conf={screen_result['confidence']:.2f})")
                    
                    last_seen = latest["id"]
                    self.state.set(last_seen, self.state_key_last)
                    self._print_newline(f"Bootstrap complete | last_seen={last_seen}")
                    self.feed.log(f"Bootstrap complete: last_seen={last_seen}", subclass="lifecycle")
            except Exception as e:
                self._print_newline(f"Bootstrap error: {e}")
                self.feed.error("Bootstrap failed", exception=e, subclass="lifecycle")
        
        # Main loop
        while True:
            try:
                self._print_newline("Poll tick")
                self.feed.log("Poll cycle starting", subclass="polling")

                try:
                    page_iter = self.api.pull_statuses(
                        username=self.handle, replies=False, verbose=False,
                        created_after=None, since_id=last_seen, pinned=False,
                    )
                except Exception as e:
                    self._print_newline(f"Truthbrush fetch failed: {e}")
                    self.feed.error("Fetch failed", exception=e, subclass="polling")
                    time.sleep(60)
                    continue
                
                new_posts = []
                for i, post in enumerate(page_iter):
                    pid = post.get("id")
                    if not pid:
                        continue
                    if last_seen and pid <= last_seen:
                        break
                    new_posts.append(post)
                    if i > 25:
                        break
                
                if new_posts:
                    self._print_newline(f"Found {len(new_posts)} new post(s)")
                    self.feed.log(f"Found {len(new_posts)} new posts", subclass="polling")
                    
                    post_delay = self.config.get("POST_PROCESS_DELAY", 2.0)
                    
                    for post in reversed(new_posts):
                        raw = post.get("content") or post.get("text") or ""
                        text = self._strip_html(raw)
                        
                        if not text:
                            continue
                        
                        # Log post to feed
                        self.feed.log_post(post, action="received", subclass="posts")
                        
                        # Screen with Haiku
                        screen_result = self._screen_with_haiku(text)
                        
                        if screen_result["is_tariff_related"] and screen_result["confidence"] > 0.6:
                            url = post.get("url") or f"https://truthsocial.com/@{self.handle}/{post.get('id')}"
                            created_at = post.get("created_at") or ""
                            
                            self._print_newline(f"✓ TARIFF POST (conf={screen_result['confidence']:.2f}) → TACO analysis")
                            self.feed.log(
                                f"Tariff post detected (conf={screen_result['confidence']:.2f})",
                                subclass="screening",
                                data={"reasoning": screen_result['reasoning']}
                            )
                            self.feed.stat("tariff_posts_detected", 1, subclass="signals")
                            
                            evt = Event(
                                source=self.name,
                                title="TACO Analysis",
                                message="Analyzing tariff-related post...",
                                url=url,
                                created_at=created_at,
                                priority=0,
                                payload={
                                    "analyze": True,
                                    "text": text,
                                    "taco_mode": True,
                                    "screen_confidence": screen_result["confidence"],
                                }
                            )
                            self.publish(evt)
                            
                            if post_delay > 0:
                                time.sleep(post_delay)
                        else:
                            self._print_newline(f"✗ Not tariff-related (conf={screen_result['confidence']:.2f})")
                            self.feed.log(
                                f"Post skipped (conf={screen_result['confidence']:.2f})",
                                subclass="screening",
                                data={"reasoning": screen_result['reasoning']}
                            )
                        
                        pid = post.get("id")
                        if pid and (not last_seen or pid > last_seen):
                            last_seen = pid
                            self.state.set(last_seen, self.state_key_last)
                else:
                    self._print_newline(f"No new posts (last_seen={last_seen})")
                
                # Smart idle with updating display
                for remaining in range(self.poll_seconds, 0, -5):
                    self._print_updating(f"idle … {remaining}s left")
                    time.sleep(min(5, remaining))
                self._print_newline("")
                
            except Exception as e:
                self._print_newline(f"Error: {e}")
                self.feed.error("Loop error", exception=e)
                import traceback
                traceback.print_exc()
                time.sleep(30)