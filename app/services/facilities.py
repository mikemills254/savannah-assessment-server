from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.core.permissions import STAFF_ROLES, UserRole
from app.core.security import create_invite_token
from app.models.doctor_facility import DoctorFacility
from app.models.facility import Facility
from app.models.user import User
from app.services.auth import AuthService
from app.services.email import send_added_to_facility, send_invite


class MemberAlreadyExistsError(ConflictError):
    detail = "This person is already on this facility's staff"


class EmailInUseError(ConflictError):
    detail = "This email already belongs to another account"


class MemberNotFoundError(NotFoundError):
    detail = "Member not found"


class LastAdminError(ConflictError):
    detail = "A facility must keep at least one active admin"


@dataclass(frozen=True)
class Member:
    """Someone on a facility's staff, as that facility sees them. For a
    doctor at several facilities, is_active and created_at are about this
    one: whether they still work here, and when they joined it."""

    user: User
    facility_id: UUID
    # A doctor's link to this facility; None for an admin.
    link: DoctorFacility | None = None

    @property
    def id(self) -> UUID:
        return self.user.id

    @property
    def name(self) -> str:
        return self.user.name

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> UserRole:
        return self.user.role

    @property
    def has_password(self) -> bool:
        return self.user.has_password

    @property
    def is_active(self) -> bool:
        if self.link is not None:
            return self.link.is_active and self.user.is_active
        return self.user.is_active

    @property
    def created_at(self) -> datetime:
        return self.link.created_at if self.link is not None else self.user.created_at


class FacilityService:
    def __init__(self, db: Session):
        self.db = db

    def create(self, *, name: str, timezone: str) -> Facility:
        facility = Facility(name=name, timezone=timezone)
        self.db.add(facility)
        self.db.commit()
        return facility

    def list_active(self) -> list[Facility]:
        return list(
            self.db.scalars(
                select(Facility)
                .where(Facility.is_active.is_(True))
                .order_by(Facility.name)
            )
        )

    def get_active(self, facility_id: UUID) -> Facility:
        facility = self.db.get(Facility, facility_id)
        if facility is None or not facility.is_active:
            raise NotFoundError("Facility not found")
        return facility

    def list_members(self, facility: Facility) -> list[Member]:
        """The facility's admins and doctors, including deactivated ones, in
        the order they joined it."""
        admins = self.db.scalars(
            select(User).where(
                User.facility_id == facility.id,
                User.role == UserRole.FACILITY_ADMIN,
            )
        )
        links = self.db.scalars(
            select(DoctorFacility)
            .where(DoctorFacility.facility_id == facility.id)
            .options(joinedload(DoctorFacility.doctor))
        )
        members = [Member(user, facility.id) for user in admins] + [
            Member(link.doctor, facility.id, link) for link in links
        ]
        return sorted(members, key=lambda m: (m.created_at, m.id))

    def add_member(
        self,
        facility: Facility,
        *,
        email: str,
        role: UserRole,
        name: str | None,
        background_tasks: BackgroundTasks,
    ) -> tuple[Member, str | None]:
        """Add a facility admin or doctor. Returns the member and, if they
        have never set a password, an invite token to do so; the invite is
        also emailed to them after the response is sent.

        - A new email creates the account.
        - A doctor who already works elsewhere is linked here too, keeping
          their account, password and other facilities; they're emailed
          that they've been added.
        - Someone removed from this facility is restored, with their
          working hours and history (and may come back in the other role).
        - Any other account is refused: an account has one role, so a
          patient or another facility's admin can't be added."""
        if role not in STAFF_ROLES:
            raise InvalidRequestError("Members must be a facility admin or a doctor")

        user = AuthService(self.db).get_user_by_email(email)
        joined_elsewhere = False

        if user is None:
            if name is None:
                raise InvalidRequestError(
                    "Name is required when adding someone without an account"
                )
            user = User(
                email=AuthService.normalize_email(email),
                name=name,
                hashed_password=None,
                role=role,
                facility=facility if role == UserRole.FACILITY_ADMIN else None,
            )
            self.db.add(user)
            if role == UserRole.DOCTOR:
                self.db.add(DoctorFacility(doctor=user, facility_id=facility.id))
        elif role == UserRole.DOCTOR and user.role == UserRole.DOCTOR:
            link = self._link(user, facility)
            if link is not None and link.is_active and user.is_active:
                raise MemberAlreadyExistsError
            joined_elsewhere = user.is_active and any(
                other.is_active for other in user.clinic_links if other is not link
            )
            if link is None:
                self.db.add(DoctorFacility(doctor=user, facility_id=facility.id))
            else:
                link.is_active = True
            user.is_active = True
        elif self._only_here(user, facility):
            if user.is_active and user.role == role:
                raise MemberAlreadyExistsError
            if user.is_active:
                # Changing someone's role would lose what they can do now.
                raise ConflictError(
                    "This person is already on this facility's staff in another role"
                )
            self._restore(user, facility, role)
        else:
            raise EmailInUseError

        try:
            self.db.commit()
        except IntegrityError:
            # A concurrent request created the account or link first.
            self.db.rollback()
            raise ConflictError(
                "This person was just added by another request; reload and try again"
            ) from None

        member = self._get_member(facility, user.id)
        token = self._invite_token_for(user)
        if token is not None:
            self._email_invite(background_tasks, user, facility, token)
        elif joined_elsewhere:
            background_tasks.add_task(
                send_added_to_facility,
                user_id=user.id,
                email=user.email,
                name=user.name,
                facility_id=facility.id,
                facility_name=facility.name,
            )
        return member, token

    def deactivate_member(self, facility: Facility, member_id: UUID) -> Member:
        """Remove someone from the facility's staff. History is kept.

        An admin's account is deactivated. A doctor is removed from this
        facility only: they take no new bookings here, but keep working at
        their others; their account is deactivated only when this was the
        last facility they were active at."""
        member = self._get_member(facility, member_id)
        if not member.is_active:
            return member
        user = member.user

        if user.role == UserRole.FACILITY_ADMIN:
            # Lock the facility's active admins so two admins removing each
            # other at the same time can't leave the facility with none.
            active_admins = self.db.scalars(
                select(User)
                .where(
                    User.facility_id == facility.id,
                    User.role == UserRole.FACILITY_ADMIN,
                    User.is_active.is_(True),
                )
                .with_for_update()
            ).all()
            if len(active_admins) <= 1:
                raise LastAdminError
            user.is_active = False
        else:
            assert member.link is not None
            member.link.is_active = False
            if not any(link.is_active for link in user.clinic_links):
                user.is_active = False

        self.db.commit()
        return member

    def reissue_invite(
        self,
        facility: Facility,
        member_id: UUID,
        *,
        background_tasks: BackgroundTasks,
    ) -> str:
        member = self._get_member(facility, member_id)
        token = self._invite_token_for(member.user)
        if token is None:
            raise ConflictError("This person has already set a password")
        self._email_invite(background_tasks, member.user, facility, token)
        return token

    @staticmethod
    def _link(user: User, facility: Facility) -> DoctorFacility | None:
        return next(
            (link for link in user.clinic_links if link.facility_id == facility.id),
            None,
        )

    @staticmethod
    def _only_here(user: User, facility: Facility) -> bool:
        """Whether every tie the account has is to this facility, so it may
        be restored here, possibly in the other staff role."""
        if user.role == UserRole.FACILITY_ADMIN:
            return user.facility_id == facility.id
        if user.role == UserRole.DOCTOR:
            return bool(user.clinic_links) and all(
                link.facility_id == facility.id for link in user.clinic_links
            )
        return False

    def _restore(self, user: User, facility: Facility, role: UserRole) -> None:
        """Bring back someone removed from this facility, in `role`."""
        user.role = role
        user.is_active = True
        if role == UserRole.FACILITY_ADMIN:
            user.facility_id = facility.id
            # A former doctor's link stays, inactive, for their history.
        else:
            user.facility_id = None
            link = self._link(user, facility)
            if link is None:
                self.db.add(DoctorFacility(doctor=user, facility_id=facility.id))
            else:
                link.is_active = True

    @staticmethod
    def _email_invite(
        background_tasks: BackgroundTasks,
        user: User,
        facility: Facility,
        token: str,
    ) -> None:
        # The token is also in the response, so the admin can share the link
        # by hand if the email doesn't arrive.
        background_tasks.add_task(
            send_invite,
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            facility_name=facility.name,
            token=token,
        )

    def _get_member(self, facility: Facility, member_id: UUID) -> Member:
        """A member of the facility's staff. Anyone else, including a doctor
        who only works at other facilities, is treated as not found."""
        user = self.db.get(User, member_id)
        if user is None:
            raise MemberNotFoundError
        if user.role == UserRole.FACILITY_ADMIN and user.facility_id == facility.id:
            return Member(user, facility.id)
        if user.role == UserRole.DOCTOR:
            link = self._link(user, facility)
            if link is not None:
                return Member(user, facility.id, link)
        raise MemberNotFoundError

    @staticmethod
    def _invite_token_for(user: User) -> str | None:
        if user.hashed_password is not None:
            return None
        return create_invite_token(user.id)
