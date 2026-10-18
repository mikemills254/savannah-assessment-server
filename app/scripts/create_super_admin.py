"""Create a super admin, or promote an existing user to one.

Usage:
    python -m app.scripts.create_super_admin --email you@example.com --name "Your Name"

Super admins can't be created through the API, so whoever reaches a fresh
deployment first can't make themselves one.
"""

import argparse
import getpass
import sys

from app.core.permissions import UserRole
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.user import User
from app.services.auth import AuthService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", help="Required when creating a new user")
    args = parser.parse_args()

    with SessionLocal() as db:
        auth = AuthService(db)
        user = auth.get_user_by_email(args.email)

        if user is not None:
            if user.facility_id is not None:
                # Staff are tied to their facility's records; an account has one role.
                print(
                    f"{user.email} is {user.role.value} at a facility and can't "
                    "be promoted; use a different email.",
                    file=sys.stderr,
                )
                return 1
            user.role = UserRole.SUPER_ADMIN
            db.commit()
            print(f"Promoted {user.email} to super admin.")
            return 0

        if not args.name:
            print("--name is required to create a new user.", file=sys.stderr)
            return 1

        password = getpass.getpass("Password: ")
        if len(password) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            return 1
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords do not match.", file=sys.stderr)
            return 1

        user = User(
            email=auth.normalize_email(args.email),
            name=args.name.strip(),
            hashed_password=hash_password(password),
            role=UserRole.SUPER_ADMIN,
        )
        db.add(user)
        db.commit()
        print(f"Created super admin {user.email}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
