"""
Monitor Feed System - Optional structured logging for monitors

Usage in monitors:
    self.feed.log("Starting poll cycle")
    self.feed.log_post(post_data, action="screened")
    self.feed.log_api_call("anthropic", tokens=1234, cost=0.015)
    self.feed.stat("posts_analyzed", 1)  # increment counter
"""

import time
import json
import sqlite3
import threading
from typing import Any, Dict, Optional
from datetime import datetime
from pathlib import Path


class MonitorFeed:
    """
    Structured logging interface for monitors.
    All methods are no-ops if feed is disabled.
    """
    
    def __init__(self, monitor_name: str, storage: Optional['FeedStorage'] = None, enabled: bool = True):
        self.monitor_name = monitor_name
        self.storage = storage
        self.enabled = enabled and storage is not None
        
    def log(self, message: str, subclass: str = None, level: str = "INFO", data: Dict = None):
        """Log a general message to the feed"""
        if not self.enabled:
            return
        
        self.storage.add_log(
            monitor=self.monitor_name,
            subclass=subclass,
            level=level,
            message=message,
            data=data or {}
        )
    
    def log_post(self, post: Dict[str, Any], action: str, subclass: str = None):
        """Log a social media post being processed"""
        if not self.enabled:
            return
        
        post_id = post.get("id", "unknown")
        preview = (post.get("content") or post.get("text") or "")[:100]
        
        self.storage.add_log(
            monitor=self.monitor_name,
            subclass=subclass or "posts",
            level="INFO",
            message=f"Post {post_id}: {action}",
            data={
                "type": "post",
                "action": action,
                "post_id": post_id,
                "preview": preview,
                "url": post.get("url"),
                "created_at": post.get("created_at"),
            }
        )
        
        self.stat("posts_processed", 1, subclass=subclass)
    
    def log_api_call(self, provider: str, model: str = None, tokens: int = 0, 
                     cost: float = 0.0, duration: float = 0.0, subclass: str = None):
        """Log an API call with statistics"""
        if not self.enabled:
            return
        
        self.storage.add_log(
            monitor=self.monitor_name,
            subclass=subclass or "api",
            level="INFO",
            message=f"API call: {provider} ({model or 'unknown'})",
            data={
                "type": "api_call",
                "provider": provider,
                "model": model,
                "tokens": tokens,
                "cost": cost,
                "duration_sec": duration,
            }
        )
        
        # Update statistics
        self.stat("api_calls", 1, subclass=subclass)
        self.stat("tokens_used", tokens, subclass=subclass)
        self.stat("api_cost", cost, subclass=subclass)
    
    def stat(self, key: str, increment: float = 1, subclass: str = None):
        """Increment a statistic counter"""
        if not self.enabled:
            return
        
        self.storage.increment_stat(
            monitor=self.monitor_name,
            subclass=subclass,
            key=key,
            value=increment
        )
    
    def error(self, message: str, exception: Exception = None, subclass: str = None):
        """Log an error"""
        data = {}
        if exception:
            data["exception"] = str(exception)
            data["exception_type"] = type(exception).__name__
        
        self.log(message, subclass=subclass, level="ERROR", data=data)
        self.stat("errors", 1, subclass=subclass)


class FeedStorage:
    """
    SQLite-based storage for monitor feeds.
    Thread-safe, automatically creates schema.
    """
    
    def __init__(self, db_path: str = ".monitor_feeds.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Create tables if they don't exist"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    monitor TEXT NOT NULL,
                    subclass TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_monitor 
                ON logs(monitor, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_subclass 
                ON logs(monitor, subclass, timestamp DESC)
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS statistics (
                    monitor TEXT NOT NULL,
                    subclass TEXT,
                    key TEXT NOT NULL,
                    value REAL NOT NULL,
                    last_updated REAL NOT NULL,
                    PRIMARY KEY (monitor, subclass, key)
                )
            """)
            conn.commit()
            conn.close()
    
    def add_log(self, monitor: str, subclass: Optional[str], level: str, 
                message: str, data: Dict[str, Any]):
        """Add a log entry"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO logs (timestamp, monitor, subclass, level, message, data)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                time.time(),
                monitor,
                subclass,
                level,
                message,
                json.dumps(data) if data else None
            ))
            conn.commit()
            conn.close()
    
    def increment_stat(self, monitor: str, subclass: Optional[str], key: str, value: float):
        """Increment a statistic (creates if doesn't exist)"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            
            # Try to update existing
            cursor = conn.execute("""
                UPDATE statistics 
                SET value = value + ?, last_updated = ?
                WHERE monitor = ? AND subclass IS ? AND key = ?
            """, (value, time.time(), monitor, subclass, key))
            
            # If no rows updated, insert new
            if cursor.rowcount == 0:
                conn.execute("""
                    INSERT INTO statistics (monitor, subclass, key, value, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (monitor, subclass, key, value, time.time()))
            
            conn.commit()
            conn.close()
    
    def get_logs(self, monitor: str = None, subclass: str = None, 
                 limit: int = 100, level: str = None) -> list:
        """Query logs with filters"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            
            query = "SELECT * FROM logs WHERE 1=1"
            params = []
            
            if monitor:
                query += " AND monitor = ?"
                params.append(monitor)
            if subclass:
                query += " AND subclass = ?"
                params.append(subclass)
            if level:
                query += " AND level = ?"
                params.append(level)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            conn.close()
            
            return [
                {
                    "id": row[0],
                    "timestamp": row[1],
                    "monitor": row[2],
                    "subclass": row[3],
                    "level": row[4],
                    "message": row[5],
                    "data": json.loads(row[6]) if row[6] else {}
                }
                for row in rows
            ]
    
    def get_statistics(self, monitor: str = None) -> Dict[str, Any]:
        """Get all statistics for a monitor"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            
            if monitor:
                cursor = conn.execute("""
                    SELECT subclass, key, value, last_updated
                    FROM statistics WHERE monitor = ?
                """, (monitor,))
            else:
                cursor = conn.execute("""
                    SELECT monitor, subclass, key, value, last_updated
                    FROM statistics
                """)
            
            rows = cursor.fetchall()
            conn.close()
            
            if monitor:
                # Return nested dict: {subclass: {key: value}}
                result = {}
                for subclass, key, value, updated in rows:
                    if subclass not in result:
                        result[subclass] = {}
                    result[subclass][key] = {
                        "value": value,
                        "last_updated": updated
                    }
                return result
            else:
                # Return {monitor: {subclass: {key: value}}}
                result = {}
                for monitor_name, subclass, key, value, updated in rows:
                    if monitor_name not in result:
                        result[monitor_name] = {}
                    if subclass not in result[monitor_name]:
                        result[monitor_name][subclass] = {}
                    result[monitor_name][subclass][key] = {
                        "value": value,
                        "last_updated": updated
                    }
                return result
    
    def get_monitors(self) -> list:
        """Get list of all monitors that have logged"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT DISTINCT monitor FROM logs ORDER BY monitor")
            monitors = [row[0] for row in cursor.fetchall()]
            conn.close()
            return monitors


class NoOpFeed(MonitorFeed):
    """Feed that does nothing - used when feeds are disabled"""
    
    def __init__(self, monitor_name: str):
        super().__init__(monitor_name, storage=None, enabled=False)