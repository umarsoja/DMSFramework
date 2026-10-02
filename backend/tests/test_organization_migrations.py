from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AssignmentSnapshotMigrationTests(TransactionTestCase):
    """Existing assignment rows must not be given invented historical labels."""

    migrate_from = [("organization", "0004_organizationauditevent_subject_group_and_more")]
    migrate_to = [("organization", "0005_employeeassignment_department_code_snapshot_and_more")]

    @classmethod
    def targets_with_organization_at(cls, node, executor):
        return [leaf for leaf in executor.loader.graph.leaf_nodes() if leaf[0] != "organization"] + [node]

    def _fixture_teardown(self):
        # TransactionTestCase.flush() would erase rows installed by data
        # migrations (including the Internal Memo workflow seed).
        pass

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.from_targets)
        EmployeeAssignment = self.old_apps.get_model("organization", "EmployeeAssignment")
        EmployeeProfile = self.old_apps.get_model("organization", "EmployeeProfile")
        Department = self.old_apps.get_model("organization", "Department")
        Position = self.old_apps.get_model("organization", "Position")
        User = self.old_apps.get_model("auth", "User")
        EmployeeAssignment.objects.filter(pk=getattr(self, "assignment_pk", None)).delete()
        EmployeeProfile.objects.filter(pk=getattr(self, "profile_pk", None)).delete()
        Department.objects.filter(pk=getattr(self, "department_pk", None)).delete()
        Position.objects.filter(pk=getattr(self, "position_pk", None)).delete()
        User.objects.filter(pk=getattr(self, "user_pk", None)).delete()
        super().tearDown()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        executor = MigrationExecutor(connection)
        cls.from_targets = cls.targets_with_organization_at(cls.migrate_from[0], executor)
        cls.to_targets = cls.targets_with_organization_at(cls.migrate_to[0], executor)
        executor.migrate(cls.from_targets)
        cls.old_apps = executor.loader.project_state(cls.from_targets).apps

    @classmethod
    def tearDownClass(cls):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDownClass()

    def test_forward_migration_keeps_existing_snapshots_unknown(self):
        User = self.old_apps.get_model("auth", "User")
        Department = self.old_apps.get_model("organization", "Department")
        Position = self.old_apps.get_model("organization", "Position")
        EmployeeProfile = self.old_apps.get_model("organization", "EmployeeProfile")
        EmployeeAssignment = self.old_apps.get_model("organization", "EmployeeAssignment")

        user = User.objects.create(username="historical-employee", first_name="Amina", last_name="Old")
        profile = EmployeeProfile.objects.create(user=user, staff_id="HIST-001")
        department = Department.objects.create(code="OPS", name="Old Operations")
        position = Position.objects.create(code="ENG", title="Old Engineer")
        assignment = EmployeeAssignment.objects.create(
            employee=profile, department=department, position=position, start_date="2020-01-01"
        )
        self.user_pk = user.pk
        self.profile_pk = profile.pk
        self.department_pk = department.pk
        self.position_pk = position.pk
        self.assignment_pk = assignment.pk
        department.name = "Current Operations"
        department.save(update_fields=["name"])
        position.title = "Current Engineer"
        position.save(update_fields=["title"])

        executor = MigrationExecutor(connection)
        executor.migrate(self.to_targets)
        migrated_apps = executor.loader.project_state(self.to_targets).apps
        MigratedAssignment = migrated_apps.get_model("organization", "EmployeeAssignment")
        migrated = MigratedAssignment.objects.get(pk=assignment.pk)
        self.assertEqual(
            [migrated.department_code_snapshot, migrated.department_name_snapshot,
             migrated.position_code_snapshot, migrated.position_title_snapshot,
             migrated.supervisor_staff_id_snapshot, migrated.supervisor_name_snapshot],
            ["", "", "", "", "", ""],
        )
        self.assertEqual(migrated.department.name, "Current Operations")
        self.assertEqual(migrated.position.title, "Current Engineer")

        executor = MigrationExecutor(connection)
        executor.migrate(self.from_targets)
        rolled_back_apps = executor.loader.project_state(self.from_targets).apps
        RolledBackAssignment = rolled_back_apps.get_model("organization", "EmployeeAssignment")
        self.assertTrue(RolledBackAssignment.objects.filter(pk=assignment.pk).exists())
