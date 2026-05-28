from web import app, check_overdue_tasks, _init_user_db, _APSCHEDULER_AVAILABLE

_init_user_db()

if _APSCHEDULER_AVAILABLE:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(check_overdue_tasks, "interval", minutes=30, id="overdue_check")
        scheduler.start()
    except Exception:
        pass

if __name__ == '__main__':
    app.run()
