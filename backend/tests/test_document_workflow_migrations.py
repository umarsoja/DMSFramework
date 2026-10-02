from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class WorkflowSubmittedVersionMigrationTests(TransactionTestCase):
    migrate_from = [("documents", "0004_alter_document_options_auditevent_actor_assignment_and_more")]
    migrate_to = [("documents", "0005_workflowtask_submitted_version")]

    @classmethod
    def targets_with_documents_at(cls, node, executor):
        return [leaf for leaf in executor.loader.graph.leaf_nodes() if leaf[0] != "documents"] + [node]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        executor = MigrationExecutor(connection)
        cls.from_targets = cls.targets_with_documents_at(cls.migrate_from[0], executor)
        cls.to_targets = cls.targets_with_documents_at(cls.migrate_to[0], executor)
        executor.migrate(cls.from_targets)
        cls.old_apps = executor.loader.project_state(cls.from_targets).apps

    def _fixture_teardown(self):
        # MigrationExecutor owns this test's schema state; flush would erase seeds.
        pass

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.from_targets)
        Document = self.old_apps.get_model("documents", "Document")
        DocumentVersion = self.old_apps.get_model("documents", "DocumentVersion")
        WorkflowInstance = self.old_apps.get_model("documents", "WorkflowInstance")
        WorkflowTask = self.old_apps.get_model("documents", "WorkflowTask")
        AuditEvent = self.old_apps.get_model("documents", "AuditEvent")
        WorkflowStep = self.old_apps.get_model("documents", "WorkflowStep")
        WorkflowDefinition = self.old_apps.get_model("documents", "WorkflowDefinition")
        User = self.old_apps.get_model("auth", "User")

        if getattr(self, "document_pk", None):
            AuditEvent.objects.filter(document_id=self.document_pk).delete()
            WorkflowTask.objects.filter(instance__document_id=self.document_pk).delete()
            WorkflowInstance.objects.filter(document_id=self.document_pk).delete()
            DocumentVersion.objects.filter(document_id=self.document_pk).delete()
            Document.objects.filter(pk=self.document_pk).delete()
        if getattr(self, "definition_pk", None):
            WorkflowStep.objects.filter(definition_id=self.definition_pk).delete()
            WorkflowDefinition.objects.filter(pk=self.definition_pk).delete()
        if getattr(self, "user_pk", None):
            User.objects.filter(pk=self.user_pk).delete()
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDownClass()

    def test_only_unique_submission_audit_matches_are_backfilled(self):
        User = self.old_apps.get_model("auth", "User")
        Document = self.old_apps.get_model("documents", "Document")
        DocumentType = self.old_apps.get_model("documents", "DocumentType")
        DocumentVersion = self.old_apps.get_model("documents", "DocumentVersion")
        WorkflowDefinition = self.old_apps.get_model("documents", "WorkflowDefinition")
        WorkflowStep = self.old_apps.get_model("documents", "WorkflowStep")
        WorkflowInstance = self.old_apps.get_model("documents", "WorkflowInstance")
        WorkflowTask = self.old_apps.get_model("documents", "WorkflowTask")
        AuditEvent = self.old_apps.get_model("documents", "AuditEvent")

        user = User.objects.create(username="version-migration-user")
        document = Document.objects.create(
            document_type=DocumentType.objects.get(code="IM"),
            title="Migration test", created_by=user,
        )
        first_version = DocumentVersion.objects.create(
            document=document, number=1, content={"subject": "First"}, created_by=user,
        )
        second_version = DocumentVersion.objects.create(
            document=document, number=2, content={"subject": "Second"}, created_by=user,
        )
        definition = WorkflowDefinition.objects.create(
            document_type=document.document_type, name="Migration test", version=2,
        )
        step = WorkflowStep.objects.create(definition=definition, sequence=1, name="Review")
        workflow = WorkflowInstance.objects.create(document=document, definition=definition)
        tasks = [
            WorkflowTask.objects.create(instance=workflow, step=step, assigned_to=user, cycle=cycle)
            for cycle in (1, 2, 3)
        ]
        AuditEvent.objects.create(
            document=document, actor=user, action="SUBMITTED", version=first_version, context={"cycle": 1},
        )
        AuditEvent.objects.create(
            document=document, actor=user, action="SUBMITTED", version=first_version, context={"cycle": 3},
        )
        AuditEvent.objects.create(
            document=document, actor=user, action="RESUBMITTED", version=second_version, context={"cycle": 3},
        )
        self.user_pk = user.pk
        self.document_pk = document.pk
        self.definition_pk = definition.pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.to_targets)
        migrated_apps = executor.loader.project_state(self.to_targets).apps
        MigratedTask = migrated_apps.get_model("documents", "WorkflowTask")

        self.assertEqual(MigratedTask.objects.get(pk=tasks[0].pk).submitted_version_id, first_version.pk)
        self.assertIsNone(MigratedTask.objects.get(pk=tasks[1].pk).submitted_version_id)
        self.assertIsNone(MigratedTask.objects.get(pk=tasks[2].pk).submitted_version_id)
