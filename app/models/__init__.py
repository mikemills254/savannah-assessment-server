# Import every model here so SQLAlchemy can resolve relationship names
# (e.g. "Appointment") and Alembic sees all tables on Base.metadata.
from app.models.appointment import Appointment, AppointmentStatus
from app.models.doctor_working_hours import DoctorWorkingHour
from app.models.facility import Facility
from app.models.user import User

__all__ = [
    "Appointment",
    "AppointmentStatus",
    "DoctorWorkingHour",
    "Facility",
    "User",
]
