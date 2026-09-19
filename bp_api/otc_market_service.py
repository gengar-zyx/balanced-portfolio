"""OTC market helpers shared across transports."""
from fastapi import HTTPException
from . import db, repositories_otc as rotc
from .quant.otc.calendar import gen_ko_observation_dates, roll_dates

def observation_dates(payload):
    if payload.maturity_date <= payload.start_date or (payload.maturity_date-payload.start_date).days > 366*30 or payload.freq_months < 1:
        raise HTTPException(400, "观察日范围或频率无效")
    with db.get_conn() as conn:
        cal = rotc.load_calendar_view(conn, payload.start_date, payload.maturity_date)
    if payload.dates:
        return {"dates": roll_dates(payload.dates, cal)}
    dates = gen_ko_observation_dates(
        payload.start_date, payload.maturity_date, cal,
        freq_months=payload.freq_months, lock_term_months=payload.lock_term_months,
    )
    return {"dates": [{"requested": d.isoformat(), "effective": d.isoformat(), "rolled": False} for d in dates]}

