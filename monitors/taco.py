"""
TACO Monitor: Trump Always Chickens Out
Uses Haiku for cheap, intelligent screening of tariff-related posts.
"""
import os, re, time, inspect, traceback
from typing import Optional
from .base import Monitor
from core.bus import Event

VERSION = "taco/2.2.0-threadsafe"
print(f"[taco] module file -> {inspect.getfile(inspect.currentframe())}", flush=True)

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
        self.state_key_processed = "taco:processed_posts"  # Track what we've analyzed
        self.config = config
        
        self._print(f"initialized for @{self.handle} | poll={self.poll_seconds}s | screening={self.screening_model}")
        
        self.feed.log(f"Monitor initialized for @{self.handle}", subclass="lifecycle")
        self.feed.log(f"Screening model: {self.screening_model}", subclass="config")
    
    def _strip_html(self, s: str) -> str:
        if not s:
            return ""
        if "<" in s and ">" in s:
            s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    
    def _already_processed(self, post_id: str) -> bool:
        """Check if we've already processed this post"""
        processed = self.state.get(self.state_key_processed, default={}) or {}
        return post_id in processed
    
    def _mark_processed(self, post_id: str):
        """Mark post as processed and clean old entries (keep last 100)"""
        processed = self.state.get(self.state_key_processed, default={}) or {}
        if not isinstance(processed, dict):
            processed = {}
        
        processed[post_id] = time.time()
        
        # Keep only last 100 posts to prevent unbounded growth
        if len(processed) > 100:
            sorted_items = sorted(processed.items(), key=lambda x: x[1])
            processed = dict(sorted_items[-100:])
        
        self.state.set(processed, self.state_key_processed)
    
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
            self._print(f"Haiku screening error: {e}")
            self.feed.error("Screening failed", exception=e, subclass="screening")
            # Fallback to simple keyword check
            text_lower = text.lower()
            is_related = any(kw in text_lower for kw in ["tariff", "trade war", "trade deal"])
            return {"is_tariff_related": is_related, "confidence": 0.5, "reasoning": "fallback"}
    
    def run(self) -> None:
        self._print(f"RUN START - {VERSION}")
        self.feed.log(f"Monitor starting - {VERSION}", subclass="lifecycle")
        
        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        self._print(f"Monitoring @{self.handle} for tariff posts | last_seen={last_seen}")
        
        # Bootstrap
        if not last_seen:
            self._print("Bootstrap -> fetching recent posts")
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
                            
                            self._print(f"OK Bootstrap: TARIFF POST (conf={screen_result['confidence']:.2f})")
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
                            self._print(f"[X] Bootstrap: not tariff-related (conf={screen_result['confidence']:.2f})")
                    
                    last_seen = latest["id"]
                    self.state.set(last_seen, self.state_key_last)
                    self._print(f"Bootstrap complete | last_seen={last_seen}")
                    self.feed.log(f"Bootstrap complete: last_seen={last_seen}", subclass="lifecycle")
            except Exception as e:
                self._print(f"Bootstrap error: {e}")
                traceback.print_exc()
                self.feed.error("Bootstrap failed", exception=e, subclass="lifecycle")
        
        # Main loop
        while True:
            try:
                self._print("Poll cycle")
                self.feed.log("Poll cycle starting", subclass="polling")

                try:
                    page_iter = self.api.pull_statuses(
                        username=self.handle, replies=False, verbose=False,
                        created_after=None, since_id=last_seen, pinned=False,
                    )
                except Exception as e:
                    self._print(f"Truthbrush fetch failed: {e}")
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
                    self._print(f"Found {len(new_posts)} new post(s)")
                    self.feed.log(f"Found {len(new_posts)} new posts", subclass="polling")
                    
                    post_delay = self.config.get("POST_PROCESS_DELAY", 2.0)
                    
                    for post in reversed(new_posts):
                        pid = post.get("id")
                        
                        # Skip if already processed (handles overlap with truth_social)
                        if self._already_processed(pid):
                            self._print(f"Skipping already processed post {pid}")
                            continue
                        
                        raw = post.get("content") or post.get("text") or ""
                        text = self._strip_html(raw)
                        
                        if not text:
                            self._mark_processed(pid)
                            continue
                        
                        # Log post to feed
                        self.feed.log_post(post, action="received", subclass="posts")
                        
                        # Screen with Haiku
                        screen_result = self._screen_with_haiku(text)
                        
                        if screen_result["is_tariff_related"] and screen_result["confidence"] > 0.6:
                            url = post.get("url") or f"https://truthsocial.com/@{self.handle}/{post.get('id')}"
                            created_at = post.get("created_at") or ""
                            
                            self._print(f"[OK] TARIFF POST (conf={screen_result['confidence']:.2f}) -> TACO analysis")
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
                            self._print(f"[X] Not tariff-related (conf={screen_result['confidence']:.2f})")
                            self.feed.log(
                                f"Post skipped (conf={screen_result['confidence']:.2f})",
                                subclass="screening",
                                data={"reasoning": screen_result['reasoning']}
                            )
                        
                        # Mark as processed regardless of whether we analyzed it
                        self._mark_processed(pid)
                        
                        # Update last_seen for API pagination
                        if pid and (not last_seen or pid > last_seen):
                            last_seen = pid
                            self.state.set(last_seen, self.state_key_last)
                else:
                    self._print("No new posts")
                
                # Idle
                self._print(f"Idle {self.poll_seconds}s")
                self.feed.log(f"Idle {self.poll_seconds}s", subclass="polling")
                time.sleep(self.poll_seconds)
                
            except Exception as e:
                self._print(f"Error: {e}")
                self.feed.error("Loop error", exception=e)
                traceback.print_exc()
                time.sleep(30)
