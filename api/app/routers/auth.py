from fastapi import APIRouter, Depends, status, HTTPException, Response, Body, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from .. import database, schemas, models, utils, audit_service


def _origin(request: Request):
    """Return (ip, user_agent) for the calling client."""
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)
    return ip, request.headers.get("user-agent")

router = APIRouter(
    prefix="/auth",
    tags=['Authentication']
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    session_record = db.query(models.UserSession).filter(
        models.UserSession.session_token == token,
        models.UserSession.is_active == True
    ).first()
    
    if not session_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    # Check if session is expired
    if session_record.expires_at < datetime.now(timezone.utc):
        session_record.is_active = False
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    user = db.query(models.User).filter(models.User.id == session_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    return user


def _role(user) -> str:
    return (getattr(user, "role", None) or ("admin" if user.is_admin else "user"))


def get_current_admin(current_user: models.User = Depends(get_current_user)):
    """Authorization guard for admin-only endpoints (configuration, user management)."""
    if _role(current_user) != "admin" and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return current_user


def require_approver(current_user: models.User = Depends(get_current_user)):
    """Admin or DBA — may approve sign-ups and clone overrides, and view audit."""
    if _role(current_user) not in ("admin", "dba") and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approver role (Admin or DBA) required",
        )
    return current_user


def require_agent_access(current_user: models.User = Depends(get_current_user)):
    """Admin or DBA — may invoke agents. The default 'user' role can chat only."""
    if _role(current_user) not in ("admin", "dba") and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role can use chat but is not permitted to invoke agents. Contact an administrator.",
        )
    return current_user


@router.get("/getuser", response_model=schemas.UserWithRolesOut)
def get_user(current_user: models.User = Depends(get_current_user)):
    """Get the currently logged in user details, along with roles and agents"""
    return current_user


@router.post("/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def register(user: schemas.UserCreate, request: Request, db: Session = Depends(database.get_db)):
    # Public self sign-up is disabled unless an admin enables it (or this is the
    # very first account being bootstrapped).
    is_first_user = db.query(models.User.id).first() is None
    if not is_first_user:
        sso = db.query(models.SsoSettings).filter(models.SsoSettings.id == 1).first()
        if not (sso and sso.signup_enabled):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Self sign-up is disabled. Please contact your administrator for an account.",
            )

    # Check if user already exists
    existing_user = db.query(models.User).filter(
        (models.User.email == user.email) | (models.User.username == user.username)
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )

    hashed_password = utils.hash_password(user.password)

    # The first account is bootstrapped as an active admin. Self sign-ups are
    # created INACTIVE and PENDING — an administrator must approve them.
    new_user = models.User(
        username=user.username,
        email=user.email,
        password_hash=hashed_password,
        is_admin=is_first_user,
        is_active=is_first_user,
        approval_status="approved" if is_first_user else "pending",
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    if not is_first_user:
        ip, ua = _origin(request)
        audit_service.log("signup_request", user_id=new_user.id, username=new_user.username,
                          ip=ip, user_agent=ua, method="POST", path="/auth/register",
                          detail={"email": new_user.email})

    return new_user
    

@router.post("/login", response_model=schemas.SessionOut)
def login(user_credentials: schemas.UserLogin, request: Request, db: Session = Depends(database.get_db)):
    ip, ua = _origin(request)
    # Find user by username
    user = db.query(models.User).filter(models.User.username == user_credentials.username).first()

    if not user or not user.password_hash or not utils.verify_password(user_credentials.password, user.password_hash):
        audit_service.log("login_failed", user_id=(user.id if user else None),
                          username=user_credentials.username, ip=ip, user_agent=ua,
                          method="POST", path="/auth/login")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Credentials"
        )

    # Account must be active and approved to sign in.
    if not user.is_active:
        if user.approval_status == "pending":
            msg = "Your account is pending administrator approval."
        elif user.approval_status == "rejected":
            msg = "Your account request was declined. Contact your administrator."
        else:
            msg = "Your account is disabled. Contact your administrator."
        audit_service.log("login_failed", user_id=user.id, username=user.username,
                          ip=ip, user_agent=ua, method="POST", path="/auth/login",
                          detail={"reason": user.approval_status or "inactive"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)

    # Generate session token and expiration
    session_token = utils.create_session_token()
    expires_at = utils.get_session_expiration(hours=24)

    # Save session to DB
    new_session = models.UserSession(
        user_id=user.id,
        session_token=session_token,
        expires_at=expires_at
    )
    db.add(new_session)
    db.commit()

    audit_service.log("login", user_id=user.id, username=user.username,
                      ip=ip, user_agent=ua, method="POST", path="/auth/login")

    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "token_type": "bearer",
        "user": user
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session_token: str = Body(..., embed=True), db: Session = Depends(database.get_db)):
    # Find active session
    session_record = db.query(models.UserSession).filter(
        models.UserSession.session_token == session_token,
        models.UserSession.is_active == True
    ).first()

    if not session_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or already inactive"
        )

    ip, ua = _origin(request)
    _u = db.query(models.User).filter(models.User.id == session_record.user_id).first()
    audit_service.log("logout", user_id=session_record.user_id,
                      username=(_u.username if _u else None), ip=ip, user_agent=ua,
                      method="POST", path="/auth/logout")

    # Invalidate session
    session_record.is_active = False
    db.commit()
    
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    payload: schemas.PasswordChangeRequest,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Change the logged-in user's own password."""
    # Verify the current password
    if not current_user.password_hash or not utils.verify_password(
        payload.old_password, current_user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        )

    new_password = (payload.new_password or "").strip()
    if len(new_password) < 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must be at least 4 characters",
        )

    current_user.password_hash = utils.hash_password(new_password)
    db.commit()

    return {"message": "Password updated successfully"}
