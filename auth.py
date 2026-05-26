import os
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from database import get_db, fetchone

load_dotenv()

SECRET_KEY   = os.getenv("SECRET_KEY", "change-me")
ALGORITHM    = "HS256"
TOKEN_EXPIRE = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", 24))

bearer_scheme = HTTPBearer()


def create_access_token(employee_id: int) -> str:
    payload = {
        "sub": str(employee_id),
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_employee_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> int:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        employee_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен недействителен или истёк",
        )
    return employee_id
