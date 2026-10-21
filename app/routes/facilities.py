from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.core.permissions import FacilityPermission
from app.dependencies.auth import (
    FacilityAccess,
    SuperAdmin,
    require_facility_permission,
)
from app.dependencies.db import DbSession
from app.models.facility import Facility
from app.schemas.doctor import NextSlotRead
from app.schemas.facility import (
    FacilityCreate,
    FacilityListItem,
    FacilityRead,
    InviteRead,
    MemberCreate,
    MemberCreated,
    MemberRead,
)
from app.services.availability import slot_end
from app.services.doctors import DoctorService
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


@router.get("", response_model=list[FacilityListItem])
def list_facilities(db: DbSession) -> list[FacilityListItem]:
    """Public: active facilities patients can book at, each with its
    earliest free slot in the next 14 days (null if none)."""
    facilities = FacilityService(db).list_active()
    next_available = DoctorService(db).next_available_by_facility(facilities)
    items = []
    for facility in facilities:
        found = next_available[facility.id]
        items.append(
            FacilityListItem(
                **FacilityRead.model_validate(facility).model_dump(),
                next_available=None
                if found is None
                else NextSlotRead(
                    doctor_id=found[0].id,
                    starts_at=found[1],
                    ends_at=slot_end(found[1]),
                ),
            )
        )
    return items


@router.get("/{facility_id}", response_model=FacilityRead)
def get_facility(facility_id: UUID, db: DbSession) -> Facility:
    return FacilityService(db).get_active(facility_id)


@router.get("/{facility_id}/members", response_model=list[MemberRead])
def list_members(access: ManageMembers, db: DbSession) -> list[MemberRead]:
    """The facility's admins and doctors, including deactivated ones. A
    doctor who also works elsewhere is shown as they are here."""
    return [
        MemberRead.model_validate(m)
        for m in FacilityService(db).list_members(access.facility)
    ]


@router.post(
    "/{facility_id}/members",
    response_model=MemberCreated,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    body: MemberCreate,
    access: ManageMembers,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> MemberCreated:
    """Add a FACILITY_ADMIN or DOCTOR here: a new account, a doctor who
    already works at another facility (linked here too, keeping their
    account), or someone removed from this facility. Other accounts are
    refused. Someone who needs to set a password is emailed the invite; an
    existing doctor is emailed that they've been added."""
    member, invite_token = FacilityService(db).add_member(
        access.facility,
        email=body.email,
        role=body.role,
        name=body.name,
        background_tasks=background_tasks,
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
) -> MemberRead:
    """Remove someone from the facility's staff. An admin can no longer log
    in. A doctor stops taking bookings here but keeps their other
    facilities, and can't log in only once removed from all of them.
    History is kept, and re-adding their email restores it."""
    return MemberRead.model_validate(
        FacilityService(db).deactivate_member(access.facility, member_id)
    )


@router.post(
    "/{facility_id}/members/{member_id}/invite",
    response_model=InviteRead,
)
def reissue_invite(
    member_id: UUID,
    access: ManageMembers,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> InviteRead:
    """New invite for a member who hasn't set a password yet (e.g. the old
    one expired). It's emailed to them too."""
    token = FacilityService(db).reissue_invite(
        access.facility, member_id, background_tasks=background_tasks
    )
    return InviteRead(invite_token=token)
