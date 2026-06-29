from datetime import datetime
from sqlalchemy.orm import Session
from ..models import ActivityLog

def log_activity(db: Session, message: str, level: str = "INFO"):
    try:
        print(f"[{level}] {message}")
        log_entry = ActivityLog(message=message, level=level)
        db.add(log_entry)
        db.commit()
        
        # Keep database size compact by pruning logs beyond the last 300 entries
        try:
            db.execute(
                "DELETE FROM activity_logs WHERE id < (SELECT COALESCE(max(id), 0) - 300 FROM activity_logs)"
            )
            db.commit()
        except Exception:
            pass
    except Exception as e:
        print(f"Failed to log activity: {e}")
