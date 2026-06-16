from src.access import User, can_read_project


def check_active_member_can_read_project():
    user = User(id="u1", active=True, is_admin=False, project_ids=("p1",))

    assert can_read_project(user, "p1") is True


def check_inactive_member_is_denied():
    user = User(id="u1", active=False, is_admin=False, project_ids=("p1",))

    assert can_read_project(user, "p1") is False


def check_admin_can_read_any_project():
    user = User(id="admin", active=True, is_admin=True, project_ids=())

    assert can_read_project(user, "p2") is True
