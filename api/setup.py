"""
One-time webhook setup endpoint.
Visit https://your-app.vercel.app/api/setup once after deploying
to register your Vercel URL as the Telegram webhook.
"""

import os
import json
from http.server import BaseHTTPRequestHandler

import httpx

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("VERCEL_URL", "")  # Auto-set by Vercel


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if not BOT_TOKEN:
            self._respond(500, {"error": "TELEGRAM_BOT_TOKEN not set"})
            return

        # Build the webhook URL
        host = WEBHOOK_URL or self.headers.get("Host", "")
        if not host:
            self._respond(500, {"error": "Could not determine host URL. Set VERCEL_URL env var."})
            return

        webhook_url = f"https://{host}/api/webhook"

        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["message", "edited_message"]},
                timeout=10,
            )
            result = r.json()
        except Exception as e:
            self._respond(500, {"error": str(e)})
            return

        if result.get("ok"):
            self._respond(200, {
                "status": "✅ Webhook registered successfully!",
                "webhook_url": webhook_url,
                "telegram_response": result,
            })
        else:
            self._respond(400, {
                "status": "❌ Failed to register webhook",
                "telegram_response": result,
            })

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
