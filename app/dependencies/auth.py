from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.logging import bind
from app.core.permissions import (
    FacilityPermission,
    UserRole,
    facility_permissions,
)
from app.core.security import decode_access_token
from app.dependencies.db import DbSession, get_db
from app.models.facility import Facility
from app.models.user import User
from app.services.auth import WITH_FACILITIES

# Swagger's "Authorize" button takes the token from POST /auth/login.
# auto_error=False so a missing header is our 401, not FastAPI's default.
bearer_scheme = HTTPBearer(auto_error=False)


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to perform this action",
    )


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_current_user(credentials: Credentials, db: DbSession) -> User:
    return _authenticate(credentials, db)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_streaming_user(
    credentials: Credentials,
    # Closed as soon as the user is loaded, rather than when the response
    # ends, so a long-lived response doesn't hold a pooled connection.
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> User:
    """The caller, for routes whose response stays open, such as an event
    stream."""
    return _authenticate(credentials, db)


StreamingUser = Annotated[User, Depends(get_streaming_user)]


def _authenticate(
    credentials: HTTPAuthorizationCredentials | None, db: Session
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_error

    user_id = decode_access_token(credentials.credentials)
    if user_id is None:
        raise credentials_error

    # Where they work, in the same query: GET /auth/me returns it, and the
    # permission checks under /facilities/{id} read it.
    user = db.get(User, user_id, options=list(WITH_FACILITIES))
    if user is None or not user.is_active:
        raise credentials_error

    bind(user_id=str(user.id))
    return user


def require_super_admin(user: CurrentUser) -> User:
    if user.role != UserRole.SUPER_ADMIN:
        raise _forbidden()
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


@dataclass(frozen=True)
class FacilityAccess:
    """The caller, the facility named by the {facility_id} path parameter,
    and what the caller may do there."""

    user: User
    facility: Facility
    permissions: frozenset[FacilityPermission]

    def can(self, permission: FacilityPermission) -> bool:
        return permission in self.permissions


def get_facility_access(
    facility_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> FacilityAccess:
    facility = db.get(Facility, facility_id)
    # Only super admins can reach a deactivated facility.
    if facility is None or (
        not facility.is_active and user.role != UserRole.SUPER_ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Facility not found",
        )

    return FacilityAccess(
        user=user,
        facility=facility,
        permissions=facility_permissions(
            user.role,
            is_own_facility=user.works_at(facility.id),
        ),
    )


def require_facility_permission(
    permission: FacilityPermission,
) -> Callable[..., FacilityAccess]:
    """For routes under /facilities/{facility_id}/...

    Usage: access: Annotated[FacilityAccess, Depends(require_facility_permission(FacilityPermission.MEMBERS_MANAGE))]
    """

    def checker(
        access: Annotated[FacilityAccess, Depends(get_facility_access)],
    ) -> FacilityAccess:
        if not access.can(permission):
            raise _forbidden()
        return access

    return checker
