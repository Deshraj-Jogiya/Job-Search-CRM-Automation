from ..models import ActivityLog


def log_activity(db, message: str, level: str = "INFO"):
    entry = ActivityLog(message=message, level=level)
    db.add(entry)
    db.commit()
    print(f"[{level}] {message}")
