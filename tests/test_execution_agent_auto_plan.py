"""Auto-plan persistence: save/load round-trip, missing/corrupt -> None."""

from execution_agent import auto_plan


def _day():
    return {
        "date": "2026-11-14",
        "cash": 10000.0,
        "currency": "USD",
        "positions": [{"ticker": "NVDA", "quantity": 5, "average_price": 100.0}],
        "universe": ["NVDA", "AAPL"],
        "plan": [
            {
                "ticker": "NVDA",
                "side": "BUY",
                "signal": "Buy",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 189.5,
                "entry_price": 189.5,
                "stop_loss": 172.0,
                "price_target": 200.0,
                "summary": "Start a position.",
                "executed": {"order_id": 123, "order_status": "SUBMITTED"},
                "reason": "ok",
            }
        ],
    }


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_plan, "auto_plan_path", lambda: str(tmp_path / "plan.json"))
    day = _day()

    auto_plan.save_plan(day)

    assert auto_plan.load_plan() == day


def test_load_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_plan, "auto_plan_path", lambda: str(tmp_path / "nope.json"))

    assert auto_plan.load_plan() is None


def test_load_corrupt_returns_none(monkeypatch, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(auto_plan, "auto_plan_path", lambda: str(path))

    assert auto_plan.load_plan() is None


def test_save_plan_creates_parent_dir(monkeypatch, tmp_path):
    path = tmp_path / "deep" / "nested" / "plan.json"
    monkeypatch.setattr(auto_plan, "auto_plan_path", lambda: str(path))

    auto_plan.save_plan(_day())

    assert path.exists()
