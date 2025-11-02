
import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime, timezone
import threading
import time
from queue import Queue, Empty


# --- CONFIGURATION ---
URL = 'https://www.32auctions.com/organizations/ORG_ID/auctions/AUCTION_ID?r=1&t=all'
SUMMARY_URL = 'https://www.32auctions.com/FRIENDLY_AUCTION_PATH'
COOKIES = {
    '_session_id': 'SESSION_ID_GOES_HERE',
    'auth_token': 'AUTH_TOKEN_GOES_HERE'
}
REFRESH_INTERVAL = 10  # seconds

from urllib.parse import urljoin

MAX_LOGS = 200

manual_adjustment = 0.0
manual_lock = threading.Lock()
refresh_paused = False
refresh_lock = threading.Lock()
refresh_now = False
refresh_now_lock = threading.Lock()
log_entries = []
log_lock = threading.Lock()
status = {
    "next_refresh_in": REFRESH_INTERVAL,
    "last_total": None,
    "last_refresh": None
}
status_lock = threading.Lock()
event_subscribers = set()
event_subscribers_lock = threading.Lock()
KEEPALIVE_SECONDS = 15
filters = []
filters_lock = threading.Lock()


def get_status_snapshot():
    with status_lock:
        snapshot = dict(status)
    with refresh_lock:
        snapshot['refresh_paused'] = refresh_paused
    with manual_lock:
        snapshot['manual_adjustment'] = manual_adjustment
    with filters_lock:
        snapshot['filters'] = list(filters)
    snapshot['refresh_interval'] = REFRESH_INTERVAL
    return snapshot


def publish_event(event_type, data):
    payload = {'type': event_type, 'data': data}
    with event_subscribers_lock:
        subscribers = list(event_subscribers)
    for q in subscribers:
        try:
            q.put(payload, timeout=0.1)
        except Exception:
            continue


def update_status(**kwargs):
    if kwargs:
        with status_lock:
            status.update(kwargs)
    snapshot = get_status_snapshot()
    publish_event('status', snapshot)
    return snapshot


def subscribe_events():
    q = Queue()
    with event_subscribers_lock:
        event_subscribers.add(q)
    return q


def unsubscribe_events(q):
    with event_subscribers_lock:
        event_subscribers.discard(q)

def add_log(message):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')
    entry = {"timestamp": timestamp, "message": message}
    with log_lock:
        log_entries.append(entry)
        if len(log_entries) > MAX_LOGS:
            log_entries.pop(0)
    publish_event('log', entry)

def fetch_auction_items(url, session=None, progress_cb=None):
    """
    Scrape all auction items from paginated auction site.
    Args:
        url (str): The URL of the auction page.
        session (requests.Session): The session to use for requests.
    Returns:
        list: List of item dicts.
    """
    items = []
    next_url = url
    seen_pages = set()
    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        try:
            response = session.get(next_url)
            response.raise_for_status()
        except Exception as e:
            add_log(f"❌ Error fetching {next_url}: {e}")
            break
        soup = BeautifulSoup(response.text, 'html.parser')
        # Each item is an <a> with class 'item'
        for a in soup.select('a.item'):
            card = a.select_one('.card')
            if not card:
                continue
            # Image URL
            img = card.select_one('.img-section img')
            picture_url = img['src'] if img and 'src' in img.attrs else None
            # Title
            title = None
            h5 = card.select_one('h5.text-truncate-2.narrow')
            if h5:
                title = h5.get_text(strip=True)
            # Price (Buy Now or Bid)
            price = None
            h6 = card.select_one('h6.text-truncate')
            if h6:
                price = h6.get_text(strip=True)
            # Remaining, Value, Bids
            remaining = None
            value = None
            bids = None
            for div in card.select('div.card-text.small.text-truncate.text-muted'):
                text = div.get_text(strip=True).lower()
                if text.startswith('remaining:'):
                    remaining = text.replace('remaining:', '').strip()
                elif text.startswith('value:'):
                    value = text.replace('value:', '').strip()
                elif text.startswith('bids:'):
                    bids = text.replace('bids:', '').strip()
            items.append({
                'title': title,
                'picture_url': picture_url,
                'price': price,
                'remaining': remaining,
                'value': value,
                'bids': bids
            })
            if progress_cb:
                progress_cb(f"🔍 Found: {title[:60] if title else 'Untitled'}")
        # Find next page link
        next_link = soup.select_one('ul.pagination li.next a.page-link, ul.pagination li.next a[rel="next"]')
        if next_link and next_link.get('href'):
            next_url = urljoin(next_url, next_link['href'])
        else:
            next_url = None
    if progress_cb:
        progress_cb(f"🔍 Scrape collected {len(items)} items")
    return items


def apply_filters_to_items(items):
    """Filter items using active keyword list, returns kept items and filtered details."""
    with filters_lock:
        active_filters = [(f, f.lower()) for f in filters if f]
    if not active_filters:
        return items, []
    filtered_items = []
    filtered_out = []
    for item in items:
        title = item.get('title') or ''
        title_lower = title.lower()
        matched = None
        for original, term in active_filters:
            if term in title_lower:
                matched = original
                break
        if matched:
            filtered_out.append((item, matched))
            continue
        filtered_items.append(item)
    return filtered_items, filtered_out



def get_total_raised(summary_url, session=None):
    """
    Scrape the total raised value from the summary page.
    Args:
        summary_url (str): The URL of the summary page.
        session (requests.Session): The session to use for requests.
    Returns:
        str or None: The total raised value as a string, or None if not found.
    """
    try:
        response = session.get(summary_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        amt_div = soup.select_one('div.raised.amt')
        if amt_div:
            return amt_div.get_text(strip=True)
    except Exception as e:
        add_log(f"❌ Error fetching total raised: {e}")
    return None


from flask import request

def main():
    global refresh_paused, refresh_now
    add_log("⏳ Starting 32datasources auction data retrieval service...")
    session = requests.Session()
    session.cookies.update(COOKIES)
    try:
        while True:
            with refresh_lock:
                paused = refresh_paused
            if paused:
                with status_lock:
                    status["next_refresh_in"] = None
                time.sleep(1)
                continue

            with refresh_now_lock:
                do_refresh_now = refresh_now
                if do_refresh_now:
                    refresh_now = False

            if not do_refresh_now:
                for i in range(REFRESH_INTERVAL, 0, -1):
                    with refresh_lock:
                        if refresh_paused:
                            paused = True
                    if paused:
                        with status_lock:
                            status["next_refresh_in"] = None
                        break
                    with refresh_now_lock:
                        if refresh_now:
                            do_refresh_now = True
                            refresh_now = False
                            break
                    with status_lock:
                        status["next_refresh_in"] = i
                    time.sleep(1)
                if paused:
                    continue

            with status_lock:
                status["next_refresh_in"] = 0
            add_log("🚀 Beginning scrape cycle")
            start_time = datetime.now(timezone.utc)
            items = fetch_auction_items(URL, session=session, progress_cb=add_log)
            kept_items, filtered_out = apply_filters_to_items(items)
            for item, term in filtered_out:
                title = item.get('title') or 'Untitled'
                add_log(f"🚫 Filtered item '{title}' (matched '{term}')")
            if filtered_out:
                add_log(f"🧹 Filtered out {len(filtered_out)} item(s) before saving")
            total_raised = get_total_raised(SUMMARY_URL, session=session)
            with manual_lock:
                adj = manual_adjustment
            # Try to parse total_raised as float, else just append adjustment
            try:
                tr_float = float(total_raised.replace('$','').replace(',','')) if total_raised else 0.0
                total_raised_adj = tr_float + adj
                total_raised_display = f"${total_raised_adj:,.2f}"
            except Exception:
                total_raised_display = f"{total_raised} (+{adj:+.2f})"
            output = {
                'refreshed_at': datetime.now(timezone.utc).isoformat(),
                'url': URL,
                'total_items': len(kept_items),
                'total_raised': total_raised_display,
                'items': kept_items
            }
            with open('auction_items.json', 'w', encoding='utf-8') as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()
            add_log(f"🎉 Inventory completed at {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')} (Duration: {duration:.2f} seconds)")
            add_log(f"💾 Saved {len(kept_items)} items to auction_items.json")
            add_log(f"💰 Total Raised: {total_raised_display}")
            update_status(
                last_total=total_raised_display,
                last_refresh=end_time.isoformat(),
                next_refresh_in=REFRESH_INTERVAL
            )
    except KeyboardInterrupt:
        add_log("🛑 Stopped by user.")


from threading import Thread
from flask import Flask, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
import os


app = Flask(__name__)
CORS(app)

# REST API to set manual adjustment
@app.route('/adjustment', methods=['POST'])
def set_adjustment():
    global manual_adjustment
    data = None
    try:
        data = request.get_json(force=True)
        amt = float(data.get('amount', 0))
        with manual_lock:
            manual_adjustment = amt
        add_log(f"Manual adjustment set to {manual_adjustment:+.2f}")
        update_status()
        return jsonify({'status': 'ok', 'manual_adjustment': manual_adjustment}), 200
    except Exception as e:
        return jsonify({'error': str(e), 'data': data}), 400


@app.route('/filters', methods=['POST'])
def set_filters():
    data = None
    try:
        data = request.get_json(force=True)
        raw_filters = data.get('keywords', []) if data else []
        parsed_filters = []
        if isinstance(raw_filters, str):
            parsed_filters = [segment.strip() for segment in raw_filters.split(',') if segment.strip()]
        elif isinstance(raw_filters, (list, tuple)):
            for value in raw_filters:
                value_str = str(value).strip()
                if value_str:
                    parsed_filters.append(value_str)
        else:
            value_str = str(raw_filters).strip()
            if value_str:
                parsed_filters.append(value_str)
        with filters_lock:
            filters.clear()
            filters.extend(parsed_filters)
            active_filters = list(filters)
        display_value = ', '.join(active_filters) if active_filters else 'none'
        add_log(f"Filters updated: {display_value}")
        update_status()
        return jsonify({'status': 'ok', 'filters': active_filters}), 200
    except Exception as e:
        return jsonify({'error': str(e), 'data': data}), 400

# REST API to pause/resume/refresh
@app.route('/refresh', methods=['POST'])
def set_refresh():
    global refresh_paused, refresh_now
    data = None
    try:
        data = request.get_json(force=True)
        state = str(data.get('state', '')).lower()
        message = None
        triggered = False
        if state == 'pause':
            with refresh_lock:
                refresh_paused = True
            update_status(next_refresh_in=None)
            message = "⏸️ Refresh paused"
        elif state == 'resume':
            with refresh_lock:
                refresh_paused = False
            update_status(next_refresh_in=REFRESH_INTERVAL)
            message = "▶️ Refresh resumed"
        elif state == 'now':
            with refresh_now_lock:
                refresh_now = True
            update_status(next_refresh_in=0)
            triggered = True
            message = "🔁 Manual refresh requested"
        if message:
            add_log(message)
        return jsonify({'status': 'ok', 'refresh_paused': refresh_paused, 'refresh_triggered': triggered}), 200
    except Exception as e:
        return jsonify({'error': str(e), 'data': data}), 400


@app.route('/logs')
def get_logs():
    with log_lock:
        return jsonify(list(log_entries))


@app.route('/status')
def get_status():
    return jsonify(get_status_snapshot())


@app.route('/stream')
def stream_events():
    def event_stream():
        q = subscribe_events()
        try:
            yield f"event: status\ndata: {json.dumps(get_status_snapshot())}\n\n"
            with log_lock:
                logs_copy = list(log_entries)
            if logs_copy:
                yield f"event: logs\ndata: {json.dumps(logs_copy)}\n\n"
            while True:
                try:
                    event = q.get(timeout=KEEPALIVE_SECONDS)
                except Empty:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
        finally:
            unsubscribe_events(q)

    response = Response(stream_with_context(event_stream()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Scraper Control</title>
        <style>
        body {
            font-family: 'Segoe UI', 'Liberation Sans', 'Arial', sans-serif;
            margin: 2em;
            background: #1e1e1e;
            color: #d4d4d4;
        }
        h2 { color: #d7ba7d; }
        label { color: #d4d4d4; }
        input, button {
            font-size: 1em;
            background: #252526;
            color: #d4d4d4;
            border: 1px solid #3c3c3c;
            border-radius: 3px;
            padding: 0.5em;
            margin: 0.2em 0;
        }
        input:focus, button:focus {
            outline: 2px solid #007acc;
        }
        button {
            background: #0e639c;
            color: #fff;
            border: none;
            cursor: pointer;
            transition: background 0.2s;
        }
        button:hover {
            background: #1177bb;
        }
        form {
            background: #232323;
            padding: 1em;
            border-radius: 5px;
            box-shadow: 0 2px 8px #000a;
            max-width: 350px;
        }
        .controls {
            display: flex;
            gap: 0.5em;
            margin-top: 1em;
        }
        .pause-btn {
            background: #68217a;
        }
        .pause-btn.paused {
            background: #c586c0;
            color: #222;
        }
        .refresh-btn {
            background: #388a34;
        }
        .refresh-btn:active {
            background: #4ec94e;
        }
        .panel {
            background: #232323;
            padding: 1em;
            border-radius: 5px;
            box-shadow: 0 2px 8px #000a;
            border: 1px solid #3c3c3c;
            margin-top: 1em;
        }
        #status div {
            margin-bottom: 0.3em;
        }
        #logs {
            max-height: 300px;
            overflow-y: auto;
            font-family: 'Consolas', 'Courier New', monospace;
        }
        #logs .entry {
            padding: 0.25em 0;
            border-bottom: 1px solid #333;
        }
        #logs .entry:last-child {
            border-bottom: none;
        }
        #logs .ts {
            color: #569cd6;
            margin-right: 0.5em;
        }
        #logs .msg {
            color: #d4d4d4;
        }
        #result {
            margin-top: 1em;
            color: #b5cea8;
            min-height: 1.2em;
        }
        .hint {
            font-size: 0.85em;
            color: #a6a6a6;
        }
        </style>
    </head>
    <body>
        <h2>Scraper Control Panel</h2>
        <form id="adjForm">
            <label for="amount">Amount to set:</label><br>
            <input type="number" step="any" id="amount" name="amount" required><br>
            <button type="submit">Set Adjustment</button>
        </form>
        <form id="filterForm">
            <label for="filters">Filter keywords (comma-separated):</label><br>
            <input type="text" id="filters" name="filters" placeholder="e.g. raffle,ticket"><br>
            <button type="submit">Set Filters</button>
            <div class="hint">Matches item titles, case-insensitive.</div>
        </form>
        <div class="controls">
            <button id="pauseBtn" class="pause-btn">Pause Refresh</button>
            <button id="refreshBtn" class="refresh-btn">Refresh Now</button>
        </div>
        <div id="status" class="panel"></div>
        <div id="logs" class="panel"></div>
        <div id="result"></div>
        <script>
        let paused = false;
        let logBuffer = [];

        const statusEl = document.getElementById('status');
        const logsEl = document.getElementById('logs');
        const resultEl = document.getElementById('result');
    const filterInput = document.getElementById('filters');
    const filterForm = document.getElementById('filterForm');

        function updatePauseBtn() {
            const btn = document.getElementById('pauseBtn');
            btn.textContent = paused ? 'Resume Refresh' : 'Pause Refresh';
            btn.classList.toggle('paused', paused);
        }

        function escapeHTML(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }

        function renderStatus(data) {
            if (!data || typeof data !== 'object') {
                statusEl.innerHTML = '<div>Status unavailable.</div>';
                return;
            }
            paused = !!data.refresh_paused;
            updatePauseBtn();
            const next = data.next_refresh_in === null ? 'Paused' : `${data.next_refresh_in}s`;
            const lines = [];
            lines.push(`<div><strong>Next refresh:</strong> ${escapeHTML(next)}</div>`);
            if (data.last_refresh) {
                const ts = new Date(data.last_refresh).toLocaleString();
                lines.push(`<div><strong>Last refresh:</strong> ${escapeHTML(ts)}</div>`);
            }
            lines.push(`<div><strong>Last total raised:</strong> ${escapeHTML(data.last_total || 'N/A')}</div>`);
            const adj = Number(data.manual_adjustment ?? 0).toFixed(2);
            lines.push(`<div><strong>Manual adjustment:</strong> ${escapeHTML(adj)}</div>`);
            const filters = Array.isArray(data.filters) ? data.filters : [];
            if (filterInput && document.activeElement !== filterInput) {
                filterInput.value = filters.join(', ');
            }
            const filterText = filters.length ? filters.join(', ') : 'None';
            lines.push(`<div><strong>Filters:</strong> ${escapeHTML(filterText)}</div>`);
            statusEl.innerHTML = lines.join('');
        }

        function renderLogs() {
            if (!logBuffer.length) {
                logsEl.innerHTML = '<div>No logs yet.</div>';
                return;
            }
            logsEl.innerHTML = logBuffer.map(entry => `
                <div class="entry">
                    <span class="ts">${escapeHTML(entry.timestamp)}</span>
                    <span class="msg">${escapeHTML(entry.message)}</span>
                </div>
            `).join('');
            logsEl.scrollTop = logsEl.scrollHeight;
        }

        function setLogs(logs) {
            if (Array.isArray(logs)) {
                logBuffer = logs.slice(-200);
                renderLogs();
            }
        }

        function appendLog(entry) {
            if (!entry) return;
            logBuffer.push(entry);
            if (logBuffer.length > 200) {
                logBuffer.shift();
            }
            renderLogs();
        }

        const evtSource = new EventSource('/stream');
        evtSource.addEventListener('status', event => {
            try {
                const data = JSON.parse(event.data);
                renderStatus(data);
            } catch (err) {
                console.error('Failed to parse status event', err);
            }
        });
        evtSource.addEventListener('logs', event => {
            try {
                const data = JSON.parse(event.data);
                setLogs(data);
            } catch (err) {
                console.error('Failed to parse logs event', err);
            }
        });
        evtSource.addEventListener('log', event => {
            try {
                const data = JSON.parse(event.data);
                appendLog(data);
            } catch (err) {
                console.error('Failed to parse log event', err);
            }
        });
        evtSource.addEventListener('ping', () => {
            /* keep-alive */
        });
        evtSource.onerror = () => {
            resultEl.textContent = 'Connection disrupted. Attempting to reconnect...';
        };

        async function fetchJSON(url, options) {
            try {
                const res = await fetch(url, options);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data && data.error ? data.error : 'Request failed');
                }
                return data;
            } catch (err) {
                resultEl.textContent = `Error: ${err.message}`;
                return null;
            }
        }

        document.getElementById('adjForm').onsubmit = async function(e) {
            e.preventDefault();
            const amt = document.getElementById('amount').value;
            const data = await fetchJSON('/adjustment', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ amount: amt })
            });
            if (data) {
                resultEl.textContent = `Adjustment set to ${data.manual_adjustment}`;
            }
        };

        if (filterForm) {
            filterForm.onsubmit = async function(e) {
                e.preventDefault();
                const keywords = filterInput ? filterInput.value : '';
                const data = await fetchJSON('/filters', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ keywords })
                });
                if (data) {
                    const list = Array.isArray(data.filters) ? data.filters.join(', ') : '';
                    resultEl.textContent = list ? `Filters set to ${list}` : 'Filters cleared.';
                }
            };
        }

        document.getElementById('pauseBtn').onclick = async function(e) {
            e.preventDefault();
            const state = paused ? 'resume' : 'pause';
            const data = await fetchJSON('/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ state })
            });
            if (data) {
                resultEl.textContent = state === 'pause' ? 'Refresh paused.' : 'Refresh resumed.';
            }
        };

        document.getElementById('refreshBtn').onclick = async function(e) {
            e.preventDefault();
            const data = await fetchJSON('/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ state: 'now' })
            });
            if (data) {
                resultEl.textContent = 'Refresh triggered.';
            }
        };
        </script>
    </body>
    </html>
    '''

@app.route('/auction_items.json')
def serve_json():
    if os.path.exists('auction_items.json'):
        return send_file('auction_items.json', mimetype='application/json')
    else:
        return jsonify({'error': 'auction_items.json not found'}), 404

def run_scraper():
    main()

if __name__ == '__main__':
    # Start scraper in a background thread
    scraper_thread = Thread(target=run_scraper, daemon=True)
    scraper_thread.start()
    # Start Flask server
    app.run(host='0.0.0.0', port=8081)
