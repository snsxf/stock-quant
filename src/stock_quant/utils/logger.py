"""统一 loguru 日志配置。其他模块 `from stock_quant.utils.logger import logger` 即可。"""
import sys
from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:MM-DD HH:mm:ss}</green> | <level>{level:<7}</level> | "
           "<cyan>{name}</cyan> - <level>{message}</level>",
)

__all__ = ["logger"]
