from app.collectors.extract import extract_pricing


def test_extracts_prices_from_model_cards():
    html = """
    <ul><li><div><a aria-label="Google: Gemini Test" href="/google/gemini-test"></a>
      <span>$0.10<!-- -->/M<!-- --> <!-- -->input<!-- --> <span>tokens</span></span>
      <span>$0<!-- -->/M<!-- --> <!-- -->output<!-- --> <span>tokens</span></span>
    </div></li></ul>
    """
    result = extract_pricing(html)
    assert result["parse_ok"] is True
    assert result["models"]["Google: Gemini Test"] == {
        "name": "Google: Gemini Test",
        "input_price": "0.1",
        "output_price": "0",
        "group": None,
        "multiplier": None,
        "currency": "USD",
        "price_unit": "per_1m_tokens",
    }
