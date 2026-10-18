from fastapi import APIRouter, HTTPException, status

from app.core.google import google_sign_in_enabled, verify_google_id_token
from app.core.security import create_access_token
from app.dependencies.auth import CurrentUser
from app.dependencies.db import DbSession
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    GoogleLoginRequest,
    LoginRequest,
    MeRead,
    RegisterRequest,
)
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_response(user: User) -> AuthResponse:
    return AuthResponse(
        access_token=create_access_token(user.id),
        user=MeRead.model_validate(user),
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(body: RegisterRequest, db: DbSession) -> AuthResponse:
    """Public sign-up, which also logs the user in. Creates a PATIENT.

    With facility_name (and timezone) it creates a FACILITY_ADMIN instead,
    together with their facility."""
    service = AuthService(db)

    # The request validator guarantees timezone whenever facility_name is set.
    if body.facility_name is not None and body.timezone is not None:
        user = service.register_facility_admin(
            name=body.name,
            email=body.email,
            password=body.password,
            facility_name=body.facility_name,
            timezone=body.timezone,
        )
    else:
        user = service.register(
            name=body.name,
            email=body.email,
            password=body.password,
        )

    return _auth_response(user)


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, db: DbSession) -> AuthResponse:
    user = AuthService(db).login(email=body.email, password=body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _auth_response(user)


@router.post("/google", response_model=AuthResponse)
def login_with_google(body: GoogleLoginRequest, db: DbSession) -> AuthResponse:
    """Sign in or sign up with a Google ID token from Google Identity Services."""
    if not google_sign_in_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured",
        )

    identity = verify_google_id_token(body.id_token)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google sign-in",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = AuthService(db).login_with_google(identity)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated",
        )
    return _auth_response(user)


@router.post("/accept-invite", response_model=AuthResponse)
def accept_invite(body: AcceptInviteRequest, db: DbSession) -> AuthResponse:
    """Set the password of an invited account and log in."""
    user = AuthService(db).accept_invite(token=body.token, password=body.password)
    return _auth_response(user)


@router.get("/me", response_model=MeRead)
def me(user: CurrentUser) -> User:
    """The caller, with their facility if they're a FACILITY_ADMIN or DOCTOR."""
    return user
