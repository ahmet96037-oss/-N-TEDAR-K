"""JWT-based authentication helper'ları."""
import os
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthCredentials

# JWT secret key
SECRET_KEY = os.environ.get("JWT_SECRET", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"

security = HTTPBearer()


def create_token(data: dict, expires_delta: Optional[timedelta] = None):
    """JWT token oluştur."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=7)
    to_encode.update({"exp": expire.timestamp()})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(credentials: HTTPAuthCredentials = Depends(security)):
    """JWT token verify et ve customer_id döndür."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Token required")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        customer_id: int = payload.get("sub")
        if customer_id is None:
            raise HTTPException(status_code=401, detail="Token invalid")
        return customer_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Token invalid: {str(e)}")


def hash_password(password: str) -> str:
    """Parolayı hash'le."""
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Parola doğrula."""
    import bcrypt
    return bcrypt.checkpw(plain.encode(), hashed.encode())
