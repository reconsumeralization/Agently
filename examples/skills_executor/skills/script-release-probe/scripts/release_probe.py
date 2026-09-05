from __future__ import annotations

import argparse
import json
import secrets


parser = argparse.ArgumentParser()
parser.add_argument("--release", required=True)
parser.add_argument("--component", required=True)
args = parser.parse_args()

print(
    json.dumps(
        {
            "release": args.release,
            "component": args.component,
            "probe_token": secrets.token_hex(4),
            "status": "success",
        },
        sort_keys=True,
    )
)
