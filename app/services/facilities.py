from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.core.permissions import STAFF_ROLES, UserRole
from app.core.security import create_invite_token
from app.models.facility import Facility
from app.models.user import User
from app.services.auth import AuthService


class MemberAlreadyExistsError(ConflictError):
    detail = "This person is already on this facility's staff"


class EmailInUseError(ConflictError):
    detail = "This email already belongs to another account"


class MemberNotFoundError(NotFoundError):
    detail = "Member not found"


class LastAdminError(ConflictError):
    detail = "A facility must keep at least one active admin"


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

    def list_members(self, facility: Facility) -> list[User]:
        """The facility's admins and doctors, including deactivated ones."""
        return list(
            self.db.scalars(
                select(User)
                .where(User.facility_id == facility.id)
                .order_by(User.created_at)
            )
        )

    def add_member(
        self,
        facility: Facility,
        *,
        email: str,
        role: UserRole,
        name: str | None,
    ) -> tuple[User, str | None]:
        """Create a staff account at the facility, or reactivate a
        deactivated one of its own with the given role. Returns the member
        and, if they have never set a password, an invite token to do so.

        An email that belongs to any other account is refused: an account
        has one role, so a patient or another facility's staff can't be
        added."""
        if role not in STAFF_ROLES:
            raise InvalidRequestError("Members must be a facility admin or a doctor")

        user = AuthService(self.db).get_user_by_email(email)

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
                facility=facility,
            )
            self.db.add(user)
        elif user.facility_id != facility.id:
            raise EmailInUseError
        elif user.is_active:
            raise MemberAlreadyExistsError
        else:
            # Re-adding someone who was removed restores their account, and
            # with it their working hours and appointment history.
            user.role = role
            user.is_active = True

        try:
            self.db.commit()
        except IntegrityError:
            # A concurrent request created an account with this email first.
            self.db.rollback()
            raise ConflictError(
                "This person was just added by another request; reload and try again"
            ) from None

        return user, self._invite_token_for(user)

    def deactivate_member(self, facility: Facility, member_id: UUID) -> User:
        """Deactivate a staff account: they can no longer log in, and a
        doctor takes no new bookings. History is kept."""
        member = self._get_member(facility, member_id)
        if not member.is_active:
            return member

        if member.role == UserRole.FACILITY_ADMIN:
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

        member.is_active = False
        self.db.commit()
        return member

    def reissue_invite(self, facility: Facility, member_id: UUID) -> str:
        member = self._get_member(facility, member_id)
        token = self._invite_token_for(member)
        if token is None:
            raise ConflictError("This person has already set a password")
        return token

    def _get_member(self, facility: Facility, member_id: UUID) -> User:
        member = self.db.get(User, member_id)
        # Anyone who isn't on this facility's staff is treated as not found.
        if member is None or member.facility_id != facility.id:
            raise MemberNotFoundError
        return member

    @staticmethod
    def _invite_token_for(user: User) -> str | None:
        if user.hashed_password is not None:
            return None
        return create_invite_token(user.id)
