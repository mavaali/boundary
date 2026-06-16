# Product notes

## Access policy

The service uses role-based access checks for project data:

- Admins have global read access.
- Active project members have scoped read access.
- Inactive users should be denied.

## Roadmap

- Add audit logging for denied access attempts.
- Add an export endpoint after the access checks are covered by tests.

