import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Paths
PROJECT_ROOT = Path(__file__).parent.parent

# Browser connection
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]
BROWSER_CHANNEL = "chrome"
BROWSER_HEADLESS = False

# Database
DATABASE_URL = os.environ["DATABASE_URL"]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DATABASE_URL + "_test")

# File storage base path (local: relative path, production: gs://bucket-name)
STORAGE_BASE_PATH = os.environ["STORAGE_BASE_PATH"]
