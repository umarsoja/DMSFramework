/* Shared enhancements only. Page-specific behavior belongs in extra_js. */
(() => {
  'use strict';

  document.addEventListener('alpine:init', () => {
    Alpine.data('employeeOnboardingWizard', (options = {}) => ({
      currentStep: Number(options.initialStep || 1),
      accountMode: options.accountMode || 'create',
      formValues: {},
      steps: [
        { number: 1, title: 'Account Identity' },
        { number: 2, title: 'Organisation & Access' },
        { number: 3, title: 'Review & Onboard' },
      ],

      init() {
        document.getElementById('employee-onboarding-form')?.querySelectorAll('[name]').forEach((field) => {
          if (field.name === 'account_mode') return;
          this.formValues[field.name] = field.multiple
            ? [...field.selectedOptions].map((option) => option.value)
            : field.value;
        });
      },

      goTo(step) {
        if (step <= this.currentStep) {
          this.currentStep = step;
          return;
        }
        if (step === this.currentStep + 1 && this.validateCurrentStep()) {
          this.currentStep = step;
        }
      },

      next() {
        if (this.currentStep >= 3) return;
        if (!this.validateCurrentStep()) return;
        this.currentStep += 1;
      },

      back() {
        this.currentStep = Math.max(1, this.currentStep - 1);
      },

      visibleField(name, panel) {
        const scope = panel || document.getElementById('employee-onboarding-form');
        return [...(scope?.querySelectorAll(`[name="${name}"]`) || [])]
          .find((field) => field.getClientRects().length > 0 && !field.disabled) || null;
      },

      setFieldError(field, message) {
        if (!field) return;
        const error = document.getElementById(`onboarding-client-error-${field.name}`);
        if (error) {
          error.textContent = message;
          error.hidden = false;
          error.style.display = 'block';
        }
        field.setAttribute('aria-invalid', 'true');
        const describedBy = new Set((field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
        describedBy.add(`onboarding-client-error-${field.name}`);
        field.setAttribute('aria-describedby', [...describedBy].join(' '));
        field.setCustomValidity(message);
      },

      clearStepErrors(panel) {
        panel.querySelectorAll('[name]').forEach((field) => {
          field.setCustomValidity('');
          field.removeAttribute('aria-invalid');
          const clientErrorId = `onboarding-client-error-${field.name}`;
          const describedBy = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter((id) => id && id !== clientErrorId);
          if (describedBy.length) field.setAttribute('aria-describedby', describedBy.join(' '));
          else field.removeAttribute('aria-describedby');
        });
        panel.querySelectorAll('[id^="onboarding-client-error-"]').forEach((error) => {
          error.textContent = '';
          error.hidden = true;
          error.style.display = 'none';
        });
      },

      focusInvalid(field, message) {
        this.setFieldError(field, message);
        field.focus();
      },

      usernamePreview() {
        const normalize = (value) => value.normalize('NFKD')
          .replace(/[\u0300-\u036f]/g, '')
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, '.')
          .replace(/^\.+|\.+$/g, '');
        const parts = [normalize(this.formValues.first_name || ''), normalize(this.formValues.last_name || '')].filter(Boolean);
        return parts.join('.') || 'employee';
      },

      selectedLabel(name, fallback = 'Not selected') {
        const selectedValue = this.formValues[name];
        const field = document.getElementById('employee-onboarding-form')?.querySelector(`[name="${name}"]`);
        if (!field) return fallback;
        if (field.multiple) {
          return [...field.selectedOptions].map((option) => option.textContent.trim()).join(', ') || fallback;
        }
        if (!selectedValue) return fallback;
        return field.selectedOptions?.[0]?.textContent.trim() || selectedValue || fallback;
      },

      value(name, fallback = 'Not provided') {
        const value = this.formValues[name];
        return (Array.isArray(value) ? value.join(', ') : String(value || '').trim()) || fallback;
      },

      selectedItems(name) {
        const field = document.getElementById('employee-onboarding-form')?.querySelector(`[name="${name}"]`);
        if (!field?.multiple) return [];
        return [...field.selectedOptions].map((option) => ({value: option.value, label: option.textContent.trim()}));
      },

      validateCurrentStep() {
        const form = document.getElementById('employee-onboarding-form');
        const panel = form.querySelector(`[data-wizard-step="${this.currentStep}"]`);
        if (!panel) return false;
        this.clearStepErrors(panel);

        if (this.currentStep === 1) {
          const requiredNames = this.accountMode === 'create' ? ['first_name', 'last_name', 'email'] : ['existing_user'];
          for (const name of requiredNames) {
            const field = this.visibleField(name, panel);
            if (!field) continue;
            const value = String(this.formValues[name] || '').trim();
            let message = value ? '' : 'This field is required.';
            if (name === 'email' && value && field.validity.typeMismatch) message = 'Enter a valid email address.';
            if (message) {
              this.focusInvalid(field, message);
              return false;
            }
          }

          if (this.accountMode === 'link') {
            const selected = this.visibleField('existing_user', panel)?.selectedOptions?.[0];
            const existingEmail = selected?.textContent.split('·').at(-1)?.trim();
            const email = this.visibleField('email', panel);
            const value = String(this.formValues.email || '').trim();
            const message = !existingEmail && !value
              ? 'Enter an official email for this account before continuing.'
              : value && email.validity.typeMismatch ? 'Enter a valid email address.' : '';
            if (message) {
              this.focusInvalid(email, message);
              return false;
            }
          }
        }

        const invalid = [...panel.querySelectorAll(':invalid:not([disabled])')]
          .find((field) => field.getClientRects().length > 0);
        if (invalid) {
          this.focusInvalid(invalid, invalid.validationMessage || 'Enter a valid value.');
          return false;
        }
        return true;
      },

      employeeName() {
        if (this.accountMode === 'link') return this.selectedLabel('existing_user', 'Selected existing account');
        return `${this.value('first_name', '')} ${this.value('last_name', '')}`.trim() || 'Not provided';
      },

      linkedAccountPart(index, fallback) {
        const parts = this.selectedLabel('existing_user', '').split('·').map((part) => part.trim());
        return parts[index] || fallback;
      },

      accountUsername() {
        return this.accountMode === 'link' ? this.linkedAccountPart(1, 'Existing username') : this.usernamePreview();
      },

      accountEmail() {
        return this.accountMode === 'link' ? this.linkedAccountPart(2, 'Saved account email') : this.value('email');
      },

      accountModeLabel() {
        return this.accountMode === 'link' ? 'Linked existing account' : 'New employee account';
      },

      assignmentDate() {
        return this.value('start_date', 'Not provided');
      },
    }));

    Alpine.data('recordConfirmation', () => ({
      confirmed: false,
      submitterName: '',
      submitterValue: '',
      confirmationTitle: '',
      confirmationMessage: '',
      confirmationLabel: '',
      changedFields: {},

      recordChange(field) {
        if (!field.name || field.name === 'csrfmiddlewaretoken') return;
        const label = field.labels?.[0]?.textContent.replace('*', '').trim() || field.name;
        let value;
        if (field.multiple) {
          value = [...field.selectedOptions].map((option) => option.textContent.trim()).join(', ') || 'None selected';
        } else if (field.type === 'checkbox') {
          value = field.checked ? 'Active' : 'Inactive';
        } else if (field.tagName === 'SELECT') {
          value = field.selectedOptions[0]?.textContent.trim() || 'None selected';
        } else {
          value = field.value.trim();
        }
        if (value.length > 120) value = `${value.slice(0, 117)}…`;
        this.changedFields[field.name] = `${label}: ${value || 'Cleared'}`;
      },

      confirmSubmit(event) {
        if (this.confirmed) return;
        event.preventDefault();
        const form = event.currentTarget;
        const kind = form.dataset.confirmKind;
        const mode = form.dataset.confirmMode || 'update';
        const name = form.dataset.recordName || '';
        const details = Object.values(this.changedFields);
        const summary = details.length ? ` Changes: ${details.join('; ')}.` : '';
        const field = (fieldName) => form.elements.namedItem(fieldName);
        const selectedText = (fieldName, fallback = 'not assigned') => {
          const control = field(fieldName);
          if (!control) return fallback;
          if (control.multiple) return [...control.selectedOptions].map((option) => option.textContent.trim()).join(', ') || fallback;
          if (!control.value) return fallback;
          return control.selectedOptions?.[0]?.textContent.trim() || fallback;
        };
        const textValue = (fieldName) => field(fieldName)?.value.trim() || '';

        this.submitterName = event.submitter?.name || '';
        this.submitterValue = event.submitter?.value || '';

        if (kind === 'employee' && mode === 'create') {
          const linkedAccount = selectedText('existing_user', '');
          const employeeName = linkedAccount || `${textValue('first_name')} ${textValue('last_name')}`.trim() || 'the entered employee';
          this.confirmationTitle = `Create employee ${employeeName}?`;
          this.confirmationMessage = `Create an employee record with ${selectedText('position', 'the selected position')} in ${selectedText('department', 'the selected department')}, reporting to ${selectedText('supervisor')}. DMS roles: ${selectedText('roles', 'none selected')}.`;
          this.confirmationLabel = 'Create Employee';
        } else if (kind === 'employee') {
          const changed = Object.keys(this.changedFields);
          const employmentStatus = field('employment_status');
          if (changed.includes('employment_status') && employmentStatus?.value === 'SEPARATED') {
            this.confirmationLabel = 'Record Separation';
            this.confirmationTitle = `Record separation for ${name}?`;
            this.confirmationMessage = `Separate ${name} and apply the submitted account and assignment changes.${summary}`;
          } else {
          const status = field('account_status');
          const initialStatus = form.dataset.initialAccountStatus;
          const statusChanged = status && status.value !== initialStatus;
          if (statusChanged) {
            const actions = {
              ACTIVE: { label: 'Activate Account', verb: 'Activate' },
              INACTIVE: { label: 'Deactivate Account', verb: 'Deactivate' },
              SUSPENDED: { label: 'Suspend Account', verb: 'Suspend' },
            };
            const action = actions[status.value] || { label: 'Change Account Status', verb: 'Change the status of' };
            this.confirmationLabel = action.label;
            this.confirmationTitle = `${action.verb} ${name}'s account?`;
            this.confirmationMessage = `Change the account status for ${name} to ${status.selectedOptions[0]?.textContent.trim()}.${summary}`;
          } else {
            if (changed.length === 1 && changed[0] === 'supervisor') this.confirmationLabel = field('supervisor')?.value ? 'Assign Supervisor' : 'Remove Supervisor';
            else if (changed.length === 1 && changed[0] === 'roles') this.confirmationLabel = 'Update DMS Roles';
            else if (changed.some((key) => ['department', 'position', 'start_date'].includes(key))) this.confirmationLabel = 'Update Assignment';
            else this.confirmationLabel = 'Save Changes';
            this.confirmationTitle = `Update employee ${name}?`;
            this.confirmationMessage = `Save the employee profile and organisational changes for ${name}.${summary}`;
          }
          }
        } else if (kind === 'department' || kind === 'position') {
          const recordType = kind === 'department' ? 'Department' : 'Position';
          const recordName = mode === 'create'
            ? (kind === 'department' ? textValue('name') : textValue('title'))
            : name;
          const active = field('is_active');
          const initialActive = form.dataset.initialActive === 'true';
          const statusChanged = mode === 'update' && active && active.checked !== initialActive;
          if (statusChanged) {
            this.confirmationLabel = active.checked ? `Activate ${recordType}` : `Deactivate ${recordType}`;
            this.confirmationTitle = `${this.confirmationLabel} ${recordName}?`;
            this.confirmationMessage = active.checked
              ? `Make ${recordName} available for new assignments.${summary}`
              : `Deactivate ${recordName}. It will no longer be available for new assignments; existing historical references will remain.${summary}`;
          } else if (mode === 'create') {
            this.confirmationTitle = `Create ${recordType.toLowerCase()}${recordName ? ` ${recordName}` : ''}?`;
            this.confirmationMessage = kind === 'department'
              ? `Create ${recordType.toLowerCase()} ${recordName} with its selected parent and department head.${summary}`
              : `Create ${recordType.toLowerCase()} ${recordName} with its selected department scope.${summary}`;
            this.confirmationLabel = `Create ${recordType}`;
          } else {
            const changed = Object.keys(this.changedFields);
            this.confirmationLabel = kind === 'department' && changed.length === 1 && changed[0] === 'head'
              ? 'Assign Department Head'
              : kind === 'department' && changed.length === 1 && changed[0] === 'parent'
                ? 'Update Department Hierarchy'
                : `Update ${recordType}`;
            this.confirmationTitle = `Update ${recordType.toLowerCase()} ${recordName}?`;
            this.confirmationMessage = `Save the changes to ${recordName}.${summary}`;
          }
        } else if (kind === 'bootstrap') {
          const employeeName = `${textValue('first_name')} ${textValue('last_name')}`.trim() || 'the entered employee';
          this.confirmationTitle = `Create first DMS Administrator ${employeeName}?`;
          this.confirmationMessage = `Create an inactive employee account for ${employeeName} as ${selectedText('position', 'the selected position')} in ${selectedText('department', 'the selected department')}. The account must be activated through the approved process.`;
          this.confirmationLabel = 'Create Administrator';
        } else if (kind === 'activation') {
          this.confirmationTitle = 'Set password and complete account activation?';
          this.confirmationMessage = 'Set the password for this account. Account activation remains subject to the existing employee eligibility and authorization rules.';
          this.confirmationLabel = 'Set Password';
        } else if (kind === 'resend-activation') {
          this.confirmationTitle = `Send a fresh activation email to ${form.dataset.recordEmail}?`;
          this.confirmationMessage = 'The previous activation link will be invalidated and a fresh link will be sent to this employee.';
          this.confirmationLabel = 'Send Activation Email';
        }

        form.querySelector('dialog.action-confirmation')?.showModal();
      },
    }));
  });

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!form.dataset?.confirmationSubmitting) return;
    if (form.dataset.confirmationDispatch === 'true') {
      delete form.dataset.confirmationDispatch;
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  // Close the mobile menu before navigating so it cannot cover the anchor target.
  const navigation = document.getElementById('public-navigation');
  navigation?.addEventListener('click', (event) => {
    if (!event.target.closest('a[href]') || !navigation.classList.contains('show')) return;
    const toggle = document.querySelector('[aria-controls="public-navigation"]');
    if (toggle && getComputedStyle(toggle).display !== 'none') toggle.click();
  });

  const sidebarButtons = [...document.querySelectorAll('[data-dms-sidebar-toggle]')];
  const sidebar = document.getElementById('dms-sidebar');
  const syncSidebarButtons = () => {
    const mobile = window.matchMedia('(max-width: 991.98px)').matches;
    const mobileOpen = document.body.classList.contains('dms-sidebar-open');
    const expanded = mobile
      ? mobileOpen
      : !document.body.classList.contains('dms-sidebar-collapsed');
    if (sidebar) {
      sidebar.inert = mobile && !mobileOpen;
      sidebar.setAttribute('aria-hidden', String(mobile && !mobileOpen));
    }
    sidebarButtons.forEach((button) => {
      button.setAttribute('aria-expanded', String(expanded));
      button.setAttribute('aria-label', expanded ? 'Collapse workspace navigation' : 'Expand workspace navigation');
    });
  };
  sidebarButtons.forEach((button) => button.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 991.98px)').matches) {
      document.body.classList.toggle('dms-sidebar-open');
    } else {
      document.body.classList.toggle('dms-sidebar-collapsed');
    }
    syncSidebarButtons();
  }));
  document.querySelector('[data-dms-sidebar-close]')?.addEventListener('click', () => {
    document.body.classList.remove('dms-sidebar-open');
    syncSidebarButtons();
    document.querySelector('.dms-nav-toggle')?.focus();
  });
  sidebar?.addEventListener('click', (event) => {
    if (event.target.closest('a[href]') && window.matchMedia('(max-width: 991.98px)').matches) {
      document.body.classList.remove('dms-sidebar-open');
      syncSidebarButtons();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('dms-sidebar-open')) {
      document.body.classList.remove('dms-sidebar-open');
      syncSidebarButtons();
      document.querySelector('.dms-nav-toggle')?.focus();
    }
  });
  window.addEventListener('resize', syncSidebarButtons, { passive: true });
  syncSidebarButtons();

  document.addEventListener('click', (event) => {
    document.querySelectorAll('.dms-menu[open]').forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute('open');
    });
  });

  document.addEventListener('htmx:configRequest', (event) => {
    const target = new URL(event.detail.path, window.location.href);
    if (target.origin !== window.location.origin) return;
    const token = document.body.dataset.csrfToken;
    if (token && token !== 'NOTPROVIDED') {
      event.detail.headers['X-CSRFToken'] = token;
    }
  });
})();
