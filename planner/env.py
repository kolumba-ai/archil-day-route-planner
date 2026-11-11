"""Load KEY=value lines from the project's .env file into the environment.

Keeps secrets (ARCHIL_API_KEY, KEENABLE_API_KEY, ANTHROPIC_API_KEY) out of code and chat.
Values already set in the environment win.
"""
import os

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def load():
    if not os.path.exists(ENV_FILE):
        return []
    loaded = []
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
    return loaded


load()
