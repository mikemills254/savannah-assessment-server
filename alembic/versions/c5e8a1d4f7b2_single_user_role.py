"""one role per user: drop facility_members

users.role becomes SUPER_ADMIN/FACILITY_ADMIN/DOCTOR/PATIENT and staff get
users.facility_id. Each facility_members row becomes its user's role and
facility; doctor references in appointments and working hours move from
facility_members.id to users.id.

The upgrade refuses to run if a user has more than one facility role, or a
super admin has any, since one account can no longer hold both.

Revision ID: c5e8a1d4f7b2
Revises: b81c3e5f2a90
Create Date: 2026-10-18 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e8a1d4f7b2'
down_revision: Union[str, Sequence[str], None] = 'b81c3e5f2a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    if conn.execute(sa.text(
        'SELECT EXISTS (SELECT 1 FROM facility_members GROUP BY user_id HAVING count(*) > 1)'
    )).scalar():
        raise RuntimeError(
            'Some users have more than one facility role, and an account can '
            'now have only one. Remove the extra facility_members rows before upgrading.'
        )
    if conn.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM facility_members m JOIN users u ON u.id = m.user_id
            WHERE u.role = 'SUPER_ADMIN'
        )
    """)).scalar():
        raise RuntimeError(
            'Some super admins also have a facility role, and an account can '
            'now have only one. Remove those facility_members rows before upgrading.'
        )

    # Rewrite users.role as text: Postgres can't use a join in ALTER ... USING.
    op.execute('ALTER TABLE users ALTER COLUMN role TYPE varchar(20) USING role::text')
    op.execute('DROP TYPE user_role')

    op.add_column('users', sa.Column('facility_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'users_facility_id_fkey', 'users', 'facilities',
        ['facility_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_index(op.f('ix_users_facility_id'), 'users', ['facility_id'], unique=False)

    # A removed member becomes a deactivated staff account.
    op.execute("""
        UPDATE users u SET
            role = CASE m.role::text WHEN 'ADMIN' THEN 'FACILITY_ADMIN' ELSE 'DOCTOR' END,
            facility_id = m.facility_id,
            is_active = u.is_active AND m.is_active
        FROM facility_members m
        WHERE m.user_id = u.id
    """)
    op.execute("UPDATE users SET role = 'PATIENT' WHERE role = 'USER'")

    postgresql.ENUM(
        'SUPER_ADMIN', 'FACILITY_ADMIN', 'DOCTOR', 'PATIENT', name='user_role'
    ).create(conn)
    op.execute('ALTER TABLE users ALTER COLUMN role TYPE user_role USING role::user_role')
    op.create_check_constraint(
        'ck_user_staff_has_facility', 'users',
        "(role IN ('FACILITY_ADMIN', 'DOCTOR')) = (facility_id IS NOT NULL)",
    )
    op.create_unique_constraint('uq_user_id_facility', 'users', ['id', 'facility_id'])

    op.drop_constraint('doctor_working_hours_doctor_id_fkey', 'doctor_working_hours', type_='foreignkey')
    op.drop_constraint('fk_appointment_doctor_in_facility', 'appointments', type_='foreignkey')
    op.execute("""
        UPDATE doctor_working_hours w SET doctor_id = m.user_id
        FROM facility_members m WHERE m.id = w.doctor_id
    """)
    op.execute("""
        UPDATE appointments a SET doctor_id = m.user_id
        FROM facility_members m WHERE m.id = a.doctor_id
    """)
    op.create_foreign_key(
        'doctor_working_hours_doctor_id_fkey', 'doctor_working_hours', 'users',
        ['doctor_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_appointment_doctor_in_facility', 'appointments', 'users',
        ['doctor_id', 'facility_id'], ['id', 'facility_id'], ondelete='RESTRICT',
    )

    op.drop_index(op.f('ix_facility_members_user_id'), table_name='facility_members')
    op.drop_table('facility_members')
    op.execute('DROP TYPE facility_role')


def downgrade() -> None:
    """Downgrade schema. Each staff user gets one facility_members row whose
    id is their user id, so doctor references stay valid. PATIENT,
    FACILITY_ADMIN and DOCTOR all become USER."""
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
    op.execute("""
        INSERT INTO facility_members (id, facility_id, user_id, role, is_active)
        SELECT
            id, facility_id, id,
            (CASE role::text WHEN 'FACILITY_ADMIN' THEN 'ADMIN' ELSE 'DOCTOR' END)::facility_role,
            is_active
        FROM users
        WHERE facility_id IS NOT NULL
    """)

    op.drop_constraint('fk_appointment_doctor_in_facility', 'appointments', type_='foreignkey')
    op.drop_constraint('doctor_working_hours_doctor_id_fkey', 'doctor_working_hours', type_='foreignkey')
    op.create_foreign_key(
        'fk_appointment_doctor_in_facility', 'appointments', 'facility_members',
        ['doctor_id', 'facility_id'], ['id', 'facility_id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'doctor_working_hours_doctor_id_fkey', 'doctor_working_hours', 'facility_members',
        ['doctor_id'], ['id'], ondelete='RESTRICT',
    )

    op.drop_constraint('uq_user_id_facility', 'users', type_='unique')
    op.drop_constraint('ck_user_staff_has_facility', 'users', type_='check')
    op.drop_index(op.f('ix_users_facility_id'), table_name='users')
    op.drop_constraint('users_facility_id_fkey', 'users', type_='foreignkey')
    op.drop_column('users', 'facility_id')

    op.execute('ALTER TYPE user_role RENAME TO user_role_new')
    postgresql.ENUM('SUPER_ADMIN', 'USER', name='user_role').create(op.get_bind())
    op.execute("""
        ALTER TABLE users ALTER COLUMN role TYPE user_role USING (
            CASE role::text WHEN 'SUPER_ADMIN' THEN 'SUPER_ADMIN' ELSE 'USER' END
        )::user_role
    """)
    op.execute('DROP TYPE user_role_new')
