(() => {
    const connection = document.querySelector('[data-crosscheck-connection]');
    if (connection) {
        const button = connection.querySelector('button');
        const status = connection.querySelector('[data-connection-status]');
        const url = document.getElementById('id_crosscheck_service_url');
        const folder = document.getElementById('id_crosscheck_service_folder');
        const credentialsForm = document.getElementById('crosscheck-secrets');
        const credentialInputs = ['cc-web-user', 'cc-web-password', 'cc-ieee-user', 'cc-ieee-password']
            .map(id => document.getElementById(id));
        const credentialsStatus = document.querySelector('[data-crosscheck-credentials-status]');
        const accountSummary = document.querySelector('[data-crosscheck-account-summary]');
        let generation = 0;

        function invalidateConnection() {
            generation++;
            status.textContent = 'Not checked';
            status.className = 'text-muted';
        }

        function enteredConfiguration() {
            const body = new FormData(credentialsForm);
            body.set('url', url.value.trim());
            return body;
        }
        const folderPicker = window.TomSelect ? new window.TomSelect(folder, {
            create: false,
            maxItems: 1,
            maxOptions: null,
            closeAfterSelect: true,
            render: {
                option: (data, escape) => `<div>${escape(data.text)}</div>`,
                item: (data, escape) => `<div>${escape(data.text)}</div>`,
            },
        }) : null;
        if (folderPicker) folderPicker.wrapper.classList.add('cfm-paper-picker', 'cfm-folder-picker');
        url.addEventListener('input', () => {
            credentialsForm.elements.url.value = url.value;
            credentialsStatus.textContent = '';
            accountSummary.textContent = 'Saved credentials apply only to their service URL; environment overrides take precedence.';
            invalidateConnection();
        });
        credentialInputs.forEach(input => input.addEventListener('input', invalidateConnection));
        credentialsForm.addEventListener('submit', async event => {
            event.preventDefault();
            const body = enteredConfiguration();
            if (event.submitter?.name === 'remove') body.set('remove', '1');
            const requestedGeneration = generation;
            const buttons = document.querySelectorAll('button[form="crosscheck-secrets"]');
            buttons.forEach(control => { control.disabled = true; });
            credentialsStatus.textContent = 'Updating local credentials…';
            credentialsStatus.className = 'small mt-2 text-muted';
            try {
                const response = await fetch(credentialsForm.action, {method: 'POST', body, headers: {Accept: 'application/json'}});
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || 'Credential update failed.');
                if (requestedGeneration !== generation) {
                    credentialsStatus.textContent = 'Credentials saved for the submitted address and values. Your newer edits are not saved.';
                    credentialsStatus.className = 'small mt-2 text-warning';
                    return;
                }
                credentialInputs.forEach(input => { input.value = ''; });
                accountSummary.textContent = `${data.account_label} · ${data.login_label}`;
                credentialsStatus.textContent = data.message;
                credentialsStatus.className = 'small mt-2 text-success';
                invalidateConnection();
            } catch (error) {
                credentialsStatus.textContent = error.message;
                credentialsStatus.className = 'small mt-2 text-danger';
            } finally {
                buttons.forEach(control => { control.disabled = false; });
            }
        });
        button.addEventListener('click', async () => {
            const body = enteredConfiguration();
            if (!body.get('url')) {
                status.textContent = 'Enter the CrossCheck service URL.';
                status.className = 'text-danger';
                return;
            }
            const requestedGeneration = generation;
            button.disabled = true;
            status.textContent = 'Checking service and IEEE folders…';
            try {
                const response = await fetch(connection.dataset.crosscheckConnection, {method: 'POST', body});
                const data = await response.json();
                if (requestedGeneration !== generation) return;
                if (!response.ok || !data.available) throw new Error(data.message || 'Connection failed.');
                const selectedFolder = folder.value;
                if (folderPicker) {
                    folderPicker.clearOptions();
                    folderPicker.addOptions(data.folders.map(name => ({value: name, text: name})));
                    folderPicker.refreshOptions(false);
                } else {
                    const names = [...new Set([selectedFolder, ...data.folders].filter(Boolean))];
                    folder.replaceChildren(new Option('Select a conference folder', ''),
                        ...names.map(name => new Option(name, name)));
                    folder.value = selectedFolder;
                }
                status.textContent = `Ready · IEEE account: ${data.username} · ${data.folders.length} folders`;
                status.className = 'text-success';
                if (folder.value && !data.folders.includes(folder.value)) {
                    status.textContent += ' · Selected folder is unavailable'; status.className = 'text-warning';
                }
            } catch (error) {
                if (requestedGeneration === generation) { status.textContent = error.message; status.className = 'text-danger'; }
            }
            finally { button.disabled = false; }
        });
    }
    const job = document.querySelector('[data-crosscheck-job]');
    if (!job || job.dataset.status !== 'running') return;
    const form = job.querySelector('[data-job-refresh]');
    const countdown = job.querySelector('[data-job-countdown]');
    const interval = 15000;
    let timer;
    let nextUpdate;
    let updating = false;
    let stopped = false;
    countdown.hidden = false;

    function tick() {
        clearTimeout(timer);
        if (stopped || updating) return;
        if (document.hidden) {
            countdown.textContent = 'Auto-update paused while this tab is hidden.';
            return;
        }
        const seconds = Math.max(0, Math.ceil((nextUpdate - Date.now()) / 1000));
        if (seconds === 0) { refresh(); return; }
        countdown.textContent = `Next update in ${seconds}s`;
        timer = setTimeout(tick, 1000);
    }

    function schedule() {
        nextUpdate = Date.now() + interval;
        tick();
    }

    async function refresh() {
        updating = true;
        countdown.textContent = 'Checking progress…';
        const body = new FormData(form); body.set('action', 'refresh');
        try {
            const response = await fetch(location.href, {method: 'POST', body, headers: {Accept: 'application/json'}});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Progress check failed.');
            if (data.status !== 'running') {
                stopped = true;
                countdown.textContent = 'Updating batch page…';
                location.reload();
                return;
            }
            const p = data.progress;
            job.querySelector('[data-job-message]').textContent = `Uploads confirmed: ${p.succeeded || 0} · Reports ready: ${p.ready || 0} · Downloaded: ${p.downloaded || 0}`;
        } catch (error) {
            stopped = true;
            countdown.textContent = 'Auto-update paused. Use Check progress to resume.';
            job.querySelector('[data-job-message]').textContent = `${error.message} Use Check progress to resume.`;
        } finally {
            updating = false;
            if (!stopped) schedule();
        }
    }
    document.addEventListener('visibilitychange', () => {
        if (!stopped && !updating) schedule();
    });
    form.addEventListener('submit', () => {
        stopped = true;
        clearTimeout(timer);
        countdown.textContent = 'Checking progress…';
    });
    schedule();
})();
