"""Run one-time Google OAuth setup for standalone transcript documents."""
from __future__ import annotations

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from google_docs import OAUTH_CLIENT_PATH, OAUTH_TOKEN_PATH, SCOPES


def main() -> None:
    if not OAUTH_CLIENT_PATH.exists():
        raise RuntimeError(f"OAuth client file not found: {OAUTH_CLIENT_PATH}")

    OAUTH_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_PATH), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, prompt="consent", open_browser=False)
    OAUTH_TOKEN_PATH.write_text(creds.to_json())
    os.chmod(OAUTH_TOKEN_PATH, 0o600)
    print(f"Saved Google OAuth token to {OAUTH_TOKEN_PATH}")


if __name__ == "__main__":
    main()
