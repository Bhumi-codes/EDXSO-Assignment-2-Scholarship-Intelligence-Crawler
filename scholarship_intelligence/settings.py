"""Project-relative paths and crawler settings."""

from pathlib import Path


# Resolve paths from this file, regardless of the terminal's location.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
RAW_SNAPSHOT_DIR = RUNTIME_DIR / "snapshots" / "raw"
PREPARED_SNAPSHOT_DIR = RUNTIME_DIR / "snapshots" / "prepared"
DATABASE_PATH = RUNTIME_DIR / "scholarships.sqlite3"
SEEDS_PATH = PROJECT_ROOT / "config" / "seeds.json"

# Default discovery safety budgets.
# External pages count toward the total page limit.
MAX_PAGES_PER_SEED = 30
MAX_EXTERNAL_PAGES_PER_SEED = 5

# Standard settings understood by Scrapy.
SCRAPY_SETTINGS = {
    "ROBOTSTXT_OBEY": True,
    "USER_AGENT": "ScholarshipIntelligenceCrawler/0.1",
    "CONCURRENT_REQUESTS": 4,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
    "DOWNLOAD_DELAY": 2.0,
    "DOWNLOAD_TIMEOUT": 30,
    "DEPTH_LIMIT": 3,
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 1,
    "COOKIES_ENABLED": False,
    "TELNETCONSOLE_ENABLED": False,
    "LOG_LEVEL": "INFO",
}