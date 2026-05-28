from web import app, check_overdue_tasks, _init_user_db
from apscheduler.schedulers.background import BackgroundScheduler

_init_user_db()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_overdue_tasks, "interval", minutes=30, id="overdue_check")
scheduler.start()

if __name__ == '__main__':
    app.run()
