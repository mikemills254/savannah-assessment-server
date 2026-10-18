from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.permissions import FacilityPermission
from app.dependencies.auth import (
    FacilityAccess,
    SuperAdmin,
    require_facility_permission,
)
from app.dependencies.db import DbSession
from app.models.facility import Facility
from app.models.user import User
from app.schemas.facility import (
    FacilityCreate,
    FacilityRead,
    InviteRead,
    MemberCreate,
    MemberCreated,
    MemberRead,
)
from app.services.facilities import FacilityService

router = APIRouter(prefix="/facilities", tags=["facilities"])

ManageMembers = Annotated[
    FacilityAccess,
    Depends(require_facility_permission(FacilityPermission.MEMBERS_MANAGE)),
]


@router.post(
    "",
    response_model=FacilityRead,
    status_code=status.HTTP_201_CREATED,
)
def create_facility(
    body: FacilityCreate,
    _: SuperAdmin,
    db: DbSession,
) -> Facility:
    """Super admin only. Add the facility's first admin with POST /facilities/{id}/members."""
    return FacilityService(db).create(name=body.name, timezone=body.timezone)


@router.get("", response_model=list[FacilityRead])
def list_facilities(db: DbSession) -> list[Facility]:
    """Public: active facilities patients can book at."""
    return FacilityService(db).list_active()


@router.get("/{facility_id}", response_model=FacilityRead)
def get_facility(facility_id: UUID, db: DbSession) -> Facility:
    return FacilityService(db).get_active(facility_id)


@router.get("/{facility_id}/members", response_model=list[MemberRead])
def list_members(access: ManageMembers, db: DbSession) -> list[User]:
    """The facility's admins and doctors, including deactivated ones."""
    return FacilityService(db).list_members(access.facility)


@router.post(
    "/{facility_id}/members",
    response_model=MemberCreated,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    body: MemberCreate,
    access: ManageMembers,
    db: DbSession,
) -> MemberCreated:
    """Create a FACILITY_ADMIN or DOCTOR account here, or reactivate a
    deactivated one of this facility's. Emails of other accounts are refused."""
    member, invite_token = FacilityService(db).add_member(
        access.facility,
        email=body.email,
        role=body.role,
        name=body.name,
    )
    return MemberCreated(
        member=MemberRead.model_validate(member),
        invite_token=invite_token,
    )


@router.post(
    "/{facility_id}/members/{member_id}/deactivate",
    response_model=MemberRead,
)
def deactivate_member(
    member_id: UUID,
    access: ManageMembers,
    db: DbSession,
) -> User:
    """Deactivate a staff account; they can no longer log in. History is
    kept, and re-adding their email restores it."""
    return FacilityService(db).deactivate_member(access.facility, member_id)


@router.post(
    "/{facility_id}/members/{member_id}/invite",
    response_model=InviteRead,
)
def reissue_invite(
    member_id: UUID,
    access: ManageMembers,
    db: DbSession,
) -> InviteRead:
    """New invite for a member who hasn't set a password yet (e.g. the old one expired)."""
    token = FacilityService(db).reissue_invite(access.facility, member_id)
    return InviteRead(invite_token=token)
