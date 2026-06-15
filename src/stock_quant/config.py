from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    futu_host: str = "127.0.0.1"
    futu_port: int = 11111

    finnhub_api_key: str = ""
    tavily_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "stock-quant/0.1"


settings = Settings()
