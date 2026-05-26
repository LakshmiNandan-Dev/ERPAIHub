from fastapi import APIRouter, Depends, status, HTTPException, Response, Body
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from .. import database, schemas, models, utils

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

@router.get("/getuser", response_model=schemas.UserWithRolesOut)
def get_user(current_user: models.User = Depends(get_current_user)):
    """Get the currently logged in user details, along with roles and agents"""
    return current_user


@router.post("/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def register(user: schemas.UserCreate, db: Session = Depends(database.get_db)):
    # Check if user already exists
    existing_user = db.query(models.User).filter(
        (models.User.email == user.email) | (models.User.username == user.username)
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
        
    # Hash the password
    hashed_password = utils.hash_password(user.password)
    
    # Create new user
    new_user = models.User(
        username=user.username,
        email=user.email,
        password_hash=hashed_password
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return new_user
    

@router.post("/login", response_model=schemas.SessionOut)
def login(user_credentials: schemas.UserLogin, db: Session = Depends(database.get_db)):
    # Find user by username
    user = db.query(models.User).filter(models.User.username == user_credentials.username).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Invalid Credentials"
        )
        
    if not utils.verify_password(user_credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Invalid Credentials"
        )
        
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
    
    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "token_type": "bearer",
        "user": user
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(session_token: str = Body(..., embed=True), db: Session = Depends(database.get_db)):
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
        
    # Invalidate session
    session_record.is_active = False
    db.commit()
    
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(payload: schemas.UserChangePassword, db: Session = Depends(database.get_db)):
    # Find user
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
        
    # Verify old password
    if not utils.verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid old password"
        )
        
    # Hash new password and update
    user.password_hash = utils.hash_password(payload.new_password)
    db.commit()
    
    return {"message": "Password updated successfully"}
