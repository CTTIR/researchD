#!/usr/bin/env python3
# ============================================================
# scheduler.py — per-question email distribution.
#
# Each question carries its own cron cadence (email_cron), its own
# recipient list (email_to), and a last-sent timestamp. This service
# wakes every CHECK_INTERVAL seconds, finds questions whose cadence
# is due since email_last_sent, and emails that question's NEW
# (un-emailed) records — grouped by study type — to its recipients.
#
# Independent of the R fetch cron: fetching fills the records table;
# this drains it to email on each question's own schedule.
#
# Env:
#   DB_PATH, CHECK_INTERVAL (s, default 900)
#   SMTP_HOST, SMTP_PORT, SMTP_USER, EMAIL_PW, SMTP_FROM
#   SMTP_TLS = starttls | ssl | none
# ============================================================
import os, json, time, sqlite3, smtplib, ssl
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from html import escape
from urllib.parse import urlsplit
from email.mime.text import MIMEText
from email.utils import formataddr

from croniter import croniter

DB_PATH = os.environ.get("DB_PATH", "/app/db/pipeline.sqlite")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PW   = os.environ.get("EMAIL_PW", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_TLS  = os.environ.get("SMTP_TLS", "starttls").lower()

STUDY_ORDER = ["systematic_review","rct","clinical_trial","guideline",
               "observational","case_report","review","other","unclassified"]
STUDY_LABEL = {
    "systematic_review":"Systematic reviews & meta-analyses",
    "rct":"Randomized controlled trials",
    "clinical_trial":"Clinical trials (other)",
    "guideline":"Guidelines & consensus",
    "observational":"Observational & comparative studies",
    "case_report":"Case reports",
    "review":"Reviews (narrative)",
    "other":"Editorials, letters & news",
    "unclassified":"Unclassified / not yet indexed",
}


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000;")
    return con


def now_local():
    """Current time in the deployment's timezone.

    Cron expressions here are written by a human in the dashboard ("0 7 * * 1"
    means Monday at seven), so they have to fire in the timezone the
    deployment is configured for -- docker-compose passes TZ to this service
    precisely so that it can. Running the comparison in UTC instead would
    silently shift every digest by the local offset (two hours, for a
    Europe/Berlin deployment in summer).
    """
    zone = os.environ.get("TZ", "")
    return datetime.now(ZoneInfo(zone)) if zone else datetime.now().astimezone()


def heartbeat(ok=True, detail="", error=""):
    """Service-level liveness, mirroring R/07_db_questions.R::heartbeat().

    The scheduler is the quietest service in the stack -- a healthy one with
    nothing due does nothing at all -- so "no output" is its normal state and
    also its failure state. One row, overwritten in place, tells them apart.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = db()
        con.execute(
            """INSERT INTO service_heartbeat(service,last_run,last_ok,last_detail,last_error)
               VALUES('scheduler',?,?,?,?)
               ON CONFLICT(service) DO UPDATE SET
                 last_run    = excluded.last_run,
                 last_ok     = COALESCE(excluded.last_ok, service_heartbeat.last_ok),
                 last_detail = excluded.last_detail,
                 last_error  = excluded.last_error""",
            (now, now if ok else None, detail, error))
        con.commit(); con.close()
    except Exception as e:                      # telemetry must never break a tick
        print(f"[scheduler] heartbeat failed: {e}", flush=True)


def is_due(cron_expr, last_sent_iso, now):
    """True if at least one scheduled time has passed since last_sent."""
    base = None
    if last_sent_iso:
        try:
            base = datetime.fromisoformat(last_sent_iso)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            # croniter walks WALL-CLOCK fire points from `base`, so base has to
            # be expressed in the same zone as `now` or the fire points land in
            # the wrong hour. Stored timestamps may be UTC (older rows).
            if now.tzinfo is not None:
                base = base.astimezone(now.tzinfo)
        except Exception:
            base = None
    if base is None:
        # A new schedule can first run at any fire point today, including
        # midnight. croniter.get_next is exclusive of its starting instant.
        base = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)
    try:
        nxt = croniter(cron_expr, base).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt <= now
    except Exception as e:
        print(f"[scheduler] bad cron '{cron_expr}': {e}", flush=True)
        return False


def _text(value):
    return escape(str(value or ""), quote=True)


def _link(url, title):
    # Metadata is external input. Escape text and attributes separately, and
    # never create executable/data links in a digest.
    value = str(url or "")
    try:
        parsed = urlsplit(value)
        safe = (parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
                and not any(ord(c) < 32 for c in value))
    except ValueError:
        safe = False
    label = f"<strong>{_text(title)}</strong>"
    return f'<a href="{_text(value)}">{label}</a>' if safe else label


def render_html(label, rows):
    parts = [f"<h1>{_text(label)}</h1>"]
    by_type = {}
    for r in rows:
        by_type.setdefault(r["study_type"] or "unclassified", []).append(r)
    for st in STUDY_ORDER + sorted(set(by_type) - set(STUDY_ORDER)):
        sub = by_type.get(st, [])
        if not sub:
            continue
        parts.append(f"<h3 style='border-bottom:1px solid #ddd'>{_text(STUDY_LABEL.get(st, st))} ({len(sub)})</h3><ol>")
        for r in sub:
            pp = " <span style='background:#fff3cd;padding:1px 5px;border-radius:3px;font-size:11px'>preprint</span>" if r["is_preprint"] else ""
            src = f"<span style='background:#eee;padding:1px 5px;border-radius:3px;font-size:11px'>{_text(r['source'])}</span>"
            parts.append(
                f"<li style='margin-bottom:10px'>{src}{pp} "
                f"{_link(r['url'], r['title'])}<br>"
                f"<span style='color:#555;font-size:13px'>{_text((r['authors'] or '')[:120])} · {_text(r['pub_date'])}</span></li>")
        parts.append("</ol>")
    return "\n".join(parts)


SIGNAL_LABEL = {
    "trial_result": "Trial results", "ir_alert": "Company IR alerts",
    "sec_filing": "SEC filings", "regulatory": "Regulatory",
    "press_release": "Press releases", "aggregator": "Research wires",
    "literature_alert": "Journal alerts", "trial_registration": "New trial registrations",
    "institution": "Institution press", "conference": "Conference",
    "trade_press": "Trade press", "grant": "New grants", "patent": "Patents",
    "news": "News",
}
SIGNAL_ORDER = ["trial_result", "ir_alert", "sec_filing", "regulatory",
                "press_release", "aggregator", "literature_alert",
                "trial_registration", "institution", "conference",
                "trade_press", "grant", "patent", "news"]
CONFIDENCE_RANK = {"unverified": 0, "reported": 1, "authoritative": 2}
# the "be first" tier: signal types eligible for instant alerting
ALERT_TYPES = {"trial_result", "ir_alert", "sec_filing", "regulatory", "press_release"}

def render_signals_html(rows):
    if not rows:
        return ""
    parts = ["<h2 style='margin-top:24px'>Signals (early intelligence)</h2>"]
    by_type = {}
    for r in rows:
        by_type.setdefault(r["signal_type"] or "unclassified", []).append(r)
    for st in SIGNAL_ORDER + sorted(set(by_type) - set(SIGNAL_ORDER)):
        sub = by_type.get(st, [])
        if not sub:
            continue
        parts.append(f"<h3 style='border-bottom:1px solid #ddd'>{_text(SIGNAL_LABEL.get(st, st))} ({len(sub)})</h3><ol>")
        for r in sub:
            conf = r["confidence"] or "reported"
            badge = {"authoritative": "#d4edda", "reported": "#fff3cd",
                     "unverified": "#f8d7da"}.get(conf, "#eee")
            parts.append(
                f"<li style='margin-bottom:10px'>"
                f"<span style='background:{badge};padding:1px 5px;border-radius:3px;font-size:11px'>{_text(conf)}</span> "
                f"{_link(r['url'], r['title'])}<br>"
                f"<span style='color:#555;font-size:13px'>{_text((r['org'] or '')[:80])} · {_text(r['signal_date'])}</span></li>")
        parts.append("</ol>")
    return "\n".join(parts)


def send_email(to_list, subject, html):
    if SMTP_TLS not in ("starttls", "ssl", "none"):
        print("[scheduler] invalid SMTP_TLS; expected starttls, ssl or none", flush=True)
        return False
    if not SMTP_HOST:
        print("[scheduler] SMTP_HOST not set; skipping send", flush=True)
        return False
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Research Feeds", SMTP_FROM))
    msg["To"] = ", ".join(to_list)
    try:
        if SMTP_TLS == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
                if SMTP_USER: s.login(SMTP_USER, SMTP_PW)
                refused = s.sendmail(SMTP_FROM, to_list, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                if SMTP_TLS == "starttls":
                    s.starttls(context=ssl.create_default_context())
                if SMTP_USER: s.login(SMTP_USER, SMTP_PW)
                refused = s.sendmail(SMTP_FROM, to_list, msg.as_string())
        if refused:
            # SMTP accepts partial delivery without throwing. Keep the batch
            # pending so refused recipients are retried; accepted recipients
            # may receive a duplicate on retry until per-recipient state exists.
            print(f"[scheduler] partial delivery: {len(refused)} recipient(s) refused; batch retained for retry", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[scheduler] send failed: {e}", flush=True)
        return False


def _send_digest(con, q, now):
    to_list = json.loads(q["email_to"] or "[]")
    if not to_list:
        return True
    if not is_due(q["email_cron"], q["email_last_sent"], now):
        return True
    # Recall safety-valve: email the high-signal stream. Records below the
    # threshold are still stored and visible in all.xml; they just don't
    # generate email noise. They are NOT marked emailed, so if you later
    # lower the threshold they can still go out.
    threshold_sql = ""
    params = [q["id"]]
    if (q["digest_mode"] if "digest_mode" in q.keys() else "all") == "threshold":
        ms = q["min_score"] if "min_score" in q.keys() else 0
        threshold_sql = " AND score >= ?"
        params.append(float(ms or 0))
    rows = con.execute(
        "SELECT * FROM records WHERE question_id=? AND emailed=0" + threshold_sql +
        " ORDER BY study_type, score DESC", params).fetchall()
    # signals for this question's digest, honoring its min_confidence
    cfg = con.execute("SELECT min_confidence FROM signal_config WHERE question_id=?",
                      (q["id"],)).fetchone()
    minc = (cfg["min_confidence"] if cfg else None) or "unverified"
    allowed = [k for k, v in CONFIDENCE_RANK.items()
               if v >= CONFIDENCE_RANK.get(minc, 0)]
    sig_rows = con.execute(
        "SELECT * FROM signals WHERE question_id=? AND emailed=0 AND confidence IN (%s)"
        " ORDER BY signal_type, score DESC" % ",".join("?" * len(allowed)),
        (q["id"], *allowed)).fetchall()
    if not rows and not sig_rows:
        # nothing new; still advance last_sent so we don't recheck constantly
        con.execute("UPDATE questions SET email_last_sent=? WHERE id=?",
                    (now.isoformat(), q["id"]))
        con.commit()
        return True
    html = render_html(q["label"], rows) + render_signals_html(sig_rows)
    subject = f"[Research] {q['label']} — {len(rows)} papers, {len(sig_rows)} signals"
    if send_email(to_list, subject, html):
        ids = [(r["dedup_key"], q["id"]) for r in rows]
        con.executemany(
            "UPDATE records SET emailed=1 WHERE dedup_key=? AND question_id=?", ids)
        sids = [(r["dedup_key"], q["id"]) for r in sig_rows]
        con.executemany(
            "UPDATE signals SET emailed=1 WHERE dedup_key=? AND question_id=?", sids)
        con.execute("UPDATE questions SET email_last_sent=? WHERE id=?",
                    (now.isoformat(), q["id"]))
        con.commit()
        print(f"[scheduler] sent {len(rows)} records + {len(sig_rows)} signals for {q['id']} to {len(to_list)} recipient(s)", flush=True)
    else:
        return False
    return True


def run_once():
    now = now_local()
    con = db()
    ok = True
    try:
        qs = con.execute(
            "SELECT * FROM questions WHERE enabled=1 AND email_enabled=1").fetchall()
        for q in qs:
            try:
                ok = _send_digest(con, q, now) and ok
            except Exception as exc:
                con.rollback()
                print(f"[scheduler] digest failed for {q['id']}: {exc}", flush=True)
                ok = False
        return ok
    finally:
        con.close()


def _send_alert(con, q):
    to_list = json.loads(q["email_to"] or "[]")
    if not to_list:
        return True
    marks = ",".join("?" * len(ALERT_TYPES))
    rows = con.execute(
        f"SELECT * FROM signals WHERE question_id=? AND emailed=0 "
        f"AND signal_type IN ({marks}) "
        f"AND confidence IN ('authoritative','reported') "
        f"ORDER BY score DESC LIMIT 20",
        (q["id"], *ALERT_TYPES)).fetchall()
    if not rows:
        return True
    html = ("<p style='color:#a00'><strong>Instant alert</strong> — "
            "high-priority signals just detected:</p>") + render_signals_html(rows)
    subject = f"[ALERT] {q['label']} — {len(rows)} high-priority signal(s)"
    if send_email(to_list, subject, html):
        ids = [(r["dedup_key"], q["id"]) for r in rows]
        con.executemany(
            "UPDATE signals SET emailed=1 WHERE dedup_key=? AND question_id=?", ids)
        con.commit()
        print(f"[scheduler] INSTANT alert: {len(rows)} for {q['id']}", flush=True)
    else:
        return False
    return True


def run_alerts():
    """Send high-priority signals on each tick; retain failed batches for retry."""
    con = db()
    ok = True
    try:
        qs = con.execute("""
            SELECT q.*, sc.instant_alerts FROM questions q
            JOIN signal_config sc ON sc.question_id = q.id
            WHERE q.enabled=1 AND q.email_enabled=1 AND sc.instant_alerts=1
        """).fetchall()
        for q in qs:
            try:
                ok = _send_alert(con, q) and ok
            except Exception as exc:
                con.rollback()
                print(f"[scheduler] alert failed for {q['id']}: {exc}", flush=True)
                ok = False
        return ok
    finally:
        con.close()


if __name__ == "__main__":
    print(f"[scheduler] up; checking every {CHECK_INTERVAL}s "
          f"(tz={datetime.now().astimezone().tzname()}, "
          f"smtp={SMTP_HOST or 'UNSET'}:{SMTP_PORT} {SMTP_TLS})", flush=True)
    if not SMTP_HOST:
        print("[scheduler] WARNING: SMTP_HOST is unset -- digests will be "
              "prepared and then skipped, and records stay unemailed for "
              "retry. Set it in .env.", flush=True)
    ticks = 0
    while True:
        ticks += 1
        try:
            alerts_ok = run_alerts()  # instant tier first (every tick)
            digest_ok = run_once()    # digest tier (per-question cron)
            ok = alerts_ok and digest_ok
            heartbeat(ok=ok, detail=f"tick {ticks}; interval {CHECK_INTERVAL}s",
                      error="" if ok else "One or more email batches failed; see scheduler log")
        except Exception as e:
            print(f"[scheduler] loop error: {e}", flush=True)
            heartbeat(ok=False, detail=f"tick {ticks}", error=str(e))
        time.sleep(CHECK_INTERVAL)
