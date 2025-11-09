#!/usr/bin/env python3
"""
TruthTrader Dashboard - Flask web interface for monitoring system activity
Run: python dashboard.py
Access: http://localhost:5000
"""

import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from typing import Dict, List, Any

# Try to import the existing feed storage
try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from core.monitor_feed import FeedStorage
except ImportError:
    print("ERROR: Could not import FeedStorage. Make sure core/monitor_feed.py exists.")
    sys.exit(1)


app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Database path
DB_PATH = os.getenv("MONITOR_FEEDS_DB", ".monitor_feeds.db")
storage = FeedStorage(DB_PATH)


def format_timestamp(ts: float) -> str:
    """Convert Unix timestamp to readable format"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def get_monitor_summary(monitor: str) -> Dict[str, Any]:
    """Get summary statistics for a monitor"""
    stats = storage.get_statistics(monitor)
    logs = storage.get_logs(monitor=monitor, limit=1)
    
    # Calculate totals across all subclasses
    totals = {
        "posts_processed": 0,
        "api_calls": 0,
        "tokens_used": 0,
        "api_cost": 0.0,
        "errors": 0,
    }
    
    for subclass_stats in stats.values():
        for key, data in subclass_stats.items():
            if key in totals:
                totals[key] += data['value']
    
    last_activity = None
    if logs:
        last_activity = format_timestamp(logs[0]['timestamp'])
    
    return {
        "name": monitor,
        "stats": stats,
        "totals": totals,
        "last_activity": last_activity,
    }


@app.route('/')
def index():
    """Homepage - list all monitors"""
    monitors = storage.get_monitors()
    
    if not monitors:
        return render_template('index.html', monitors=[], no_data=True)
    
    monitor_summaries = [get_monitor_summary(m) for m in sorted(monitors)]
    
    return render_template('index.html', monitors=monitor_summaries, no_data=False)


@app.route('/monitor/<monitor_name>')
def monitor_detail(monitor_name: str):
    """Detailed view for a specific monitor"""
    stats = storage.get_statistics(monitor_name)
    all_logs = storage.get_logs(monitor=monitor_name, limit=500)
    
    # Calculate totals
    totals = {
        "posts_processed": 0,
        "api_calls": 0,
        "tokens_used": 0,
        "api_cost": 0.0,
        "errors": 0,
    }
    
    for subclass_stats in stats.values():
        for key, data in subclass_stats.items():
            if key in totals:
                totals[key] += data['value']
    
    # Filter logs to show only screening and analysis (no lifecycle/config/bootstrap)
    relevant_subclasses = {'screening', 'analysis', 'analysis_escalation', 'macro_analysis'}
    
    # Format logs for display (filtered)
    formatted_logs = []
    for log in all_logs:
        # Only show logs from relevant subclasses
        if log['subclass'] not in relevant_subclasses:
            continue
            
        formatted_log = {
            "id": log['id'],
            "timestamp": format_timestamp(log['timestamp']),
            "subclass": log['subclass'] or "(general)",
            "level": log['level'],
            "message": log['message'],
            "has_details": bool(log.get('data') and log['data'].get('type') in ['api_call', 'post']),
        }
        formatted_logs.append(formatted_log)
    
    # Limit to most recent 200 after filtering
    formatted_logs = formatted_logs[:200]
    
    return render_template(
        'monitor.html',
        monitor_name=monitor_name,
        stats=stats,
        totals=totals,
        logs=formatted_logs
    )


@app.route('/api/log/<int:log_id>')
def log_detail(log_id: int):
    """API endpoint to get detailed log data"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT * FROM logs WHERE id = ?", (log_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "Log not found"}), 404
    
    log = {
        "id": row[0],
        "timestamp": format_timestamp(row[1]),
        "monitor": row[2],
        "subclass": row[3],
        "level": row[4],
        "message": row[5],
        "data": json.loads(row[6]) if row[6] else {}
    }
    
    return jsonify(log)


@app.route('/log/<int:log_id>/full')
def log_full_view(log_id: int):
    """Full page view for API call details"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT * FROM logs WHERE id = ?", (log_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return "Log not found", 404
    
    log = {
        "id": row[0],
        "timestamp": format_timestamp(row[1]),
        "monitor": row[2],
        "subclass": row[3],
        "level": row[4],
        "message": row[5],
        "data": json.loads(row[6]) if row[6] else {}
    }
    
    return render_template('log_detail.html', log=log)


@app.route('/api/stats')
def api_stats():
    """API endpoint for real-time statistics updates"""
    monitor = request.args.get('monitor')
    
    if monitor:
        stats = storage.get_statistics(monitor)
        return jsonify(stats)
    else:
        all_stats = storage.get_statistics()
        return jsonify(all_stats)


@app.template_filter('format_number')
def format_number(value):
    """Format numbers with commas"""
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{int(value):,}"


@app.template_filter('format_cost')
def format_cost(value):
    """Format cost as currency"""
    return f"${value:,.4f}"


def create_templates():
    """Create HTML templates"""
    templates_dir = Path(__file__).parent / "templates"
    templates_dir.mkdir(exist_ok=True)
    
    # Base template
    (templates_dir / "base.html").write_text("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}TruthTrader Dashboard{% endblock %}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 1.5rem 2rem;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .header h1 {
            font-size: 1.8rem;
            font-weight: 600;
        }
        
        .header a {
            color: white;
            text-decoration: none;
        }
        
        .header a:hover {
            text-decoration: underline;
        }
        
        .container {
            max-width: 1400px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        
        .card {
            background: white;
            border-radius: 8px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .card h2 {
            font-size: 1.4rem;
            margin-bottom: 1rem;
            color: #667eea;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        
        .stat-card {
            background: white;
            border-radius: 8px;
            padding: 1.5rem;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .stat-card .label {
            font-size: 0.9rem;
            color: #666;
            margin-bottom: 0.5rem;
        }
        
        .stat-card .value {
            font-size: 2rem;
            font-weight: bold;
            color: #667eea;
        }
        
        .stat-card .value.cost {
            color: #10b981;
        }
        
        .stat-card .value.error {
            color: #ef4444;
        }
        
        .monitor-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 1.5rem;
        }
        
        .monitor-card {
            background: white;
            border-radius: 8px;
            padding: 1.5rem;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
            cursor: pointer;
        }
        
        .monitor-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .monitor-card h3 {
            font-size: 1.3rem;
            margin-bottom: 1rem;
            color: #667eea;
        }
        
        .monitor-card .stat-row {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid #f0f0f0;
        }
        
        .monitor-card .stat-row:last-child {
            border-bottom: none;
        }
        
        .monitor-card .stat-label {
            color: #666;
        }
        
        .monitor-card .stat-value {
            font-weight: 600;
            color: #333;
        }
        
        .log-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .log-table th {
            background: #f9fafb;
            padding: 0.75rem;
            text-align: left;
            font-weight: 600;
            color: #666;
            border-bottom: 2px solid #e5e7eb;
        }
        
        .log-table td {
            padding: 0.75rem;
            border-bottom: 1px solid #f0f0f0;
        }
        
        .log-table tr:hover {
            background: #f9fafb;
        }
        
        .log-table tr.clickable {
            cursor: pointer;
        }
        
        .log-table tr.clickable:hover {
            background: #eff6ff;
        }
        
        .level-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        
        .level-INFO {
            background: #dbeafe;
            color: #1e40af;
        }
        
        .level-ERROR {
            background: #fee2e2;
            color: #991b1b;
        }
        
        .level-WARNING {
            background: #fef3c7;
            color: #92400e;
        }
        
        .no-data {
            text-align: center;
            padding: 4rem 2rem;
            color: #666;
        }
        
        .no-data h2 {
            color: #333;
            margin-bottom: 1rem;
        }
        
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
        }
        
        .modal-content {
            background: white;
            margin: 5% auto;
            padding: 2rem;
            width: 90%;
            max-width: 800px;
            max-height: 80vh;
            overflow-y: auto;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 2px solid #e5e7eb;
        }
        
        .modal-header h3 {
            color: #667eea;
            font-size: 1.3rem;
        }
        
        .close {
            font-size: 2rem;
            font-weight: bold;
            color: #999;
            cursor: pointer;
            transition: color 0.2s;
        }
        
        .close:hover {
            color: #333;
        }
        
        .json-container {
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 1rem;
            border-radius: 4px;
            overflow-x: auto;
            font-family: 'Courier New', monospace;
            font-size: 0.9rem;
            line-height: 1.5;
        }
        
        .json-key {
            color: #9cdcfe;
        }
        
        .json-string {
            color: #ce9178;
        }
        
        .json-number {
            color: #b5cea8;
        }
        
        .json-boolean {
            color: #569cd6;
        }
        
        .detail-section {
            margin-bottom: 1.5rem;
        }
        
        .detail-section h4 {
            color: #666;
            font-size: 0.9rem;
            margin-bottom: 0.5rem;
            text-transform: uppercase;
        }
        
        .detail-section .value {
            font-size: 1.1rem;
            color: #333;
        }
        
        @media (max-width: 768px) {
            .container {
                padding: 0 1rem;
            }
            
            .stats-grid {
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            }
            
            .monitor-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
    {% block extra_css %}{% endblock %}
</head>
<body>
    <div class="header">
        <h1><a href="/">TruthTrader Dashboard</a></h1>
    </div>
    
    <div class="container">
        {% block content %}{% endblock %}
    </div>
    
    {% block scripts %}{% endblock %}
</body>
</html>
""", encoding='utf-8')
    
    # Index template
    (templates_dir / "index.html").write_text("""{% extends "base.html" %}

{% block content %}
{% if no_data %}
    <div class="no-data">
        <h2>No Data Available</h2>
        <p>No monitors have reported data yet.</p>
        <p>Make sure:</p>
        <ul style="list-style: none; margin-top: 1rem;">
            <li>* ENABLE_MONITOR_FEEDS=true in your .env file</li>
            <li>* Monitors are running (python main.py)</li>
            <li>* Monitors are actively processing posts</li>
        </ul>
    </div>
{% else %}
    <h2 style="margin-bottom: 1.5rem; color: #333;">Active Monitors</h2>
    
    <div class="monitor-grid">
        {% for monitor in monitors %}
        <div class="monitor-card" onclick="window.location.href='/monitor/{{ monitor.name }}'">
            <h3>{{ monitor.name.upper() }}</h3>
            
            <div class="stat-row">
                <span class="stat-label">Posts Processed</span>
                <span class="stat-value">{{ monitor.totals.posts_processed | format_number }}</span>
            </div>
            
            <div class="stat-row">
                <span class="stat-label">API Calls</span>
                <span class="stat-value">{{ monitor.totals.api_calls | format_number }}</span>
            </div>
            
            <div class="stat-row">
                <span class="stat-label">Tokens Used</span>
                <span class="stat-value">{{ monitor.totals.tokens_used | format_number }}</span>
            </div>
            
            <div class="stat-row">
                <span class="stat-label">Total Cost</span>
                <span class="stat-value">{{ monitor.totals.api_cost | format_cost }}</span>
            </div>
            
            {% if monitor.totals.errors > 0 %}
            <div class="stat-row">
                <span class="stat-label">Errors</span>
                <span class="stat-value" style="color: #ef4444;">{{ monitor.totals.errors | format_number }}</span>
            </div>
            {% endif %}
            
            {% if monitor.last_activity %}
            <div style="margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #f0f0f0; font-size: 0.9rem; color: #666;">
                Last activity: {{ monitor.last_activity }}
            </div>
            {% endif %}
        </div>
        {% endfor %}
    </div>
{% endif %}
{% endblock %}
""", encoding='utf-8')
    
    # Monitor detail template
    (templates_dir / "monitor.html").write_text("""{% extends "base.html" %}

{% block title %}{{ monitor_name.upper() }} - TruthTrader Dashboard{% endblock %}

{% block content %}
<div style="margin-bottom: 2rem;">
    <a href="/" style="color: #667eea; text-decoration: none; font-weight: 600;"><- Back to All Monitors</a>
</div>

<h2 style="margin-bottom: 1.5rem; color: #333;">{{ monitor_name.upper() }}</h2>

<!-- Summary Statistics -->
<div class="stats-grid">
    <div class="stat-card">
        <div class="label">Posts Processed</div>
        <div class="value">{{ totals.posts_processed | format_number }}</div>
    </div>
    
    <div class="stat-card">
        <div class="label">API Calls</div>
        <div class="value">{{ totals.api_calls | format_number }}</div>
    </div>
    
    <div class="stat-card">
        <div class="label">Tokens Used</div>
        <div class="value">{{ totals.tokens_used | format_number }}</div>
    </div>
    
    <div class="stat-card">
        <div class="label">Total Cost</div>
        <div class="value cost">{{ totals.api_cost | format_cost }}</div>
    </div>
    
    {% if totals.errors > 0 %}
    <div class="stat-card">
        <div class="label">Errors</div>
        <div class="value error">{{ totals.errors | format_number }}</div>
    </div>
    {% endif %}
</div>

<!-- Detailed Statistics by Subclass -->
{% if stats %}
<div class="card">
    <h2>Statistics by Category</h2>
    {% for subclass, metrics in stats.items() %}
    <div style="margin-bottom: 1.5rem;">
        <h3 style="color: #667eea; font-size: 1.1rem; margin-bottom: 0.5rem;">
            {{ subclass if subclass else '(general)' }}
        </h3>
        <table style="width: 100%; border-collapse: collapse;">
            {% for key, data in metrics.items() %}
            <tr style="border-bottom: 1px solid #f0f0f0;">
                <td style="padding: 0.5rem; color: #666;">{{ key }}</td>
                <td style="padding: 0.5rem; text-align: right; font-weight: 600;">
                    {% if 'cost' in key.lower() %}
                        {{ data.value | format_cost }}
                    {% else %}
                        {{ data.value | format_number }}
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>
    {% endfor %}
</div>
{% endif %}

<!-- Activity Log -->
<div class="card">
    <h2>Activity Log (Screening & Analysis Only)</h2>
    {% if logs %}
    <table class="log-table">
        <thead>
            <tr>
                <th style="width: 160px;">Timestamp</th>
                <th style="width: 120px;">Category</th>
                <th style="width: 80px;">Level</th>
                <th>Message</th>
            </tr>
        </thead>
        <tbody>
            {% for log in logs %}
            <tr class="{% if log.has_details %}clickable{% endif %}" 
                {% if log.has_details %}onclick="window.location.href='/log/{{ log.id }}/full'"{% endif %}>
                <td>{{ log.timestamp }}</td>
                <td>{{ log.subclass }}</td>
                <td><span class="level-badge level-{{ log.level }}">{{ log.level }}</span></td>
                <td>
                    {{ log.message }}
                    {% if log.has_details %}
                    <span style="color: #667eea; font-size: 0.85rem;"> (click for details)</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p style="color: #666; text-align: center; padding: 2rem;">No screening or analysis log entries yet.</p>
    {% endif %}
</div>
{% endblock %}

{% block scripts %}
<script>
// Auto-refresh statistics every 30 seconds
setInterval(function() {
    fetch('/api/stats?monitor={{ monitor_name }}')
        .then(response => response.json())
        .then(data => {
            // Update stat cards
            const totals = {
                posts_processed: 0,
                api_calls: 0,
                tokens_used: 0,
                api_cost: 0.0,
                errors: 0
            };
            
            for (const subclass in data) {
                for (const key in data[subclass]) {
                    if (key in totals) {
                        totals[key] += data[subclass][key].value;
                    }
                }
            }
            
            console.log('Stats updated:', totals);
        })
        .catch(error => console.error('Error refreshing stats:', error));
}, 30000);
</script>
{% endblock %}
""", encoding='utf-8')
    
    # Log detail template (full page view)
    (templates_dir / "log_detail.html").write_text("""{% extends "base.html" %}

{% block title %}Log {{ log.id }} - TruthTrader Dashboard{% endblock %}

{% block extra_css %}
<style>
    .section {
        background: white;
        border-radius: 8px;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    
    .section h3 {
        color: #667eea;
        font-size: 1.2rem;
        margin-bottom: 1rem;
        padding-bottom: 0.5rem;
        border-bottom: 2px solid #e5e7eb;
    }
    
    .meta-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 1rem;
        margin-bottom: 1rem;
    }
    
    .meta-item {
        padding: 0.75rem;
        background: #f9fafb;
        border-radius: 4px;
    }
    
    .meta-label {
        font-size: 0.85rem;
        color: #666;
        margin-bottom: 0.25rem;
    }
    
    .meta-value {
        font-weight: 600;
        color: #333;
    }
    
    .code-block {
        background: #1e1e1e;
        color: #d4d4d4;
        padding: 1.5rem;
        border-radius: 4px;
        overflow-x: auto;
        font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
        font-size: 0.9rem;
        line-height: 1.6;
        max-height: 600px;
        overflow-y: auto;
    }
    
    .code-block pre {
        margin: 0;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    
    .message-box {
        background: #f9fafb;
        border-left: 4px solid #667eea;
        padding: 1rem;
        margin: 1rem 0;
        border-radius: 4px;
    }
    
    .message-role {
        font-weight: 600;
        color: #667eea;
        margin-bottom: 0.5rem;
    }
    
    .message-content {
        color: #333;
        line-height: 1.6;
        white-space: pre-wrap;
    }
    
    .tab-container {
        margin-top: 1rem;
    }
    
    .tab-buttons {
        display: flex;
        gap: 0.5rem;
        margin-bottom: 1rem;
        border-bottom: 2px solid #e5e7eb;
    }
    
    .tab-button {
        padding: 0.75rem 1.5rem;
        background: none;
        border: none;
        color: #666;
        cursor: pointer;
        font-weight: 600;
        border-bottom: 3px solid transparent;
        margin-bottom: -2px;
        transition: all 0.2s;
    }
    
    .tab-button:hover {
        color: #667eea;
    }
    
    .tab-button.active {
        color: #667eea;
        border-bottom-color: #667eea;
    }
    
    .tab-content {
        display: none;
    }
    
    .tab-content.active {
        display: block;
    }
</style>
{% endblock %}

{% block content %}
<div style="margin-bottom: 2rem;">
    <a href="/monitor/{{ log.monitor }}" style="color: #667eea; text-decoration: none; font-weight: 600;"><- Back to {{ log.monitor.upper() }}</a>
</div>

<h2 style="margin-bottom: 1.5rem; color: #333;">API Call Details</h2>

<!-- Metadata Section -->
<div class="section">
    <h3>Metadata</h3>
    <div class="meta-grid">
        <div class="meta-item">
            <div class="meta-label">Monitor</div>
            <div class="meta-value">{{ log.monitor }}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Category</div>
            <div class="meta-value">{{ log.subclass or '(general)' }}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Timestamp</div>
            <div class="meta-value">{{ log.timestamp }}</div>
        </div>
        {% if log.data.provider %}
        <div class="meta-item">
            <div class="meta-label">Provider</div>
            <div class="meta-value">{{ log.data.provider }}</div>
        </div>
        {% endif %}
        {% if log.data.model %}
        <div class="meta-item">
            <div class="meta-label">Model</div>
            <div class="meta-value">{{ log.data.model }}</div>
        </div>
        {% endif %}
        {% if log.data.tokens %}
        <div class="meta-item">
            <div class="meta-label">Tokens</div>
            <div class="meta-value">{{ log.data.tokens | format_number }}</div>
        </div>
        {% endif %}
        {% if log.data.cost %}
        <div class="meta-item">
            <div class="meta-label">Cost</div>
            <div class="meta-value">{{ log.data.cost | format_cost }}</div>
        </div>
        {% endif %}
        {% if log.data.duration_sec %}
        <div class="meta-item">
            <div class="meta-label">Duration</div>
            <div class="meta-value">{{ "%.2f" | format(log.data.duration_sec) }}s</div>
        </div>
        {% endif %}
    </div>
</div>

{% if log.data.request or log.data.response %}
<!-- Tab Navigation -->
<div class="section">
    <div class="tab-container">
        <div class="tab-buttons">
            {% if log.data.request %}
            <button class="tab-button active" onclick="switchTab('request')">Request</button>
            {% endif %}
            {% if log.data.response %}
            <button class="tab-button {% if not log.data.request %}active{% endif %}" onclick="switchTab('response')">Response</button>
            {% endif %}
            <button class="tab-button" onclick="switchTab('raw')">Raw JSON</button>
        </div>
        
        {% if log.data.request %}
        <!-- Request Tab -->
        <div id="tab-request" class="tab-content active">
            <h3>API Request</h3>
            
            {% if log.data.request.system %}
            <div style="margin-bottom: 1.5rem;">
                <h4 style="color: #666; margin-bottom: 0.5rem;">System Prompt</h4>
                <div class="code-block">
                    <pre>{{ log.data.request.system }}</pre>
                </div>
            </div>
            {% endif %}
            
            {% if log.data.request.messages %}
            <div style="margin-bottom: 1.5rem;">
                <h4 style="color: #666; margin-bottom: 0.5rem;">Messages</h4>
                {% for message in log.data.request.messages %}
                <div class="message-box">
                    <div class="message-role">{{ message.role | upper }}</div>
                    <div class="message-content">{{ message.content }}</div>
                </div>
                {% endfor %}
            </div>
            {% endif %}
            
            {% if log.data.request.max_tokens %}
            <div style="margin-bottom: 1rem;">
                <strong>Max Tokens:</strong> {{ log.data.request.max_tokens }}
            </div>
            {% endif %}
            
            {% if log.data.request.temperature is defined %}
            <div style="margin-bottom: 1rem;">
                <strong>Temperature:</strong> {{ log.data.request.temperature }}
            </div>
            {% endif %}
            
            {% if log.data.request.tools %}
            <div style="margin-bottom: 1.5rem;">
                <h4 style="color: #666; margin-bottom: 0.5rem;">Tools Enabled</h4>
                <div class="code-block">
                    <pre>{{ log.data.request.tools | tojson(indent=2) }}</pre>
                </div>
            </div>
            {% endif %}
        </div>
        {% endif %}
        
        {% if log.data.response %}
        <!-- Response Tab -->
        <div id="tab-response" class="tab-content {% if not log.data.request %}active{% endif %}">
            <h3>API Response</h3>
            
            {% if log.data.response.content %}
            <div style="margin-bottom: 1.5rem;">
                <h4 style="color: #666; margin-bottom: 0.5rem;">Response Content</h4>
                {% if log.data.response.content is string %}
                <div class="code-block">
                    <pre>{{ log.data.response.content }}</pre>
                </div>
                {% else %}
                <!-- Handle array of content blocks -->
                {% for block in log.data.response.content %}
                    {% if block.type == 'text' %}
                    <div class="message-box">
                        <div class="message-role">TEXT</div>
                        <div class="message-content">{{ block.text }}</div>
                    </div>
                    {% elif block.type == 'thinking' %}
                    <div class="message-box" style="border-left-color: #f59e0b;">
                        <div class="message-role" style="color: #f59e0b;">THINKING</div>
                        <div class="message-content" style="color: #666; font-style: italic;">{{ block.thinking }}</div>
                    </div>
                    {% else %}
                    <div class="code-block">
                        <pre>{{ block | tojson(indent=2) }}</pre>
                    </div>
                    {% endif %}
                {% endfor %}
                {% endif %}
            </div>
            {% endif %}
            
            {% if log.data.response.usage %}
            <div style="margin-bottom: 1.5rem;">
                <h4 style="color: #666; margin-bottom: 0.5rem;">Token Usage</h4>
                <div class="meta-grid">
                    {% if log.data.response.usage.input_tokens %}
                    <div class="meta-item">
                        <div class="meta-label">Input Tokens</div>
                        <div class="meta-value">{{ log.data.response.usage.input_tokens | format_number }}</div>
                    </div>
                    {% endif %}
                    {% if log.data.response.usage.output_tokens %}
                    <div class="meta-item">
                        <div class="meta-label">Output Tokens</div>
                        <div class="meta-value">{{ log.data.response.usage.output_tokens | format_number }}</div>
                    </div>
                    {% endif %}
                    {% if log.data.response.usage.cache_creation_input_tokens %}
                    <div class="meta-item">
                        <div class="meta-label">Cache Creation</div>
                        <div class="meta-value">{{ log.data.response.usage.cache_creation_input_tokens | format_number }}</div>
                    </div>
                    {% endif %}
                    {% if log.data.response.usage.cache_read_input_tokens %}
                    <div class="meta-item">
                        <div class="meta-label">Cache Read</div>
                        <div class="meta-value">{{ log.data.response.usage.cache_read_input_tokens | format_number }}</div>
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endif %}
            
            {% if log.data.response.stop_reason %}
            <div style="margin-bottom: 1rem;">
                <strong>Stop Reason:</strong> {{ log.data.response.stop_reason }}
            </div>
            {% endif %}
        </div>
        {% endif %}
        
        <!-- Raw JSON Tab -->
        <div id="tab-raw" class="tab-content">
            <h3>Raw JSON Data</h3>
            <div class="code-block">
                <pre>{{ log.data | tojson(indent=2) }}</pre>
            </div>
        </div>
    </div>
</div>
{% else %}
<!-- Fallback for logs without request/response data -->
<div class="section">
    <h3>Log Data</h3>
    {% if log.data.type == 'post' %}
        <div class="meta-grid">
            <div class="meta-item">
                <div class="meta-label">Action</div>
                <div class="meta-value">{{ log.data.action }}</div>
            </div>
            <div class="meta-item">
                <div class="meta-label">Post ID</div>
                <div class="meta-value">{{ log.data.post_id }}</div>
            </div>
            {% if log.data.url %}
            <div class="meta-item">
                <div class="meta-label">URL</div>
                <div class="meta-value"><a href="{{ log.data.url }}" target="_blank" style="color: #667eea;">View Post</a></div>
            </div>
            {% endif %}
        </div>
        {% if log.data.preview %}
        <div style="margin-top: 1rem;">
            <h4 style="color: #666; margin-bottom: 0.5rem;">Preview</h4>
            <div class="message-box">
                <div class="message-content">{{ log.data.preview }}</div>
            </div>
        </div>
        {% endif %}
    {% else %}
        <div class="code-block">
            <pre>{{ log.data | tojson(indent=2) }}</pre>
        </div>
    {% endif %}
</div>
{% endif %}

{% endblock %}

{% block scripts %}
<script>
function switchTab(tabName) {
    // Hide all tab contents
    const contents = document.querySelectorAll('.tab-content');
    contents.forEach(content => content.classList.remove('active'));
    
    // Deactivate all buttons
    const buttons = document.querySelectorAll('.tab-button');
    buttons.forEach(button => button.classList.remove('active'));
    
    // Show selected tab
    const selectedTab = document.getElementById('tab-' + tabName);
    if (selectedTab) {
        selectedTab.classList.add('active');
    }
    
    // Activate clicked button
    event.target.classList.add('active');
}
</script>
{% endblock %}
""", encoding='utf-8')


if __name__ == '__main__':
    # Create templates directory and files
    create_templates()
    
    # Check if database exists
    if not Path(DB_PATH).exists():
        print(f"WARNING: Database not found at {DB_PATH}")
        print("The dashboard will start, but no data will be available until monitors start running.")
        print("Make sure ENABLE_MONITOR_FEEDS=true in your .env file.\n")
    
    print("=" * 60)
    print("TruthTrader Dashboard Starting")
    print("=" * 60)
    print(f"Database: {DB_PATH}")
    print(f"Access dashboard at: http://localhost:5000")
    print(f"Press Ctrl+C to stop")
    print("=" * 60)
    
    # Run Flask app
    app.run(host='0.0.0.0', port=5000, debug=True)