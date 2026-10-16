# Employee onboarding and account activation

Implementation clarification, 7 October 2026.

## Identity and authorization

The existing `User → EmployeeProfile → EmployeeAssignment` identity structure remains authoritative. Department, Position, optional Supervisor, and DMS role are assigned through the existing organization application and `onboard_employee()` transaction. Position titles never grant DMS access. Migrations `organization.0007` and `organization.0008` grant `organization.manage_roles` and `organization.manage_employees`, respectively, to DMS Administrator. Department and position permissions are not granted by these migrations. DMS Workflow Administrator receives `documents.reassign_workflow_tasks` in `organization.0007`. Do not infer or automatically grant other permissions.

## First administrator bootstrap

The normal DMS login continues to require an active employee profile, employed status, current active assignment, active account, and approved DMS group. There is no superuser bypass.

Before any DMS Administrator has an EmployeeProfile, an active Django superuser can use the one-time bootstrap flow at `/accounts/bootstrap/`. The route creates only the first DMS Administrator as an employee account with the existing role group. It closes as soon as that group is assigned to an EmployeeProfile. The bootstrap session does not satisfy the normal DMS access gate; its only application operation is first-administrator provisioning. Bootstrap does not change the employee-management or role-permission grants defined by migrations.

After provisioning, the new administrator establishes a password through the activation link, and all subsequent employee management uses the normal organization application under `organization.manage_employees`.

## Activation

New Django accounts start with an unusable password. Onboarding creates a cryptographically random, 24-hour activation token; only its SHA-256 hash is stored. The raw activation URL is shown once in the onboarding response because email delivery is not configured. Administrators must pass it to the employee through an approved secure channel.

Activation sets the password once and consumes the token. The account status becomes active only if the employee is employed, has a current assignment to active department and position, and has an approved DMS role. Otherwise the password is established while DMS access remains inactive until an administrator completes the required account setup.

The activation endpoint does not send email. Token rows store no plaintext token or password.
