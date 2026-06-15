from stock_quant import YahooSource


def test_yahoo_quote_returns_dict():
    src = YahooSource()
    quote = src.get_quote("AAPL")
    assert isinstance(quote, dict)
    assert quote["symbol"] == "AAPL"
