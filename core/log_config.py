import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Zoaholic")

logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("watchfiles.main").setLevel(logging.CRITICAL)