"""doctors at several facilities: doctor_facilities

A doctor can now work at more than one facility. Each doctor's
users.facility_id becomes a doctor_facilities row (active if the doctor
was), and users.facility_id is kept for facility admins only. Working hours
gain the facility they apply at, and appointments and working hours now
reference (doctor_id, facility_id) in doctor_facilities, so a doctor can
only be booked, or keep hours, at a facility they work at.

The downgrade refuses to run if a doctor works at more than one facility,
since the old schema can only hold one.

Revision ID: 9b4e2c7d1a35
Revises: e6b2d8f4a1c9
Create Date: 2026-10-21 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b4e2c7d1a35'
down_revision: Union[str, Sequence[str], None] = 'e6b2d8f4a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'doctor_facilities',
        sa.Column('doctor_id', sa.Uuid(), nullable=False),
        sa.Column('facility_id', sa.Uuid(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doctor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['facility_id'], ['facilities.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('doctor_id', 'facility_id'),
    )
    op.create_index(op.f('ix_doctor_facilities_facility_id'), 'doctor_facilities', ['facility_id'], unique=False)
    op.execute("""
        INSERT INTO doctor_facilities (doctor_id, facility_id, is_active, created_at)
        SELECT id, facility_id, is_active, created_at FROM users WHERE role = 'DOCTOR'
    """)

    # Working hours: the facility they apply at is the doctor's only one so far.
    op.add_column('doctor_working_hours', sa.Column('facility_id', sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE doctor_working_hours w SET facility_id = u.facility_id
        FROM users u WHERE u.id = w.doctor_id
    """)
    op.alter_column('doctor_working_hours', 'facility_id', nullable=False)
    op.create_foreign_key(
        'doctor_working_hours_facility_id_fkey', 'doctor_working_hours', 'facilities',
        ['facility_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_working_hours_doctor_in_facility', 'doctor_working_hours', 'doctor_facilities',
        ['doctor_id', 'facility_id'], ['doctor_id', 'facility_id'], ondelete='RESTRICT',
    )
    op.create_index('ix_working_hours_facility_doctor', 'doctor_working_hours', ['facility_id', 'doctor_id'], unique=False)

    # Appointments: the doctor must work at the facility, via doctor_facilities.
    op.drop_constraint('fk_appointment_doctor_in_facility', 'appointments', type_='foreignkey')
    op.create_foreign_key(
        'fk_appointment_doctor_in_facility', 'appointments', 'doctor_facilities',
        ['doctor_id', 'facility_id'], ['doctor_id', 'facility_id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_appointments_doctor', 'appointments', 'users',
        ['doctor_id'], ['id'], ondelete='RESTRICT',
    )

    # users.facility_id is now for facility admins only.
    op.drop_constraint('uq_user_id_facility', 'users', type_='unique')
    op.drop_constraint('ck_user_staff_has_facility', 'users', type_='check')
    op.execute("UPDATE users SET facility_id = NULL WHERE role = 'DOCTOR'")
    op.create_check_constraint(
        'ck_user_admin_has_facility', 'users',
        "(role = 'FACILITY_ADMIN') = (facility_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    if conn.execute(sa.text(
        'SELECT EXISTS (SELECT 1 FROM doctor_facilities GROUP BY doctor_id HAVING count(*) > 1)'
    )).scalar():
        raise RuntimeError(
            'Some doctors work at more than one facility, and before this '
            'revision a doctor could have only one. Remove the extra '
            'doctor_facilities rows (and their hours and appointments) first.'
        )

    op.drop_constraint('ck_user_admin_has_facility', 'users', type_='check')
    op.execute("""
        UPDATE users u SET facility_id = d.facility_id
        FROM doctor_facilities d WHERE d.doctor_id = u.id
    """)
    op.create_check_constraint(
        'ck_user_staff_has_facility', 'users',
        "(role IN ('FACILITY_ADMIN', 'DOCTOR')) = (facility_id IS NOT NULL)",
    )
    op.create_unique_constraint('uq_user_id_facility', 'users', ['id', 'facility_id'])

    op.drop_constraint('fk_appointments_doctor', 'appointments', type_='foreignkey')
    op.drop_constraint('fk_appointment_doctor_in_facility', 'appointments', type_='foreignkey')
    op.create_foreign_key(
        'fk_appointment_doctor_in_facility', 'appointments', 'users',
        ['doctor_id', 'facility_id'], ['id', 'facility_id'], ondelete='RESTRICT',
    )

    op.drop_index('ix_working_hours_facility_doctor', table_name='doctor_working_hours')
    op.drop_constraint('fk_working_hours_doctor_in_facility', 'doctor_working_hours', type_='foreignkey')
    op.drop_constraint('doctor_working_hours_facility_id_fkey', 'doctor_working_hours', type_='foreignkey')
    op.drop_column('doctor_working_hours', 'facility_id')

    op.drop_index(op.f('ix_doctor_facilities_facility_id'), table_name='doctor_facilities')
    op.drop_table('doctor_facilities')
