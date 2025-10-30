"""
Optional GUI for Monitor Feeds
Run separately: python gui_feed_viewer.py

Features:
- Live view of all monitor logs
- Statistics dashboard
- Filter by monitor/subclass/level
- Auto-refresh
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import time
from datetime import datetime
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

try:
    from core.monitor_feed import FeedStorage
except ImportError:
    print("ERROR: Could not import FeedStorage")
    print("Make sure core/monitor_feed.py is installed")
    sys.exit(1)


class FeedViewerGUI:
    def __init__(self, db_path: str = ".monitor_feeds.db"):
        self.storage = FeedStorage(db_path)
        self.root = tk.Tk()
        self.root.title("TruthTrader Monitor Feeds")
        self.root.geometry("1200x800")
        
        # Auto-refresh settings
        self.auto_refresh = tk.BooleanVar(value=True)
        self.refresh_interval = 2000  # ms
        
        self._create_ui()
        self._start_auto_refresh()
    
    def _create_ui(self):
        """Build the UI"""
        # Top toolbar
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        
        ttk.Label(toolbar, text="Monitor:").pack(side=tk.LEFT, padx=5)
        
        self.monitor_var = tk.StringVar(value="All")
        self.monitor_combo = ttk.Combobox(toolbar, textvariable=self.monitor_var, width=20)
        self.monitor_combo.pack(side=tk.LEFT, padx=5)
        self.monitor_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs())
        
        ttk.Label(toolbar, text="Level:").pack(side=tk.LEFT, padx=5)
        
        self.level_var = tk.StringVar(value="All")
        level_combo = ttk.Combobox(toolbar, textvariable=self.level_var, 
                                    values=["All", "INFO", "ERROR", "WARNING"], width=10)
        level_combo.pack(side=tk.LEFT, padx=5)
        level_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs())
        
        ttk.Button(toolbar, text="Refresh", command=self.refresh_logs).pack(side=tk.LEFT, padx=5)
        
        ttk.Checkbutton(toolbar, text="Auto-refresh", variable=self.auto_refresh).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(toolbar, text="Clear", command=self.clear_display).pack(side=tk.LEFT, padx=5)
        
        # Status label
        self.status_label = ttk.Label(toolbar, text="")
        self.status_label.pack(side=tk.RIGHT, padx=5)
        
        # Main content - Notebook with tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Tab 1: Logs
        logs_frame = ttk.Frame(notebook)
        notebook.add(logs_frame, text="Logs")
        
        # Logs display
        self.logs_text = scrolledtext.ScrolledText(logs_frame, wrap=tk.WORD, 
                                                    font=("Consolas", 9))
        self.logs_text.pack(fill=tk.BOTH, expand=True)
        
        # Configure tags for different log levels
        self.logs_text.tag_config("ERROR", foreground="red")
        self.logs_text.tag_config("WARNING", foreground="orange")
        self.logs_text.tag_config("INFO", foreground="black")
        self.logs_text.tag_config("timestamp", foreground="gray")
        self.logs_text.tag_config("monitor", foreground="blue", font=("Consolas", 9, "bold"))
        
        # Tab 2: Statistics
        stats_frame = ttk.Frame(notebook)
        notebook.add(stats_frame, text="Statistics")
        
        self.stats_text = scrolledtext.ScrolledText(stats_frame, wrap=tk.WORD,
                                                     font=("Consolas", 10))
        self.stats_text.pack(fill=tk.BOTH, expand=True)
        
        # Initial load
        self.refresh_monitors()
        self.refresh_logs()
        self.refresh_stats()
    
    def refresh_monitors(self):
        """Update the monitor dropdown"""
        monitors = self.storage.get_monitors()
        values = ["All"] + monitors
        self.monitor_combo['values'] = values
    
    def refresh_logs(self):
        """Refresh the logs display"""
        monitor = None if self.monitor_var.get() == "All" else self.monitor_var.get()
        level = None if self.level_var.get() == "All" else self.level_var.get()
        
        logs = self.storage.get_logs(monitor=monitor, level=level, limit=200)
        
        self.logs_text.config(state=tk.NORMAL)
        self.logs_text.delete(1.0, tk.END)
        
        for log in reversed(logs):  # Show oldest first
            ts = datetime.fromtimestamp(log['timestamp']).strftime("%H:%M:%S")
            monitor_tag = f"[{log['monitor']}"
            if log['subclass']:
                monitor_tag += f"][{log['subclass']}"
            monitor_tag += "]"
            
            # Insert with tags
            self.logs_text.insert(tk.END, f"{ts} ", "timestamp")
            self.logs_text.insert(tk.END, f"{monitor_tag} ", "monitor")
            self.logs_text.insert(tk.END, f"{log['message']}\n", log['level'])
            
            # Add data if present and interesting
            if log['data'] and log['data'].get('type') in ['api_call', 'post']:
                data = log['data']
                if data['type'] == 'api_call':
                    detail = f"  → {data.get('tokens', 0)} tokens, ${data.get('cost', 0):.4f}, {data.get('duration_sec', 0):.1f}s\n"
                    self.logs_text.insert(tk.END, detail, "INFO")
                elif data['type'] == 'post':
                    preview = data.get('preview', '')[:80]
                    if preview:
                        self.logs_text.insert(tk.END, f"  → {preview}...\n", "INFO")
        
        self.logs_text.config(state=tk.DISABLED)
        self.logs_text.see(tk.END)  # Auto-scroll to bottom
        
        self.status_label.config(text=f"Showing {len(logs)} logs")
    
    def refresh_stats(self):
        """Refresh the statistics display"""
        all_stats = self.storage.get_statistics()
        
        self.stats_text.config(state=tk.NORMAL)
        self.stats_text.delete(1.0, tk.END)
        
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
                    
                    # Format value based on key
                    if 'cost' in key.lower():
                        formatted = f"${value:.4f}"
                    elif value == int(value):
                        formatted = f"{int(value):,}"
                    else:
                        formatted = f"{value:,.2f}"
                    
                    self.stats_text.insert(tk.END, f"    {key:30s} {formatted:>15s}  ({updated})\n")
                
                self.stats_text.insert(tk.END, "\n")
        
        self.stats_text.config(state=tk.DISABLED)
        
        # Configure tags
        self.stats_text.tag_config("header", foreground="blue", font=("Consolas", 10, "bold"))
        self.stats_text.tag_config("subheader", foreground="darkblue", font=("Consolas", 10, "bold"))
    
    def clear_display(self):
        """Clear the current display"""
        self.logs_text.config(state=tk.NORMAL)
        self.logs_text.delete(1.0, tk.END)
        self.logs_text.config(state=tk.DISABLED)
    
    def _start_auto_refresh(self):
        """Start the auto-refresh loop"""
        if self.auto_refresh.get():
            self.refresh_logs()
            self.refresh_stats()
            self.refresh_monitors()
        
        self.root.after(self.refresh_interval, self._start_auto_refresh)
    
    def run(self):
        """Start the GUI"""
        self.root.mainloop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor Feed Viewer GUI")
    parser.add_argument("--db", default=".monitor_feeds.db", 
                       help="Path to monitor feeds database")
    args = parser.parse_args()
    
    if not Path(args.db).exists():
        print(f"Database not found: {args.db}")
        print("Make sure monitors are running with feeds enabled.")
        print("Set ENABLE_MONITOR_FEEDS=true in your .env file")
        return
    
    gui = FeedViewerGUI(db_path=args.db)
    gui.run()


if __name__ == "__main__":
    main()