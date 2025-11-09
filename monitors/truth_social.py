import os, re, time, random, traceback, inspect, threading, json
from typing import Any, List, Optional
from .base import Monitor
from core.bus import Event

try:
    import truthbrush as tb
except Exception as e:
    tb = None
    print(f"[truthSocial] ERROR importing truthbrush: {e}", flush=True)

VERSION = "truth_social/2.2.1-threadsafe"
print(f"[truthSocial] module file -> {inspect.getfile(inspect.currentframe())}", flush=True)

_CLOUDFLARE_STRINGS = ("Access denied | truthsocial.com used Cloudflare", "Error 1015")
_HTML_TAGS = re.compile(r"<[^>]+>")


def _safe_print_exc(prefix: str, e: Exception):
    try:
        print(f"[truthSocial] {prefix}: {e}", flush=True)
        traceback.print_exc()
    except Exception:
        pass


def _is_cloudflare_html(payload: Any) -> bool:
    if isinstance(payload, str):
        return any(s in payload for s in _CLOUDFLARE_STRINGS)
    return False


def _strip_html(s: str) -> str:
    if not s:
        return ""
    if "<" in s and ">" in s:
        s = _HTML_TAGS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


class Monitor(Monitor):  # type: ignore[misc]
    name = "truthSocial"

    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        if tb is None:
            raise RuntimeError("truthbrush module not available")

        # Import Anthropic for Haiku screening
        try:
            from anthropic import Anthropic
            self.anthropic = Anthropic(api_key=config["ANTHROPIC_API_KEY"])
            self.enable_screening = os.getenv("TRUTH_SOCIAL_ENABLE_HAIKU_SCREENING", "1") != "0"
        except Exception as e:
            self._print(f"WARNING: Anthropic not available, disabling screening: {e}")
            self.anthropic = None
            self.enable_screening = False

        self.api = tb.Api()
        self.handle = config["TRUTH_HANDLE"]
        self.poll_seconds = max(30, int(os.getenv("TRUTH_SOCIAL_POLL_SECONDS", "90")))
        self.screening_model = os.getenv("TRUTH_SOCIAL_SCREENING_MODEL", "claude-haiku-4-5-20251001")
        self.state = ctx.get("state")
        self.state_key_last = "truth_social:last_seen_id"
        self.state_key_processed = "truth_social:processed_posts"  # Track what we have analyzed
        self.config = config

        try:
            self.publish_timeout_sec = int(os.getenv("PUBLISH_TIMEOUT_SEC", "25"))
        except Exception:
            self.publish_timeout_sec = 25

        # Heartbeat controls
        try:
            self.heartbeat_sec = int(os.getenv("TRUTH_SOCIAL_HEARTBEAT_SEC", "0"))
        except Exception:
            self.heartbeat_sec = 0
        self.heartbeat_push = os.getenv("TRUTH_SOCIAL_HEARTBEAT_PUSH", "0").strip() not in ("0", "", "false", "False")

        self._print(f"Haiku screening: {'ENABLED' if self.enable_screening else 'DISABLED'}")
        self.feed.log(f"Monitor initialized for @{self.handle}", subclass="lifecycle")
        self.feed.log(f"Screening: {self.screening_model if self.enable_screening else 'DISABLED'}", subclass="config")

    def _screen_with_haiku(self, text: str) -> dict:
        """Use Haiku to screen if post is market-relevant."""
        try:
            if not self.enable_screening or not self.anthropic:
                return {"is_market_relevant": True, "confidence": 0.5, "reasoning": "screening disabled"}

            t0 = time.time()

            system_prompt = (
                "You are a trading assistant screening social media posts. "
                "Identify if a post could impact financial markets. Respond with JSON only."
            )
            user_message = (
                "Is this post market-relevant for trading? Consider:\n"
                "- Trade policy, tariffs, regulations\n"
                "- Company mentions (Tesla, Apple, etc.)\n"
                "- Economic policy, Fed, taxes\n"
                "- Major political events affecting markets\n\n"
                "Skip: judge nominations, routine endorsements, celebrations, statistics without market impact.\n\n"
                'Respond JSON: {"is_market_relevant": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}\n\n'
                f"Post: {text[:500]}"
            )

            response = self.anthropic.messages.create(
                model=self.screening_model,
                max_tokens=200,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            duration = time.time() - t0

            # Extract text from response
            response_text = ""
            for block in getattr(response, "content", []) or []:
                # Newer Anthropic SDK objects have .type/.text
                if getattr(block, "type", None) == "text":
                    response_text += getattr(block, "text", "")

            response_text = (response_text or "").strip()
            if response_text.startswith("```"):
                response_text = re.sub(r"^```[a-zA-Z]*\s*", "", response_text)
                response_text = re.sub(r"\s*```$", "", response_text)

            result = json.loads(response_text)

            # Tokens and cost
            usage = getattr(response, "usage", None)
            in_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            tokens = in_tokens + out_tokens
            # Using a nominal $0.25 / 1M tokens placeholder you had
            cost = tokens * 0.25 / 1_000_000

            request_data = {
                "model": self.screening_model,
                "max_tokens": 200,
                "temperature": 0,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }

            response_data = {
                "content": [{"type": "text", "text": response_text}],
                "usage": {
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                },
                "stop_reason": getattr(response, "stop_reason", None),
                "model": getattr(response, "model", self.screening_model),
            }

            # Optional cache usage fields
            for attr in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                if hasattr(usage, attr):
                    response_data["usage"][attr] = getattr(usage, attr)

            self.feed.log_api_call(
                provider="anthropic",
                model=self.screening_model,
                tokens=tokens,
                cost=cost,
                duration=duration,
                subclass="screening",
                request_data=request_data,
                response_data=response_data,
            )
            return result

        except Exception as e:
            self._print(f"Haiku screening error: {e}")
            self.feed.error("Haiku screening failed", exception=e, subclass="screening")
            # Fall back to letting analyzer decide
            return {"is_market_relevant": True, "confidence": 0.5, "reasoning": "screening failed"}

    def _publish_with_timeout(self, evt: Event) -> bool:
        done = threading.Event()
        err: list[BaseException] = []

        def _run():
            try:
                self.publish(evt)
            except BaseException as e:
                err.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True, name="truthSocial:publish")
        t.start()
        finished = done.wait(timeout=max(1, self.publish_timeout_sec))
        if not finished:
            self._print(f"WARNING: publish timed out after {self.publish_timeout_sec}s")
            self.feed.error(f"Publish timeout after {self.publish_timeout_sec}s", subclass="publish")
            return False
        if err:
            _safe_print_exc("publish exception", err[0])
            self.feed.error("Publish exception", exception=err[0], subclass="publish")
            return False
        return True

    def _already_processed(self, post_id: str) -> bool:
        processed = self.state.get(self.state_key_processed, default={}) or {}
        return post_id in processed

    def _mark_processed(self, post_id: str):
        processed = self.state.get(self.state_key_processed, default={}) or {}
        if not isinstance(processed, dict):
            processed = {}
        processed[post_id] = time.time()
        # Keep only last 100 posts
        if len(processed) > 100:
            sorted_items = sorted(processed.items(), key=lambda x: x[1])
            processed = dict(sorted_items[-100:])
        self.state.set(processed, self.state_key_processed)

    def _fetch_new(self, since_id: Optional[str]) -> List[dict]:
        """Pull new statuses from truthbrush API, newest first."""
        new_posts: List[dict] = []
        try:
            page_iter = self.api.pull_statuses(
                username=self.handle,
                replies=False,
                verbose=False,
                created_after=None,
                since_id=since_id,
                pinned=False,
            )
            for i, post in enumerate(page_iter):
                # Catch Cloudflare rate-limit HTML
                if _is_cloudflare_html(post):
                    raise RuntimeError("Cloudflare 1015 HTML encountered")
                pid = post.get("id")
                if not pid:
                    continue
                if since_id and pid <= since_id:
                    break
                new_posts.append(post)
                if i > 25:  # safety cap
                    break
            return new_posts
        except Exception as e:
            msg = str(e)
            if "429" in msg or "1015" in msg or "rate limit" in msg.lower() or "Access denied" in msg:
                raise RuntimeError(f"rate-limit: {msg}")
            raise

    def _publish_post(self, post: dict) -> None:
        """Analyze/screen a single post and publish an Event accordingly."""
        try:
            sid = post.get("id")
            if not sid:
                self._print("Post missing id; skipping")
                self.feed.log("Skipped post without id", subclass="posts")
                return

            # Deduplicate across runs and overlapping feeds
            if self._already_processed(sid):
                self._print(f"Skipping already processed post {sid}")
                return

            url = post.get("url") or f"https://truthsocial.com/@{self.handle}/{sid}"
            created_at = post.get("created_at") or post.get("createdAt") or ""
            raw = post.get("content") or post.get("text") or post.get("spoiler_text") or ""
            text = _strip_html(raw)

            preview = (text or "").replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:117] + "..."

            self._print(f"Post {sid}: {preview or '(media-only)'}")
            self.feed.log_post(post, action="received", subclass="posts")

            # Media-only posts: skip analysis
            if not text:
                self._print("Media-only post, skipping analysis")
                self.feed.log("Skipped media-only post", subclass="posts")
                evt = Event(
                    source=self.name,
                    title="TruthTrader - update",
                    message="Media-only post (no text). No trade signal.",
                    url=url,
                    created_at=created_at,
                    priority=0,
                    payload={"analyze": False},
                )
                self._publish_with_timeout(evt)
                self._mark_processed(sid)
                return

            # Optional: screen with Haiku
            screen_result = self._screen_with_haiku(text)

            if screen_result.get("is_market_relevant") and screen_result.get("confidence", 0.0) > 0.6:
                self._print(f"[OK] MARKET-RELEVANT (conf={screen_result['confidence']:.2f}) -> analyzing")
                self.feed.log(
                    f"Post {sid}: RELEVANT (conf={screen_result['confidence']:.2f})",
                    subclass="screening",
                    data={"reasoning": screen_result.get("reasoning", "")},
                )
                evt = Event(
                    source=self.name,
                    title="TruthTrader - update",
                    message="Analyzing market-relevant post...",
                    url=url,
                    created_at=created_at,
                    priority=0,
                    payload={
                        "analyze": True,
                        "text": text,
                        "taco_mode": False,
                        "pre_screened": True,
                        "screen_confidence": screen_result["confidence"],
                    },
                )
            else:
                self._print(f"[X] Not market-relevant (conf={screen_result.get('confidence', 0.0):.2f}), skipping")
                self.feed.log(
                    f"Post {sid}: SKIPPED (conf={screen_result.get('confidence', 0.0):.2f})",
                    subclass="screening",
                    data={"reasoning": screen_result.get("reasoning", "")},
                )
                evt = Event(
                    source=self.name,
                    title="TruthTrader - update (not analyzed)",
                    message=f"Post not market-relevant (conf={screen_result.get('confidence', 0.0):.2f}):\n{text[:200]}...",
                    url=url,
                    created_at=created_at,
                    priority=0,
                    payload={"analyze": False},
                )

            self._publish_with_timeout(evt)
            self._mark_processed(sid)

        except Exception as e:
            _safe_print_exc("publish_post error", e)
            self.feed.error("Post processing failed", exception=e, subclass="posts")

    def run(self) -> None:
        self._print(f"RUN START - {VERSION}")
        self.feed.log(f"Monitor starting - {VERSION}", subclass="lifecycle")

        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        self._print(f"Monitoring @{self.handle} | poll={self.poll_seconds}s | last_seen={last_seen}")

        next_heartbeat_ts = time.time() + max(0, self.heartbeat_sec) if self.heartbeat_sec > 0 else float("inf")

        # Bootstrap
        if not last_seen:
            self._print("Bootstrap -> fetching newest page")
            self.feed.log("Bootstrap: fetching initial posts", subclass="lifecycle")

            try:
                first_page = self._fetch_new(None)
                self._print(f"Bootstrap -> got {len(first_page) if first_page else 0} post(s)")

                if first_page:
                    latest = first_page[0]
                    self._publish_post(latest)
                    last_seen = latest.get("id")
                    if last_seen:
                        try:
                            self.state.set(last_seen, self.state_key_last)
                            self._print(f"Bootstrap -> state saved (last_seen={last_seen})")
                            self.feed.log(f"Bootstrap complete: last_seen={last_seen}", subclass="lifecycle")
                        except Exception as se:
                            _safe_print_exc("state.set error (bootstrap)", se)
                            self.feed.error("State save failed (bootstrap)", exception=se, subclass="lifecycle")
            except Exception as e:
                _safe_print_exc("bootstrap fetch error", e)
                self.feed.error("Bootstrap failed", exception=e, subclass="lifecycle")

        self._print("Entering main poll loop")

        while True:
            try:
                self._print("Poll cycle")
                self.feed.log("Poll cycle starting", subclass="polling")

                new_posts = self._fetch_new(last_seen)

                if new_posts:
                    self._print(f"Found {len(new_posts)} new post(s)")
                    self.feed.log(f"Found {len(new_posts)} new posts", subclass="polling")

                    post_delay = float(self.config.get("POST_PROCESS_DELAY", 2.0))

                    for post in reversed(new_posts):
                        try:
                            self._publish_post(post)
                            if post_delay > 0:
                                time.sleep(post_delay)
                        except Exception as pe:
                            _safe_print_exc("publish_post error", pe)
                            self.feed.error("Post processing failed", exception=pe, subclass="posts")

                        pid = post.get("id")
                        if pid and (not last_seen or pid > last_seen):
                            last_seen = pid
                            try:
                                self.state.set(last_seen, self.state_key_last)
                            except Exception as se:
                                _safe_print_exc("state.set error", se)
                                self.feed.error("State save failed", exception=se, subclass="lifecycle")
                else:
                    self._print("No new posts")

                # Heartbeat
                now = time.time()
                if now >= next_heartbeat_ts:
                    if self.heartbeat_push:
                        self._print("Heartbeat publish")
                        self._publish_with_timeout(
                            Event(
                                source=self.name,
                                title="Truth Social heartbeat",
                                message=f"Alive. last_seen={last_seen or '(none)'}",
                                priority=0,
                                payload={"analyze": False},
                            )
                        )
                    self.feed.log(f"Heartbeat: last_seen={last_seen}", subclass="lifecycle")
                    next_heartbeat_ts = now + (max(5, self.heartbeat_sec) if self.heartbeat_sec > 0 else float("inf"))

                # Idle with jitter
                jitter = int(random.uniform(-max(5, self.poll_seconds // 8), max(5, self.poll_seconds // 8)))
                sleep_s = max(30, self.poll_seconds + jitter)
                self._print(f"Idle {sleep_s}s")
                self.feed.log(f"Idle {sleep_s}s", subclass="polling")
                time.sleep(sleep_s)

            except RuntimeError as rte:
                msg = str(rte)
                if "rate-limit" in msg.lower() or "429" in msg:
                    cool = random.randint(240, 480)
                    self._print(f"Rate limited -> cooling {cool}s")
                    self.feed.log(f"Rate limited: cooling {cool}s", subclass="errors")
                    time.sleep(cool)
                    continue
                _safe_print_exc("runtime error", rte)
                self.feed.error("Runtime error", exception=rte)
                time.sleep(10)
            except Exception as e:
                _safe_print_exc("ERROR loop", e)
                self.feed.error("Loop error", exception=e)
                time.sleep(10)
