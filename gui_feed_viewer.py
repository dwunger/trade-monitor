"""
Monitor Dashboard - Real-time monitoring of TruthTrader monitors
Run: python gui_feed_viewer.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

try:
    from core.monitor_feed import FeedStorage
except ImportError:
    print("ERROR: Could not import FeedStorage")
    print("Make sure core/monitor_feed.py is installed")
    sys.exit(1)


class MonitorDashboard:
    def __init__(self, db_path: str = ".monitor_feeds.db"):
        self.storage = FeedStorage(db_path)
        self.root = tk.Tk()
        self.root.title("TruthTrader Monitor Dashboard")
        self.root.geometry("1400x900")
        
        # Colors
        self.bg_color = "#f0f0f0"
        self.card_bg = "#ffffff"
        self.accent_color = "#2196F3"
        self.success_color = "#4CAF50"
        self.warning_color = "#FF9800"
        self.error_color = "#F44336"
        
        self.root.configure(bg=self.bg_color)
        
        # Auto-refresh
        self.auto_refresh = tk.BooleanVar(value=True)
        self.refresh_interval = 2000  # 2 seconds
        
        self._create_ui()
        self._start_auto_refresh()
    
    def _create_ui(self):
        """Build the dashboard UI"""
        # Top toolbar
        toolbar = tk.Frame(self.root, bg=self.bg_color, height=50)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        
        title_label = tk.Label(
            toolbar, 
            text=" TruthTrader Monitor Dashboard",
            font=("Arial", 16, "bold"),
            bg=self.bg_color,
            fg="#333333"
        )
        title_label.pack(side=tk.LEFT, padx=10)
        
        ttk.Button(toolbar, text=" Refresh", command=self.refresh_all).pack(side=tk.RIGHT, padx=5)
        ttk.Checkbutton(toolbar, text="Auto-refresh", variable=self.auto_refresh).pack(side=tk.RIGHT, padx=5)
        
        self.status_label = tk.Label(toolbar, text="", bg=self.bg_color, fg="#666")
        self.status_label.pack(side=tk.RIGHT, padx=10)
        
        # Main content - Notebook
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Tab 1: Dashboard (main view)
        self._create_dashboard_tab(notebook)
        
        # Tab 2: Detailed Logs
        self._create_logs_tab(notebook)
        
        # Tab 3: Statistics
        self._create_stats_tab(notebook)
        
        # Initial refresh
        self.refresh_all()
    
    def _create_dashboard_tab(self, notebook):
        """Create the main dashboard view"""
        dashboard_frame = tk.Frame(notebook, bg=self.bg_color)
        notebook.add(dashboard_frame, text=" Dashboard")
        
        # Top metrics bar
        metrics_frame = tk.Frame(dashboard_frame, bg=self.bg_color)
        metrics_frame.pack(fill=tk.X, padx=10, pady=10)
        
        # Create metric cards
        self.total_posts_card = self._create_metric_card(metrics_frame, " Posts Processed", "0", self.accent_color)
        self.total_posts_card.pack(side=tk.LEFT, padx=5)
        
        self.api_calls_card = self._create_metric_card(metrics_frame, " API Calls", "0", self.success_color)
        self.api_calls_card.pack(side=tk.LEFT, padx=5)
        
        self.tokens_card = self._create_metric_card(metrics_frame, " Tokens Used", "0", self.accent_color)
        self.tokens_card.pack(side=tk.LEFT, padx=5)
        
        self.cost_card = self._create_metric_card(metrics_frame, " Total Cost", "$0.00", self.success_color)
        self.cost_card.pack(side=tk.LEFT, padx=5)
        
        self.errors_card = self._create_metric_card(metrics_frame, " Errors", "0", self.error_color)
        self.errors_card.pack(side=tk.LEFT, padx=5)
        
        # Monitor status cards
        monitors_frame = tk.Frame(dashboard_frame, bg=self.bg_color)
        monitors_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Scrollable area for monitor cards
        canvas = tk.Canvas(monitors_frame, bg=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(monitors_frame, orient="vertical", command=canvas.yview)
        self.monitors_container = tk.Frame(canvas, bg=self.bg_color)
        
        self.monitors_container.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=self.monitors_container, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Store reference for updates
        self.monitor_cards = {}
    
    def _create_metric_card(self, parent, title, value, color):
        """Create a metric card widget"""
        card = tk.Frame(parent, bg=self.card_bg, relief=tk.RAISED, borderwidth=1)
        card.pack_propagate(False)
        card.configure(width=200, height=100)
        
        title_label = tk.Label(
            card,
            text=title,
            font=("Arial", 10),
            bg=self.card_bg,
            fg="#666666"
        )
        title_label.pack(pady=(15, 5))
        
        value_label = tk.Label(
            card,
            text=value,
            font=("Arial", 20, "bold"),
            bg=self.card_bg,
            fg=color
        )
        value_label.pack()
        
        # Store reference to value label for updates
        card.value_label = value_label
        
        return card
    
    def _create_monitor_card(self, parent, monitor_name, stats):
        """Create a monitor status card"""
        card = tk.Frame(parent, bg=self.card_bg, relief=tk.RAISED, borderwidth=1)
        card.pack(fill=tk.X, pady=5)
        
        # Header
        header = tk.Frame(card, bg=self.accent_color)
        header.pack(fill=tk.X)
        
        monitor_label = tk.Label(
            header,
            text=f" {monitor_name.upper()}",
            font=("Arial", 12, "bold"),
            bg=self.accent_color,
            fg="white"
        )
        monitor_label.pack(side=tk.LEFT, padx=10, pady=5)
        
        # Last activity
        last_activity = self._get_last_activity(monitor_name)
        activity_label = tk.Label(
            header,
            text=last_activity,
            font=("Arial", 9),
            bg=self.accent_color,
            fg="white"
        )
        activity_label.pack(side=tk.RIGHT, padx=10, pady=5)
        
        # Stats grid
        stats_frame = tk.Frame(card, bg=self.card_bg)
        stats_frame.pack(fill=tk.X, padx=15, pady=10)
        
        # Organize stats by subclass
        row = 0
        for subclass, metrics in sorted(stats.items()):
            if subclass:
                subclass_label = tk.Label(
                    stats_frame,
                    text=f"[{subclass}]",
                    font=("Arial", 9, "bold"),
                    bg=self.card_bg,
                    fg=self.accent_color
                )
                subclass_label.grid(row=row, column=0, sticky="w", pady=(5, 2))
                row += 1
            
            for key, data in sorted(metrics.items()):
                value = data['value']
                
                # Format value
                if 'cost' in key.lower():
                    formatted = f"${value:.4f}"
                    color = self.success_color
                elif 'error' in key.lower():
                    formatted = f"{int(value):,}"
                    color = self.error_color
                elif value == int(value):
                    formatted = f"{int(value):,}"
                    color = "#333333"
                else:
                    formatted = f"{value:,.2f}"
                    color = "#333333"
                
                # Key label
                key_label = tk.Label(
                    stats_frame,
                    text=f"  * {key}:",
                    font=("Arial", 9),
                    bg=self.card_bg,
                    fg="#666666"
                )
                key_label.grid(row=row, column=0, sticky="w", padx=(10, 0))
                
                # Value label
                value_label = tk.Label(
                    stats_frame,
                    text=formatted,
                    font=("Arial", 9, "bold"),
                    bg=self.card_bg,
                    fg=color
                )
                value_label.grid(row=row, column=1, sticky="e", padx=(20, 0))
                
                row += 1
        
        return card
    
    def _get_last_activity(self, monitor_name):
        """Get last activity timestamp for a monitor"""
        logs = self.storage.get_logs(monitor=monitor_name, limit=1)
        if logs:
            timestamp = logs[0]['timestamp']
            dt = datetime.fromtimestamp(timestamp)
            delta = datetime.now() - dt
            
            if delta < timedelta(minutes=1):
                return f"Active {int(delta.total_seconds())}s ago"
            elif delta < timedelta(hours=1):
                return f"Active {int(delta.total_seconds() / 60)}m ago"
            else:
                return f"Active {int(delta.total_seconds() / 3600)}h ago"
        return "No activity"
    
    def _create_logs_tab(self, notebook):
        """Create detailed logs tab"""
        logs_frame = tk.Frame(notebook, bg=self.bg_color)
        notebook.add(logs_frame, text=" Logs")
        
        # Filters
        filter_frame = tk.Frame(logs_frame, bg=self.bg_color)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Label(filter_frame, text="Monitor:", bg=self.bg_color).pack(side=tk.LEFT, padx=5)
        self.monitor_var = tk.StringVar(value="All")
        self.monitor_combo = ttk.Combobox(filter_frame, textvariable=self.monitor_var, width=20)
        self.monitor_combo.pack(side=tk.LEFT, padx=5)
        self.monitor_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs())
        
        tk.Label(filter_frame, text="Level:", bg=self.bg_color).pack(side=tk.LEFT, padx=5)
        self.level_var = tk.StringVar(value="All")
        level_combo = ttk.Combobox(filter_frame, textvariable=self.level_var, 
                                    values=["All", "INFO", "ERROR", "WARNING"], width=10)
        level_combo.pack(side=tk.LEFT, padx=5)
        level_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs())
        
        # Logs display
        self.logs_text = scrolledtext.ScrolledText(logs_frame, wrap=tk.WORD, 
                                                    font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.logs_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Configure tags
        self.logs_text.tag_config("ERROR", foreground="#f44336")
        self.logs_text.tag_config("WARNING", foreground="#ff9800")
        self.logs_text.tag_config("INFO", foreground="#d4d4d4")
        self.logs_text.tag_config("timestamp", foreground="#888888")
        self.logs_text.tag_config("monitor", foreground="#2196f3", font=("Consolas", 9, "bold"))
    
    def _create_stats_tab(self, notebook):
        """Create statistics tab"""
        stats_frame = tk.Frame(notebook, bg=self.bg_color)
        notebook.add(stats_frame, text=" Statistics")
        
        self.stats_text = scrolledtext.ScrolledText(stats_frame, wrap=tk.WORD,
                                                     font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4")
        self.stats_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.stats_text.tag_config("header", foreground="#2196f3", font=("Consolas", 12, "bold"))
        self.stats_text.tag_config("subheader", foreground="#4caf50", font=("Consolas", 10, "bold"))
    
    def refresh_all(self):
        """Refresh all dashboard components"""
        self.refresh_dashboard()
        self.refresh_logs()
        self.refresh_stats()
        self.status_label.config(text=f"Updated: {datetime.now().strftime('%H:%M:%S')}")
    
    def refresh_dashboard(self):
        """Refresh the main dashboard"""
        all_stats = self.storage.get_statistics()
        monitors = self.storage.get_monitors()
        
        # Calculate totals
        total_posts = 0
        total_api_calls = 0
        total_tokens = 0
        total_cost = 0.0
        total_errors = 0
        
        for monitor_stats in all_stats.values():
            for subclass_stats in monitor_stats.values():
                total_posts += subclass_stats.get('posts_processed', {}).get('value', 0)
                total_api_calls += subclass_stats.get('api_calls', {}).get('value', 0)
                total_tokens += subclass_stats.get('tokens_used', {}).get('value', 0)
                total_cost += subclass_stats.get('api_cost', {}).get('value', 0)
                total_errors += subclass_stats.get('errors', {}).get('value', 0)
        
        # Update metric cards
        self.total_posts_card.value_label.config(text=f"{int(total_posts):,}")
        self.api_calls_card.value_label.config(text=f"{int(total_api_calls):,}")
        self.tokens_card.value_label.config(text=f"{int(total_tokens):,}")
        self.cost_card.value_label.config(text=f"${total_cost:.4f}")
        self.errors_card.value_label.config(
            text=f"{int(total_errors):,}",
            fg=self.error_color if total_errors > 0 else self.success_color
        )
        
        # Update monitor cards
        for widget in self.monitors_container.winfo_children():
            widget.destroy()
        
        self.monitor_cards = {}
        
        if not monitors:
            no_data = tk.Label(
                self.monitors_container,
                text="No monitors active yet. Make sure ENABLE_MONITOR_FEEDS=true in your .env",
                font=("Arial", 12),
                bg=self.bg_color,
                fg="#666666"
            )
            no_data.pack(pady=50)
        else:
            for monitor in sorted(monitors):
                stats = all_stats.get(monitor, {})
                card = self._create_monitor_card(self.monitors_container, monitor, stats)
                self.monitor_cards[monitor] = card
    
    def refresh_logs(self):
        """Refresh the logs display"""
        monitor = None if self.monitor_var.get() == "All" else self.monitor_var.get()
        level = None if self.level_var.get() == "All" else self.level_var.get()
        
        logs = self.storage.get_logs(monitor=monitor, level=level, limit=200)
        
        self.logs_text.config(state=tk.NORMAL)
        self.logs_text.delete(1.0, tk.END)
        
        for log in reversed(logs):
            ts = datetime.fromtimestamp(log['timestamp']).strftime("%H:%M:%S")
            monitor_tag = f"[{log['monitor']}"
            if log['subclass']:
                monitor_tag += f"][{log['subclass']}"
            monitor_tag += "]"
            
            self.logs_text.insert(tk.END, f"{ts} ", "timestamp")
            self.logs_text.insert(tk.END, f"{monitor_tag} ", "monitor")
            self.logs_text.insert(tk.END, f"{log['message']}\n", log['level'])
            
            if log['data'] and log['data'].get('type') in ['api_call', 'post']:
                data = log['data']
                if data['type'] == 'api_call':
                    detail = f"  -> {data.get('tokens', 0)} tokens, ${data.get('cost', 0):.4f}, {data.get('duration_sec', 0):.1f}s\n"
                    self.logs_text.insert(tk.END, detail, "INFO")
                elif data['type'] == 'post':
                    preview = data.get('preview', '')[:80]
                    if preview:
                        self.logs_text.insert(tk.END, f"  -> {preview}...\n", "INFO")
        
        self.logs_text.config(state=tk.DISABLED)
        self.logs_text.see(tk.END)
        
        # Update monitor dropdown
        monitors = self.storage.get_monitors()
        self.monitor_combo['values'] = ["All"] + monitors
    
    def refresh_stats(self):
        """Refresh the statistics display"""
        all_stats = self.storage.get_statistics()
        
        self.stats_text.config(state=tk.NORMAL)
        self.stats_text.delete(1.0, tk.END)
        
        if not all_stats:
            self.stats_text.insert(tk.END, "\nNo statistics yet.\n\n", "INFO")
            self.stats_text.insert(tk.END, "Make sure:\n", "INFO")
            self.stats_text.insert(tk.END, "1. ENABLE_MONITOR_FEEDS=true in .env\n", "INFO")
            self.stats_text.insert(tk.END, "2. Monitors are running\n", "INFO")
            self.stats_text.insert(tk.END, "3. Monitors are calling feed methods\n", "INFO")
        else:
            for monitor, subclasses in sorted(all_stats.items()):
                self.stats_text.insert(tk.END, f"\n{'='*60}\n", "header")
                self.stats_text.insert(tk.END, f"  {monitor.upper()}\n", "header")
                self.stats_text.insert(tk.END, f"{'='*60}\n\n", "header")
                
                for subclass, stats in sorted(subclasses.items()):
                    subclass_name = subclass if subclass else "(general)"
                    self.stats_text.insert(tk.END, f"  [{subclass_name}]\n", "subheader")
                    
                    for key, data in sorted(stats.items()):
                        value = data['value']
                        updated = datetime.fromtimestamp(data['last_updated']).strftime("%Y-%m-%d %H:%M:%S")
                        
                        if 'cost' in key.lower():
                            formatted = f"${value:.4f}"
                        elif value == int(value):
                            formatted = f"{int(value):,}"
                        else:
                            formatted = f"{value:,.2f}"
                        
                        self.stats_text.insert(tk.END, f"    {key:30s} {formatted:>15s}  ({updated})\n")
                    
                    self.stats_text.insert(tk.END, "\n")
        
        self.stats_text.config(state=tk.DISABLED)
    
    def _start_auto_refresh(self):
        """Start auto-refresh loop"""
        if self.auto_refresh.get():
            self.refresh_all()
        self.root.after(self.refresh_interval, self._start_auto_refresh)
    
    def run(self):
        """Start the dashboard"""
        self.root.mainloop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TruthTrader Monitor Dashboard")
    parser.add_argument("--db", default=".monitor_feeds.db", 
                       help="Path to monitor feeds database")
    args = parser.parse_args()
    
    if not Path(args.db).exists():
        print(f"Database not found: {args.db}")
        print("\nCreating empty database...")
        print("Once monitors start running, data will appear here.")
    
    dashboard = MonitorDashboard(db_path=args.db)
    dashboard.run()


if __name__ == "__main__":
    main()