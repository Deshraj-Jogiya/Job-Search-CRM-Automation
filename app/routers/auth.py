"""
Real signup/login/forgot-password for the one operator account this
deployment has. Not behind app_dependencies -- by definition you can't
already be authenticated to reach these. See AdminAccount's docstring
in models.py and auth_service.py for the single-account (not
multi-tenant) design.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AdminAccount
from ..services import auth_service
from ..services.activity_logger import log_activity
from ..services.email_utils import is_smtp_configured, send_email
from ..templating import render

router = APIRouter(tags=["auth"])


def _base_url(request: Request) -> str:
    import os

    configured = os.getenv("APP_BASE_URL")
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, db: Session = Depends(get_db)):
    # Signup is first-run setup, not open registration -- once an
    # account exists, this instance has its one operator.
    if db.query(AdminAccount).first():
        return RedirectResponse(url="/login", status_code=303)
    return render(request, "signup.html", {"error": request.query_params.get("error")})


@router.post("/signup")
def signup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    recovery_email: str = Form(""),
    db: Session = Depends(get_db),
):
    if db.query(AdminAccount).first():
        return RedirectResponse(url="/login", status_code=303)

    username = username.strip()
    recovery_email = recovery_email.strip()
    if not username or not password:
        return RedirectResponse(url="/signup?error=Username+and+password+are+required.", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url="/signup?error=Passwords+don%27t+match.", status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/signup?error=Password+must+be+at+least+8+characters.", status_code=303)

    account = AdminAccount(
        username=username,
        password_hash=auth_service.hash_password(password),
        recovery_email=recovery_email or None,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    log_activity(db, f"Account created for '{username}'.", "INFO")

    response = RedirectResponse(url="/", status_code=303)
    session_token = auth_service.create_session_token(account.id)
    response.set_cookie(
        auth_service.SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        samesite="strict",
        max_age=auth_service.SESSION_VALID_DAYS * 24 * 3600,
    )
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if not db.query(AdminAccount).first():
        return RedirectResponse(url="/signup", status_code=303)
    return render(
        request,
        "login.html",
        {"error": request.query_params.get("error"), "next": request.query_params.get("next", "/")},
    )


@router.post("/login")
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    account = db.query(AdminAccount).first()
    if not account or account.username != username.strip() or not auth_service.verify_password(password, account.password_hash):
        return RedirectResponse(url="/login?error=Incorrect+username+or+password.", status_code=303)

    # next must be a same-app relative path -- never redirect off-site
    # based on unvalidated user input.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(url=safe_next, status_code=303)
    session_token = auth_service.create_session_token(account.id)
    response.set_cookie(
        auth_service.SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        samesite="strict",
        max_age=auth_service.SESSION_VALID_DAYS * 24 * 3600,
    )
    log_activity(db, f"'{account.username}' logged in.", "INFO")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME)
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return render(
        request,
        "forgot_password.html",
        {"message": request.query_params.get("message"), "error": request.query_params.get("error")},
    )


@router.post("/forgot-password")
def forgot_password_submit(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip()
    account = db.query(AdminAccount).filter(AdminAccount.recovery_email == email).first() if email else None

    # Always show the same generic message regardless of whether the
    # email matched a real account -- doesn't confirm/deny account
    # existence to whoever's asking.
    generic_message = "If that email is on file, a reset link has been sent."

    if not account:
        return RedirectResponse(url=f"/forgot-password?message={generic_message.replace(' ', '+')}", status_code=303)

    if not is_smtp_configured():
        log_activity(db, "Password reset requested but SMTP isn't configured -- no email sent.", "WARNING")
        return RedirectResponse(
            url="/forgot-password?error=SMTP+isn%27t+configured+on+this+deployment+--+ask+whoever+runs+it+to+set+SMTP_USER%2FSMTP_PASSWORD.",
            status_code=303,
        )

    token = auth_service.create_reset_token(account.id)
    link = f"{_base_url(request)}/reset-password?token={token}"
    try:
        send_email(
            account.recovery_email,
            "Password reset -- Career Pilot",
            f"Reset your password: {link}\n\nThis link expires in {auth_service.RESET_TOKEN_VALID_HOURS} hours. "
            "If you didn't request this, you can ignore this email.",
        )
        log_activity(db, f"Sent password reset email for '{account.username}'.", "INFO")
    except Exception as e:
        log_activity(db, f"Failed to send password reset email: {e}", "ERROR")

    return RedirectResponse(url=f"/forgot-password?message={generic_message.replace(' ', '+')}", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    account_id = auth_service.verify_reset_token(token)
    if not account_id:
        return render(request, "reset_password.html", {"token": None, "error": "This reset link is invalid or has expired."})
    return render(request, "reset_password.html", {"token": token, "error": None})


@router.post("/reset-password")
def reset_password_submit(
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    account_id = auth_service.verify_reset_token(token)
    if not account_id:
        return RedirectResponse(url="/forgot-password?error=This+reset+link+is+invalid+or+has+expired.", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url=f"/reset-password?token={token}&error=Passwords+don%27t+match.", status_code=303)
    if len(password) < 8:
        return RedirectResponse(
            url=f"/reset-password?token={token}&error=Password+must+be+at+least+8+characters.", status_code=303
        )

    account = db.query(AdminAccount).filter(AdminAccount.id == account_id).first()
    if not account:
        return RedirectResponse(url="/forgot-password?error=Account+not+found.", status_code=303)

    account.password_hash = auth_service.hash_password(password)
    db.commit()
    log_activity(db, f"Password reset completed for '{account.username}'.", "INFO")
    return RedirectResponse(url="/login?error=Password+updated+--+log+in+with+your+new+password.", status_code=303)
