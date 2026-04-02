import logging

logger = logging.getLogger(__name__)


def hello_fitness() -> str:
    """Returns a greeting."""
    return "Hello from fitness tracker!"


def get_activities():
    logger.info(f"get_activities called")
    return []
