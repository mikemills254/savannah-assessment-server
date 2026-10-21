from enum import Enum


class UserRole(str, Enum):
    """An account's one role, which also decides the client's dashboard.

    A FACILITY_ADMIN belongs to exactly one facility (User.facility_id); a
    DOCTOR works at one or more (doctor_facilities); SUPER_ADMIN and PATIENT
    belong to none."""

    SUPER_ADMIN = "SUPER_ADMIN"
    FACILITY_ADMIN = "FACILITY_ADMIN"
    DOCTOR = "DOCTOR"
    PATIENT = "PATIENT"


STAFF_ROLES = frozenset({UserRole.FACILITY_ADMIN, UserRole.DOCTOR})


class FacilityPermission(str, Enum):
    MEMBERS_MANAGE = "members:manage"
    SCHEDULES_MANAGE_ANY = "schedules:manage_any"
    SCHEDULE_MANAGE_OWN = "schedule:manage_own"
    APPOINTMENTS_VIEW_ANY = "appointments:view_any"
    APPOINTMENTS_VIEW_OWN = "appointments:view_own"
    APPOINTMENTS_CANCEL_ANY = "appointments:cancel_any"
    APPOINTMENTS_CANCEL_OWN = "appointments:cancel_own"
    # Book on a patient's behalf (phone, walk-in), creating their account if
    # they don't have one, and look up and invite the facility's patients.
    APPOINTMENTS_BOOK_ANY = "appointments:book_any"


# What staff may do at a facility they work at (a doctor at each of theirs).
# "_OWN" permissions still need an
# ownership check in the service layer (e.g. the appointment is with this
# doctor). Booking, viewing and cancelling your own appointments as a
# patient needs no permission.
STAFF_ROLE_PERMISSIONS: dict[UserRole, frozenset[FacilityPermission]] = {
    UserRole.FACILITY_ADMIN: frozenset(FacilityPermission),
    UserRole.DOCTOR: frozenset(
        {
            FacilityPermission.SCHEDULE_MANAGE_OWN,
            FacilityPermission.APPOINTMENTS_VIEW_OWN,
            FacilityPermission.APPOINTMENTS_CANCEL_OWN,
        }
    ),
}

# Super admins set facilities up and appoint their admins, but do not see
# patients' bookings.
SUPER_ADMIN_FACILITY_PERMISSIONS = frozenset({FacilityPermission.MEMBERS_MANAGE})


def facility_permissions(
    role: UserRole,
    *,
    is_own_facility: bool,
) -> frozenset[FacilityPermission]:
    """What a user with this role may do at a facility. Staff only get
    permissions at their own facility."""
    if role == UserRole.SUPER_ADMIN:
        return SUPER_ADMIN_FACILITY_PERMISSIONS
    if is_own_facility:
        return STAFF_ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset()
