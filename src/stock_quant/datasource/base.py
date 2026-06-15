from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    """统一数据源接口：上层代码只依赖它，不关心是 yfinance 还是富途"""

    @abstractmethod
    def get_quote(self, symbol: str) -> dict: ...

    @abstractmethod
    def get_history(
        self, symbol: str, period: str = "3mo", interval: str = "1d"
    ) -> pd.DataFrame: ...

    @abstractmethod
    def get_option_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame: ...
