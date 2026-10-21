import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status

from app.core import rate_limit
from app.core.google import google_sign_in_enabled, verify_google_id_token
from app.core.logging import bind
from app.core.security import create_access_token
from app.dependencies.auth import CurrentUser
from app.dependencies.db import DbSession
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GoogleLoginRequest,
    LoginRequest,
    MeRead,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.services.auth import AuthService
from app.services.email import send_password_reset

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _per_ip(policy: rate_limit.Policy):
    return Depends(rate_limit.rate_limit(policy, rate_limit.client_ip))


def _auth_response(user: User) -> AuthResponse:
    bind(user_id=str(user.id))
    return AuthResponse(
        access_token=create_access_token(user.id),
        user=MeRead.model_validate(user),
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_per_ip(rate_limit.REGISTER)],
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

    logger.info(
        "Registered", extra={"event": "auth.registered", "role": user.role.value}
    )
    return _auth_response(user)


@router.post(
    "/login",
    response_model=AuthResponse,
    dependencies=[_per_ip(rate_limit.LOGIN_IP)],
)
def login(body: LoginRequest, request: Request, db: DbSession) -> AuthResponse:
    email = AuthService.normalize_email(body.email)
    rate_limit.check(rate_limit.LOGIN, f"{rate_limit.client_ip(request)}|{email}")

    user = AuthService(db).login(email=body.email, password=body.password)
    if user is None:
        # No email: it's personal data, and often a password typed in the
        # wrong box.
        logger.info("Login failed", extra={"event": "auth.login_failed"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.info("Logged in", extra={"event": "auth.login", "method": "password"})
    return _auth_response(user)


@router.post(
    "/google",
    response_model=AuthResponse,
    dependencies=[_per_ip(rate_limit.GOOGLE_LOGIN)],
)
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
    logger.info("Logged in", extra={"event": "auth.login", "method": "google"})
    return _auth_response(user)


@router.post(
    "/accept-invite",
    response_model=AuthResponse,
    dependencies=[_per_ip(rate_limit.ACCEPT_INVITE)],
)
def accept_invite(body: AcceptInviteRequest, db: DbSession) -> AuthResponse:
    """Set the password of an invited account and log in."""
    user = AuthService(db).accept_invite(token=body.token, password=body.password)
    return _auth_response(user)


FORGOT_PASSWORD_REPLY = (
    "If an account uses that email, we've sent it a link to reset the password."
)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_per_ip(rate_limit.FORGOT_PASSWORD)],
)
def forgot_password(
    body: ForgotPasswordRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> ForgotPasswordResponse:
    """Email a password reset link. The reply is the same whether or not the
    account exists, and the email is sent after the response, so neither
    the answer nor its timing reveals who has an account."""
    email = AuthService.normalize_email(body.email)
    rate_limit.check(rate_limit.FORGOT_PASSWORD_EMAIL, email)

    found = AuthService(db).request_password_reset(email)
    if found is not None:
        user, token = found
        background_tasks.add_task(
            send_password_reset,
            user_id=user.id,
            email=user.email,
            name=user.name,
            token=token,
        )
    logger.info(
        "Password reset requested",
        extra={"event": "auth.reset_requested", "sent": found is not None},
    )
    return ForgotPasswordResponse(detail=FORGOT_PASSWORD_REPLY)


@router.post(
    "/reset-password",
    response_model=AuthResponse,
    dependencies=[_per_ip(rate_limit.RESET_PASSWORD)],
)
def reset_password(body: ResetPasswordRequest, db: DbSession) -> AuthResponse:
    """Set a new password from an emailed reset link, and sign in. Each link
    works once; 400 if it's invalid, expired or used."""
    user = AuthService(db).reset_password(token=body.token, password=body.password)
    logger.info("Password reset", extra={"event": "auth.password_reset"})
    return _auth_response(user)


@router.get("/me", response_model=MeRead)
def me(user: CurrentUser) -> User:
    """The caller, with their facility if they're a FACILITY_ADMIN or DOCTOR."""
    return user
