from pathlib import Path

from .cli import main
from .core.dotenv import load

load(Path(__file__).resolve().parent.parent / ".env")  # API keys; see .env.example

raise SystemExit(main())
