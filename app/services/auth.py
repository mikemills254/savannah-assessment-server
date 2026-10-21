from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.errors import ConflictError, InvalidRequestError
from app.core.google import GoogleIdentity
from app.core.permissions import UserRole
from app.core.security import (
    create_password_reset_token,
    decode_invite_token,
    decode_password_reset_token,
    fingerprint_matches,
    hash_password,
    verify_password,
)
from app.models.doctor_facility import DoctorFacility
from app.models.facility import Facility
from app.models.user import User

# Verified against when the email is unknown, so a failed login takes the
# same time whether or not the account exists.
_DUMMY_HASH = hash_password("dummy-password-for-timing")

# Where a user works, loaded with them in one query: an admin's facility and
# a doctor's facility links. The auth routes send it back as
# MeRead.facilities, and permission checks read it (User.works_at).
WITH_FACILITIES = (
    joinedload(User.facility),
    joinedload(User.clinic_links).joinedload(DoctorFacility.facility),
)


class EmailAlreadyRegisteredError(ConflictError):
    detail = "Email is already registered"


class InvalidInviteError(InvalidRequestError):
    detail = "Invite is invalid, expired or already used"


class InvalidResetLinkError(InvalidRequestError):
    detail = "This reset link is invalid, expired or already used. Ask for a new one."


class GoogleAccountMismatchError(ConflictError):
    detail = "This email's account is linked to a different Google account"


class AuthService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    def get_user_by_email(self, email: str) -> User | None:
        return (
            self.db.scalars(
                select(User)
                .where(User.email == self.normalize_email(email))
                .options(*WITH_FACILITIES)
            )
            .unique()
            .first()
        )

    def register(self, *, name: str, email: str, password: str) -> User:
        """Public sign-up of a PATIENT. Super admins are created with
        app.scripts.create_super_admin."""
        user = self._add_user(
            name=name,
            email=email,
            password=password,
            role=UserRole.PATIENT,
        )
        self._commit_registration()
        return user

    def register_facility_admin(
        self,
        *,
        name: str,
        email: str,
        password: str,
        facility_name: str,
        timezone: str,
    ) -> User:
        """Sign-up of a FACILITY_ADMIN with their facility, which is live
        straight away. Both are committed together, so a failure leaves
        neither behind."""
        facility = Facility(name=facility_name, timezone=timezone)
        self.db.add(facility)
        user = self._add_user(
            name=name,
            email=email,
            password=password,
            role=UserRole.FACILITY_ADMIN,
            facility=facility,
        )
        self._commit_registration()
        return user

    def _add_user(
        self,
        *,
        name: str,
        email: str,
        password: str,
        role: UserRole,
        facility: Facility | None = None,
    ) -> User:
        email = self.normalize_email(email)
        # Also covers invited users who haven't set a password: they must use
        # their invite, or anyone knowing their email could claim the account.
        if self.get_user_by_email(email) is not None:
            raise EmailAlreadyRegisteredError

        user = User(
            name=name,
            email=email,
            hashed_password=hash_password(password),
            role=role,
            facility=facility,
        )
        self.db.add(user)
        return user

    def _commit_registration(self) -> None:
        try:
            self.db.commit()
        except IntegrityError:
            # Another request registered the same email between our check and commit.
            self.db.rollback()
            raise EmailAlreadyRegisteredError from None

    def login(self, *, email: str, password: str) -> User | None:
        """Return the user if the credentials are valid and the account can
        log in, otherwise None. Callers must not reveal which check failed."""
        user = self.get_user_by_email(email)
        if user is None or user.hashed_password is None:
            verify_password(password, _DUMMY_HASH)
            return None

        if not verify_password(password, user.hashed_password) or not user.is_active:
            return None

        return user

    def login_with_google(self, identity: GoogleIdentity) -> User | None:
        """Sign in with a verified Google account. On first use it is linked
        to the account with the same email (including an invited one), or a
        new PATIENT is created. Returns None if the account is deactivated."""
        user = (
            self.db.scalars(
                select(User)
                .where(User.google_sub == identity.sub)
                .options(*WITH_FACILITIES)
            )
            .unique()
            .first()
        )
        if user is not None:
            return user if user.is_active else None

        user = self.get_user_by_email(identity.email)
        if user is None:
            local_part = identity.email.split("@", 1)[0]
            user = User(
                name=((identity.name or "").strip() or local_part)[:100],
                email=self.normalize_email(identity.email),
                hashed_password=None,
                role=UserRole.PATIENT,
            )
            self.db.add(user)
        elif not user.is_active:
            return None
        elif user.google_sub is not None:
            raise GoogleAccountMismatchError

        user.google_sub = identity.sub
        try:
            self.db.commit()
        except IntegrityError:
            # A concurrent sign-in created or linked the same account first.
            self.db.rollback()
            raise ConflictError(
                "Sign-in conflicted with another request; try again"
            ) from None

        return user

    def accept_invite(self, *, token: str, password: str) -> User:
        """Set the first password of an invited user. The invite stops
        working once a password is set, so it can only be used once."""
        user_id = decode_invite_token(token)
        user = (
            self.db.get(
                User,
                user_id,
                options=list(WITH_FACILITIES),
                # Only the user's row: Postgres can't lock the nullable side
                # of the outer join the facility is loaded with.
                with_for_update={"of": User},
            )
            if user_id is not None
            else None
        )
        if user is None or user.hashed_password is not None or not user.is_active:
            raise InvalidInviteError

        user.hashed_password = hash_password(password)
        self.db.commit()
        return user

    def request_password_reset(self, email: str) -> tuple[User, str] | None:
        """The account and a reset token, or None if there's no active
        account with this email. The route answers the same either way, so
        it can't be used to find out who has an account.

        Invited users who never set a password can use it too: proving they
        own the email is all an invite does."""
        user = self.get_user_by_email(email)
        if user is None or not user.is_active:
            return None
        return user, create_password_reset_token(user.id, user.hashed_password)

    def reset_password(self, *, token: str, password: str) -> User:
        """Set a new password from a reset link. The token carries a
        fingerprint of the password it was issued against, so it stops
        working as soon as any password is set: each link works once."""
        claims = decode_password_reset_token(token)
        user = (
            self.db.get(
                User,
                claims[0],
                options=list(WITH_FACILITIES),
                # Two uses of one link at once: the second waits, then sees
                # the changed password and is refused.
                with_for_update={"of": User},
            )
            if claims is not None
            else None
        )
        if (
            user is None
            or not user.is_active
            or not fingerprint_matches(claims[1], user.hashed_password)
        ):
            raise InvalidResetLinkError

        user.hashed_password = hash_password(password)
        self.db.commit()
        return user
