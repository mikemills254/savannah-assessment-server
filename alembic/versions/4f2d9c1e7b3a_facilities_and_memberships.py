"""facilities, memberships and platform roles

Replaces the doctors/patients profile tables with facilities and
facility_members, moves name onto users, and changes users.role from
ADMIN/DOCTOR/PATIENT to SUPER_ADMIN/USER.

Existing users are kept. Doctors, appointments and working hours can't be
carried over (doctors had no facility), so the upgrade refuses to run if
those tables have rows.

Revision ID: 4f2d9c1e7b3a
Revises: a793658c8b98
Create Date: 2026-10-18 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4f2d9c1e7b3a'
down_revision: Union[str, Sequence[str], None] = 'a793658c8b98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Already exists from the initial migration; reused, not recreated.
appointment_status = postgresql.ENUM(
    'BOOKED', 'CANCELLED', name='appointment_status', create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    for table in ('doctors', 'appointments', 'doctor_working_hours'):
        if conn.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table})')).scalar():
            raise RuntimeError(
                f'{table} has rows, which cannot be assigned to a facility '
                'automatically. Move or delete them before upgrading.'
            )

    # users.name, taken from the patient/doctor profile it used to live on.
    op.add_column('users', sa.Column('name', sa.String(length=100), nullable=True))
    op.execute("""
        UPDATE users u SET name = COALESCE(
            (SELECT p.name FROM patients p WHERE p.user_id = u.id),
            (SELECT d.name FROM doctors d WHERE d.user_id = u.id),
            split_part(u.email, '@', 1)
        )
    """)
    op.alter_column('users', 'name', existing_type=sa.String(length=100), nullable=False)
    op.alter_column('users', 'hashed_password', existing_type=sa.String(length=255), nullable=True)

    # users.role: ADMIN -> SUPER_ADMIN, DOCTOR/PATIENT -> USER.
    op.execute('ALTER TYPE user_role RENAME TO user_role_old')
    postgresql.ENUM('SUPER_ADMIN', 'USER', name='user_role').create(conn)
    op.execute("""
        ALTER TABLE users ALTER COLUMN role TYPE user_role USING (
            CASE role::text WHEN 'ADMIN' THEN 'SUPER_ADMIN' ELSE 'USER' END
        )::user_role
    """)
    op.execute('DROP TYPE user_role_old')

    op.drop_table('doctor_working_hours')
    op.drop_table('appointments')
    op.drop_table('patients')
    op.drop_table('doctors')

    op.create_table('facilities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('facility_members',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('facility_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('ADMIN', 'DOCTOR', name='facility_role'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['facility_id'], ['facilities.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('facility_id', 'user_id', 'role', name='uq_facility_member_role'),
    sa.UniqueConstraint('id', 'facility_id', name='uq_facility_member_id_facility')
    )
    op.create_index(op.f('ix_facility_members_user_id'), 'facility_members', ['user_id'], unique=False)
    op.create_table('doctor_working_hours',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('doctor_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.CheckConstraint('start_time < end_time', name='ck_working_hours_valid_period'),
    sa.CheckConstraint('weekday >= 0 AND weekday <= 6', name='ck_working_hours_weekday'),
    sa.ForeignKeyConstraint(['doctor_id'], ['facility_members.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_working_hours_doctor_weekday', 'doctor_working_hours', ['doctor_id', 'weekday'], unique=False)
    op.create_table('appointments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('facility_id', sa.Uuid(), nullable=False),
    sa.Column('doctor_id', sa.Uuid(), nullable=False),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', appointment_status, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('ends_at > starts_at', name='ck_appointment_valid_period'),
    sa.ForeignKeyConstraint(['doctor_id', 'facility_id'], ['facility_members.id', 'facility_members.facility_id'], name='fk_appointment_doctor_in_facility', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['facility_id'], ['facilities.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['patient_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_appointments_facility_starts_at', 'appointments', ['facility_id', 'starts_at'], unique=False)
    op.create_index('ix_appointments_patient_starts_at', 'appointments', ['patient_id', 'starts_at'], unique=False)
    op.create_index('uq_active_doctor_slot', 'appointments', ['doctor_id', 'starts_at'], unique=True, postgresql_where=sa.text("status = 'BOOKED'"))


def downgrade() -> None:
    """Downgrade schema.

    Lossy: facilities, memberships, appointments and working hours are
    dropped, invited users who never set a password are deleted, and every
    remaining user becomes a PATIENT (SUPER_ADMIN becomes ADMIN).
    """
    op.drop_table('appointments')
    op.drop_table('doctor_working_hours')
    op.drop_table('facility_members')
    op.drop_table('facilities')
    op.execute('DROP TYPE facility_role')

    op.execute('DELETE FROM users WHERE hashed_password IS NULL')
    op.alter_column('users', 'hashed_password', existing_type=sa.String(length=255), nullable=False)

    op.execute('ALTER TYPE user_role RENAME TO user_role_new')
    postgresql.ENUM('ADMIN', 'DOCTOR', 'PATIENT', name='user_role').create(op.get_bind())
    op.execute("""
        ALTER TABLE users ALTER COLUMN role TYPE user_role USING (
            CASE role::text WHEN 'SUPER_ADMIN' THEN 'ADMIN' ELSE 'PATIENT' END
        )::user_role
    """)
    op.execute('DROP TYPE user_role_new')

    op.create_table('doctors',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('patients',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.execute("""
        INSERT INTO patients (id, user_id, name)
        SELECT gen_random_uuid(), id, name FROM users WHERE role = 'PATIENT'
    """)
    op.drop_column('users', 'name')

    op.create_table('appointments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('doctor_id', sa.Uuid(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', appointment_status, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('ends_at > starts_at', name='ck_appointment_valid_period'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_appointments_patient_starts_at', 'appointments', ['patient_id', 'starts_at'], unique=False)
    op.create_index('uq_active_doctor_slot', 'appointments', ['doctor_id', 'starts_at'], unique=True, postgresql_where=sa.text("status = 'BOOKED'"))
    op.create_table('doctor_working_hours',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('doctor_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.CheckConstraint('start_time < end_time', name='ck_working_hours_valid_period'),
    sa.CheckConstraint('weekday >= 0 AND weekday <= 6', name='ck_working_hours_weekday'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_working_hours_doctor_weekday', 'doctor_working_hours', ['doctor_id', 'weekday'], unique=False)
