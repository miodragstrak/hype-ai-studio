from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def plan_result() -> dict:
    return {
        "schema_version": "music-video-plan-v1",
        "concept_title": "Local Structured Signal",
        "logline": "An imaginary performance grows into silver abstraction.",
        "treatment": "A measured practical-light performance progresses through two connected beats.",
        "creative_direction": {
            "visual_style": "silver nocturne",
            "color_palette": ["silver", "black"],
            "camera_language": "controlled movement",
            "editing_rhythm": "measured",
            "performance_direction": "restrained",
            "continuity_notes": ["preserve direction"],
            "avoid": ["logos", "text"],
        },
        "shots": [
            {
                "item_key": f"shot-{index:03d}",
                "ordinal": index,
                "title": f"Signal {index}",
                "description": "A practical-light visual beat.",
                "prompt": "Cinematic imaginary silver performance, no text.",
                "duration_seconds": 5,
                "shot_type": "wide",
                "camera": "slow push",
                "subject": "imaginary performer",
                "environment": "abstract stage",
                "continuity_notes": "Preserve screen direction.",
                "reference_asset_ids": [],
            }
            for index in (1, 2)
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        count = int(COUNT.read_text()) + 1 if COUNT.exists() else 1
        COUNT.write_text(str(count))
        mode = MODE
        if mode == "transient" and count == 1:
            self.respond(
                429, {"error": {"message": "temporary rate limit", "type": "rate_limit_error"}}
            )
            return
        if mode == "auth":
            self.respond(
                401,
                {"error": {"message": f"rejected Bearer {SECRET}", "type": "authentication_error"}},
            )
            return
        result = plan_result()
        if mode == "malformed":
            result = {"schema_version": "music-video-plan-v1"}
        output = [
            {
                "id": "msg_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(result),
                        "annotations": [],
                    }
                ],
            }
        ]
        self.respond(
            200,
            {
                "id": "resp_local_002b",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-5.6-sol",
                "output": output,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 240,
                    "total_tokens": 360,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    def respond(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


PORT = int(sys.argv[1])
MODE = sys.argv[2]
COUNT = Path(sys.argv[3])
SECRET = sys.argv[4]
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
